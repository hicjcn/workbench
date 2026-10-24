"""Trusted host operations. API requests cannot provide shell commands or mounts."""
import fcntl
import json
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

    def secret_values(self, workspace):
        values = [self.settings.access_token, self.settings.runner_token, os.environ.get("OPENROUTER_API_KEY", "")]
        path = workspace / "runtime.json"
        if path.exists():
            values.append(json.loads(path.read_text()).get("password", ""))
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
        manifest = json.loads((workspace / "workspace.json").read_text())
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
    def guard_git(repo):
        directory = repo / ".git"
        if not directory.is_dir() or directory.is_symlink():
            raise WorkbenchError("工作区 Git 元数据不可信，请先停止并检查")
        if (directory / "objects/info/alternates").exists():
            raise WorkbenchError("工作区不能引用外部 Git 对象目录")
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise WorkbenchError("工作区 Git 元数据包含符号链接")

    def execute(self, job, emit, canceled):
        workspace = Path(job["workspace"]["path"])
        lock_dir = self.settings.data_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / (job["requirement_id"] + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Unknown("旧执行仍持有工作区，不能开始第二个写入者") from None
            try:
                return self._execute(job, workspace, emit, canceled)
            except Canceled:
                try:
                    if (workspace / "runtime.json").exists():
                        runtime.stop(workspace)
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
                                 job["requirement_id"], job["project"]["id"])
            check()
            return self.observe(workspace, "STOPPED")
        if action == "reconcile" and not (workspace / "workspace.json").exists():
            self.cleanup_attempts(job["requirement_id"])
            return {"environment": "NOT_CREATED"}
        if not (workspace / "workspace.json").exists():
            raise WorkbenchError("请先创建需求工作区")
        load_workspace(workspace)
        if action in ("stop", "reconcile"):
            emit("stage", {"text": "正在停止并核实运行环境"})
            self.cleanup_attempts(job["requirement_id"])
            if (workspace / "runtime.json").exists():
                runtime.stop(workspace)
            return self.observe(workspace, "STOPPED")
        if action == "start":
            if not (workspace / "runtime.json").exists():
                emit("stage", {"text": "正在启动隔离环境和 OpenCode"})
                options = {"openrouter_model": self.settings.model} if self.settings.model.startswith("openrouter/") else {"opencode_model": self.settings.model}
                ca = Path(os.environ["CODEX_PROXY_CERT"]) if os.environ.get("CODEX_PROXY_CERT") else None
                runtime.start(workspace, job["project"]["image"], ca, **options)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                check()
                try:
                    capabilities = runtime.probe(workspace)
                    if capabilities["health"].get("healthy"):
                        emit("stage", {"text": "OpenCode 已就绪，技能加载完成"})
                        return {**self.observe(workspace, "RUNNING"), "capabilities": capabilities}
                except WorkbenchError:
                    time.sleep(0.5)
            raise WorkbenchError("环境未在 90 秒内就绪，请检查镜像和运行配置")
        if action in ("refresh", "review"):
            observed = self.observe(workspace)
            if action == "review" and observed["diff_truncated"]:
                raise WorkbenchError("差异超出当前界面限制，暂不能在此确认")
            return observed
        if action == "task":
            return self.task(job, workspace, emit, canceled)
        if action == "workflow":
            from .workflow_execution import execute_step
            return execute_step(self, job, workspace, emit, canceled)
        raise WorkbenchError("未知操作")

    def cleanup_attempts(self, requirement_id):
        root = self.settings.data_dir / "attempts"
        if root.exists():
            for path in root.glob(f"*/{requirement_id}/*/runtime.json"):
                runtime.stop(path.parent)

    def task(self, job, workspace, emit, canceled, *, model=None, prompt=None, read_only=False, collect_text=False):
        state_path = workspace / "runtime.json"
        if not state_path.exists():
            raise WorkbenchError("请先启动运行环境")
        state = json.loads(state_path.read_text())
        model = model or self.settings.model
        if model.startswith("openrouter/"):
            openrouter_catalog_model(model)
        expected = runtime.model_config(model, read_only)
        configuration = safe_path(workspace, "agent-home/.config/opencode/opencode.json")
        if json.loads(configuration.read_text()) != expected:
            raise WorkbenchError("运行环境模型配置已变化，请停止并重新启动环境")
        prompt = prompt or (f"需求：{job['requirement']['title']}\n背景：{job['requirement']['description']}\n"
                  f"验收标准：{job['requirement']['acceptance']}\n本次任务：{job['input']['prompt']}\n"
                  "在当前独立工作区完成任务并执行合适验证。保留代码改动，不推送 Git，不启动子代理。")
        command = ["docker", "--host=unix:///var/run/docker.sock", "exec", state["container"], "opencode", "run",
                   "--attach", "http://127.0.0.1:4096", "--model", model, "--format", "json", prompt]
        environment = os.environ.copy()
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            environment.pop(name, None)
        emit("stage", {"text": "已提交一次 Agent 任务，正在收集输出", "model": model})
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
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
        deadline = time.monotonic() + float(os.environ.get("AWB_TASK_TIMEOUT", "300"))
        buffers = {"stdout": "", "stderr": ""}
        finished = set()
        agent_errors = False
        completed = False
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
            if not completed:
                raise Unknown("没有观察到明确结束事件，结果需核实；任务不会重放")
            result = {**self.observe(workspace, "RUNNING"), "model": model}
            if collect_text:
                # Earlier assistant text may announce a skill or explain tool use.
                # Only the final text part is the report; all parts stay in events.
                result["agent_text"] = text_parts[-1] if text_parts else ""
            return result
        finally:
            if not completed or agent_errors or canceled.is_set():
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

