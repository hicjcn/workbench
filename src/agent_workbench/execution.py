"""Trusted host operations; project commands execute only inside managed containers."""
from .platform_support import is_link
from . import locking
from .platform_support import docker_argv, docker_environment
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from . import runtime
from .providers import openrouter_catalog_model
from .workspace import WorkbenchError, create_workspace, git, load_workspace, safe_path, snapshot


class Canceled(WorkbenchError):
    pass


class Unknown(WorkbenchError):
    pass


class Executor:
    def __init__(self, settings):
        self.settings = settings

    def start_runtime(self, workspace, image, proxy_ca=None, **options):
        from . import resources
        if options.get("context_mounts"):
            if not options.get("read_only_source"): raise WorkbenchError("上下文只能挂载到只读分析环境")
            targets = set()
            for mount in options["context_mounts"]:
                path = Path(mount["source"])
                if not path.is_relative_to(self.settings.data_dir / "attempts") or path.name != "repo": raise WorkbenchError("上下文必须来自独立分析副本")
                actual, manifest = load_workspace(path.parent)
                if actual != path.parent or mount["target"] != "/workspace/candidates/" + manifest["project"] or mount["target"] in targets:
                    raise WorkbenchError("上下文路径或项目标识不符")
                self.guard_git(path); targets.add(mount["target"])
        admission = self.settings.data_dir / "locks" / "runtime-admission.lock"
        admission.parent.mkdir(parents=True, exist_ok=True)
        with admission.open("a+b") as lock:
            locking.flock(lock, locking.LOCK_EX)
            from . import dependencies
            use_dependencies = workspace.is_relative_to(self.settings.data_dir / "workspaces") and not options.get("read_only_source")
            extra = dependencies.needed(self.settings.data_dir, workspace) if use_dependencies else 0
            if len(resources.inventory(self.settings.data_dir)) + 1 + extra > self.settings.max_containers:
                raise WorkbenchError("托管容器达到上限，请停止不需要的环境后明确重试")
            from .profiles import runtime_options
            configured = runtime_options(workspace, self.settings.data_dir)
            image = configured.pop("image", image)
            if options.get("openrouter_model") and not os.environ.get("OPENROUTER_API_KEY", "").strip():
                raise WorkbenchError("Configure the existing OPENROUTER_API_KEY secret before starting OpenRouter")
            if use_dependencies: configured.update(dependencies.ensure(self.settings.data_dir, workspace))
            return runtime.start(workspace, image, proxy_ca, controller=resources.controller_id(self.settings.data_dir), **configured, **options)

    def secret_values(self, workspace):
        values = [self.settings.access_token, self.settings.runner_token, os.environ.get("OPENROUTER_API_KEY", "")]
        path = workspace / "runtime.json"
        if path.exists():
            values.append(json.loads(path.read_text(encoding="utf-8")).get("password", ""))
        from .dependencies import read, PRIVATE
        dependency = read(workspace, PRIVATE) if workspace.exists() else None
        if dependency: values.append(dependency["password"])
        manifest = workspace / "workspace.json"
        if manifest.exists() and not is_link(manifest):
            agent = json.loads(manifest.read_text(encoding="utf-8")).get("agent_cli")
            if agent:
                from .agent_clis import checked
                values.extend(os.environ.get(name, "") for name in checked(agent)["credential_env"])
        return [value for value in values if value]

    def redact(self, text, workspace):
        for value in self.secret_values(workspace):
            text = text.replace(value, "[REDACTED]")
        return text

    def observe(self, workspace, environment=None):
        if not (workspace / "workspace.json").exists():
            return {"environment": environment or "NOT_CREATED"}
        load_workspace(workspace)
        self.guard_git(workspace / "repo")
        current = snapshot(workspace / "repo")
        manifest = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
        if manifest.get("layout") == "multi-repo/v1":
            from .multi_repo import diff as multi_diff
            content = multi_diff(workspace).decode(errors="replace")
            result = {"snapshot": current, "diff": self.redact(content[:500000], workspace), "diff_truncated": len(content) > 500000}
            if environment: result["environment"] = environment
            return result
        baseline = manifest["source_baseline"]["commit_sha"]
        diff = git(workspace / "repo", "diff", "--no-ext-diff", "--no-textconv", "--binary", baseline).decode(errors="replace")
        tracked = set(git(workspace / "repo", "ls-files", "-z").decode().rstrip("\0").split("\0"))
        truncated = False
        for item in current["files"]:
            if item["path"] not in tracked:
                content = safe_path(workspace / "repo", item["path"]).read_bytes()
                decoded = content.decode("utf-8", errors="replace")
                truncated |= len(decoded) > 100000
                display = decoded[:100000]
                diff += f"\n--- /dev/null\n+++ b/{item['path']}\n" + "\n".join("+" + line for line in display.splitlines())
        truncated |= len(diff) > 500000
        diff = self.redact(diff[:500000], workspace)
        result = {"snapshot": current, "diff": diff, "diff_truncated": truncated}
        if environment:
            result["environment"] = environment
        return result

    @staticmethod
    def guard_git(repo, *, single_repository=False):
        from .multi_repo import layout
        configured = None if single_repository else layout(repo)
        if configured:
            for item in configured["repositories"]:
                Executor.guard_git(safe_path(repo, item["path"]), single_repository=True)
            return
        directory = repo / ".git"
        if not directory.is_dir() or is_link(directory):
            raise WorkbenchError("工作区 Git 元数据不可信，请先停止并检查")
        if (directory / "objects/info/alternates").exists():
            raise WorkbenchError("工作区不能引用外部 Git 对象目录")
        if any(is_link(path) for path in directory.rglob("*")):
            raise WorkbenchError("工作区 Git 元数据包含符号链接")

    def execute(self, job, emit, canceled):
        if job["action"] == "delivery":
            from .delivery_execution import execute
            return execute(self, job, emit, canceled)
        workspace = Path(job["workspace"]["path"])
        lock_dir = self.settings.data_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / (job["requirement_id"] + ".lock")).open("a+b") as lock:
            try:
                locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
            except BlockingIOError:
                raise Unknown("旧执行仍持有工作区，不能开始第二个写入者") from None
            try:
                return self._execute(job, workspace, emit, canceled)
            except Canceled:
                try:
                    from .resources import stop_workspace
                    stop_workspace(self.settings.data_dir, workspace)
                except (WorkbenchError, subprocess.TimeoutExpired):
                    raise Unknown("中止尚未核实，工作区保持阻塞") from None
                raise

    def _execute(self, job, workspace, emit, canceled):
        def check():
            if canceled.is_set():
                raise Canceled("操作已取消，已有代码保留")
        check()
        action = job["action"]
        if action == "prepare":
            if not workspace.exists():
                emit("stage", {"text": "正在创建独立源码副本"})
                create_workspace(Path(job["project"]["source_path"]), workspace.parents[1],
                                 job["requirement_id"], job["project"]["id"],
                                 repository_paths=job["requirement"].get("repositories"), shared_files=job["project"].get("shared_files"))
            check()
            from .profiles import configure
            configure(workspace, job.get("profile"), self.settings.data_dir)
            from .agent_clis import configure as configure_agent
            configure_agent(workspace, job.get("agent_cli"))
            return self.observe(workspace, "STOPPED")
        if action == "reconcile" and not (workspace / "workspace.json").exists():
            self.cleanup_attempts(job["requirement_id"])
            return {"environment": "NOT_CREATED"}
        if not (workspace / "workspace.json").exists():
            raise WorkbenchError("请先创建需求工作区")
        load_workspace(workspace)
        if action in ("start", "task", "workflow", "materials", "preflight"):
            from .agent_clis import verify_workspace
            verify_workspace(workspace, job.get("agent_cli"))
        if action == "preflight":
            from .preflight import execute
            return execute(self, job, workspace, emit, canceled)
        if action in ("start", "task", "workflow", "materials", "build", "verify", "app_start", "app_check") and job.get("profile"):
            from .preflight import require_passed
            require_passed(workspace, job["profile"])
        if action in ("stop", "reconcile"):
            emit("stage", {"text": "正在停止并核实运行环境"})
            self.cleanup_attempts(job["requirement_id"])
            from .resources import stop_workspace
            stop_workspace(self.settings.data_dir, workspace)
            return self.observe(workspace, "STOPPED")
        if action == "start":
            if not (workspace / "runtime.json").exists():
                emit("stage", {"text": "正在启动隔离环境和固定 Agent CLI"})
                options = {"openrouter_model": self.settings.model} if self.settings.model.startswith("openrouter/") else {"opencode_model": self.settings.model}
                ca = Path(os.environ["CODEX_PROXY_CERT"]) if os.environ.get("CODEX_PROXY_CERT") else None
                self.start_runtime(workspace, job["project"]["image"], ca, **options)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                check()
                try:
                    capabilities = runtime.probe(workspace)
                    if capabilities["health"].get("healthy"):
                        emit("stage", {"text": "兼容 OpenCode 的 Agent 服务已就绪，技能配置已收集"})
                        return {**self.observe(workspace, "RUNNING"), "capabilities": capabilities}
                except WorkbenchError:
                    time.sleep(0.5)
            raise WorkbenchError("环境未在 90 秒内就绪，请检查镜像和运行配置")
        if action in ("refresh", "review"):
            observed = self.observe(workspace)
            if action == "review" and observed["diff_truncated"]:
                raise WorkbenchError("差异超出当前界面限制，暂不能在此确认")
            return observed
        if action in ("app_start", "app_stop", "app_check", "app_logs"):
            from .application import execute
            return execute(self, job, workspace, emit, canceled)
        if action == "task":
            return self.task(job, workspace, emit, canceled)
        if action in ("build", "verify"):
            from .profiles import command, repository_plans
            from .workflow_execution import verify_command
            profile = job.get("profile")
            if not profile: raise WorkbenchError("请先为项目保存运行配置，并创建新的需求工作区")
            selected = job['requirement'].get('repositories', [])
            if selected and profile['content'].get('repository_commands'):
                plans = repository_plans(profile['content'], selected, action, require_all=action == 'verify')
                if not plans: raise WorkbenchError('当前候选仓库未配置构建命令')
                output = verify_command(self, job, workspace, emit, canceled, plans=plans)
                return {**self.observe(workspace), 'command_output': output}
            argv = command(profile["content"]["build_command" if action == "build" else "verification_command"])
            if not argv: raise WorkbenchError("当前配置未登记此命令")
            output = verify_command(self, job, workspace, emit, canceled, argv=argv)
            return {**self.observe(workspace), "command_output": output}
        if action == "workflow":
            from .workflow_execution import execute_step
            return execute_step(self, job, workspace, emit, canceled)
        if action == "materials":
            from .material_execution import execute
            return execute(self, job, workspace, emit, canceled)
        raise WorkbenchError("未知操作")

    def cleanup_attempts(self, requirement_id):
        root = self.settings.data_dir / "attempts"
        if root.exists():
            from .resources import stop_workspace
            for path in root.glob(f"*/{requirement_id}/*/workspace.json"):
                stop_workspace(self.settings.data_dir, path.parent)

    def task(self, job, workspace, emit, canceled, *, model=None, prompt=None, read_only=False, collect_text=False):
        state_path = workspace / "runtime.json"
        if not state_path.exists():
            raise WorkbenchError("请先启动运行环境")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        model = model or self.settings.model
        if model.startswith("openrouter/"):
            openrouter_catalog_model(model)
        expected = runtime.model_config(model, read_only, state.get("context_paths"))
        configuration = safe_path(workspace, "agent-home/.config/opencode/opencode.json")
        if not runtime.matches_model_config(json.loads(configuration.read_text(encoding="utf-8")), expected):
            raise WorkbenchError("运行环境模型配置已变化，请停止并重新启动环境")
        prompt = prompt or (f"需求：{job['requirement']['title']}\n背景：{job['requirement']['description']}\n"
                  f"验收标准：{job['requirement']['acceptance']}\n本次任务：{job['input']['prompt']}\n"
                  "在当前独立工作区完成任务并执行合适验证。保留代码改动，不推送 Git，不启动子代理。")
        from .multi_repo import layout
        selected_repositories = layout(workspace / "repo")
        if selected_repositories:
            prompt += "\n当前目录是多个独立 Git 仓库的共同父目录。仅使用下列候选仓库，可在一次会话中分析或修改多个仓库；各仓库分别使用 git -C <路径>，父目录无需初始化 Git。分析和评审保持全部源码只读。\n" + json.dumps(
                {"repositories": [item["path"] for item in selected_repositories["repositories"]], "shared_files": [item["path"] for item in selected_repositories["common_baseline"]]}, ensure_ascii=False)
        if job.get("profile"):
            content = job["profile"]["content"]
            context = {"profile_id": job["profile"]["id"], "build_command": content["build_command"], "verification_command": content["verification_command"],
                "configuration_files": [item["target"] for item in content["mounts"]], "dependencies": [item["kind"] for item in content.get("dependencies", [])],
                "application": content.get("application"),
                "database_access": "not_connected_in_readonly_session" if read_only else "use_PGHOST_PGPORT_PGUSER_PGDATABASE_PGPASSWORD_environment_variables"}
            prompt += "\n固定运行配置（不包含认证内容，不在答复或产物中写入凭据）：" + json.dumps(context, ensure_ascii=False)
        from .agent_clis import builtin, checked, command as agent_command
        selected = state.get("agent_cli") or builtin()
        if job.get("agent_cli") and (selected["id"] != job["agent_cli"]["id"] or selected["content_digest"] != job["agent_cli"]["content_digest"]):
            raise WorkbenchError("任务与运行环境的 Agent CLI 固定版本不一致，请停止并核实")
        cli_content = checked(selected)
        command = docker_argv("exec", "-i", state["container"], *agent_command(selected, "run", model=model))
        environment = docker_environment()
        emit("stage", {"text": "正在提交一次 Agent 任务并收集输出", "model": model})
        try:
            timeout = float(os.environ.get("AWB_TASK_TIMEOUT", "300")) if selected["id"] == "builtin-opencode" else cli_content["timeout_seconds"]
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
        except ValueError:
            raise WorkbenchError("AWB_TASK_TIMEOUT 必须是有限正数；尚未启动任务") from None
        deadline = time.monotonic() + timeout
        # OpenCode 1.19.4 reads piped stdin. Large documents exceed argv's per-argument limit.
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=environment)
        input_finished = threading.Event()
        input_errors = []
        def write_input():
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (OSError, ValueError):
                input_errors.append(True)
            finally:
                input_finished.set()
        threading.Thread(target=write_input, daemon=True).start()
        messages = queue.Queue()
        def read(pipe, label):
            try:
                while piece := pipe.readline(65536):
                    messages.put((label, piece))
            finally:
                messages.put((label, None))
                pipe.close()
        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            threading.Thread(target=read, args=(pipe, name), daemon=True).start()
        buffers = {"stdout": "", "stderr": ""}
        finished = set()
        agent_errors = False
        completed = False
        text_seen = False
        skill_reads = set()
        stream_digest = hashlib.sha256()
        text_parts = []
        text_length = 0
        try:
            while len(finished) < 2 or process.poll() is None:
                if canceled.is_set() or time.monotonic() > deadline:
                    try:
                        runtime.stop(workspace)
                        process.wait(timeout=5)
                    except (WorkbenchError, subprocess.TimeoutExpired):
                        raise Unknown("任务停止状态未核实，禁止重放") from None
                    if canceled.is_set():
                        raise Canceled("任务已停止，已有代码保留")
                    raise Unknown("任务超时并已停止；结果需人工核实，不自动重放")
                try:
                    stream, piece = messages.get(timeout=0.2)
                except queue.Empty:
                    continue
                if piece is None:
                    finished.add(stream)
                    continue
                buffers[stream] += piece
                while "\n" in buffers[stream]:
                    line, buffers[stream] = buffers[stream].split("\n", 1)
                    text = self.redact(line, workspace)
                    try:
                        item = json.loads(text)
                    except ValueError:
                        item = None
                    if isinstance(item, dict):
                        from .opencode import skill_usage
                        stream_digest.update(text.encode()); skill_reads.update(skill_usage(item))
                        text_seen |= item.get("type") == "text" and isinstance(item.get("part"), dict) and isinstance(item["part"].get("text"), str) and bool(item["part"]["text"].strip())
                        agent_errors |= item.get("type") == "error"
                        completed |= item.get("type") == "step_finish" and item.get("part", {}).get("reason") == "stop"
                        if collect_text and item.get("type") == "text":
                            part = item.get("part", {}).get("text", "")
                            if isinstance(part, str):
                                text_length += len(part)
                                if text_length > 80000:
                                    raise WorkbenchError("模型正文超过报告大小限制")
                                text_parts.append(part)
                        emit("agent", {"text": text[:16000]})
                    elif text:
                        emit("output", {"text": text[:16000], "stream": stream})
                if len(buffers[stream]) > 1_000_000:
                    emit("output", {"text": "超长输出已截断", "stream": stream})
                    buffers[stream] = ""
            if process.returncode != 0 or agent_errors:
                raise WorkbenchError("Agent 执行返回错误，请查看日志及保留的代码差异")
            if not input_finished.wait(1) or input_errors:
                raise Unknown("Agent 输入传输未确认完成，结果需核实")
            if not completed:
                raise Unknown("没有观察到明确结束事件，结果需核实；任务不会重放")
            emit("model_completion", {"schema": "workbench-model-completion/v1", "model": model, "completed": True, "agent_cli_id": selected["id"],
                "text_seen": bool(text_seen), "skills": sorted(skill_reads), "stream_digest": stream_digest.hexdigest()})
            result = {**self.observe(workspace, "RUNNING"), "model": model, "agent_cli": {key: selected[key] for key in ("id", "name", "number", "content_digest")}}
            if collect_text:
                # Earlier assistant text may announce a skill or explain tool use.
                # Only the final text part is the report; all parts stay in events.
                result["agent_text"] = text_parts[-1] if text_parts else ""
            return result
        finally:
            if not completed or agent_errors or canceled.is_set() or input_errors or not input_finished.is_set():
                try:
                    if (workspace / "runtime.json").exists():
                        runtime.stop(workspace)
                except (WorkbenchError, subprocess.TimeoutExpired):
                    raise Unknown("与 Agent 断开后，实际任务停止状态仍待核实") from None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()

