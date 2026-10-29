"""Real environment checks without model inference or business test assertions."""
import json
import os
from pathlib import Path
import shlex
import time

from .platform_support import is_link
from . import runtime
from .execution import Canceled, Unknown
from .profiles import command, inherit
from .container_shell import shell_argv
from .workspace import WorkbenchError, create_workspace, snapshot, write_json
from . import logging_support as logs

def read_report(workspace):
    path = workspace / "preflight-report.json"
    if not path.exists(): return None
    if is_link(path) or path.stat().st_size > 128 * 1024: raise WorkbenchError("预检记录不可信")
    return json.loads(path.read_text(encoding="utf-8"))


def require_passed(workspace, profile):
    report = read_report(workspace)
    if not report or report["status"] != "PASSED" or report["profile_digest"] != profile["content_digest"]:
        raise WorkbenchError("请先对当前固定运行配置执行环境预检并处理阻塞项")


CHECK_SCRIPT = r'''
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const config = JSON.parse(process.argv[1]);
function version(args) { const result = spawnSync(args[0], args.slice(1), {encoding:'utf8', timeout:20000, maxBuffer:65536}); return {ok:result.status===0, value:(result.stdout?.trim() || result.stderr?.trim() || '').slice(0,200), exit_code:result.status, error:result.error?.code || null}; }
if (!Array.isArray(config.version_argv) || !config.version_argv.length) throw new Error('Missing configured Agent CLI version command');
const commands = Object.entries(config.commands).map(([name, argv]) => ({name, argv, available:!argv.length || spawnSync('sh',['-c','command -v "$1"','awb',argv[0]],{encoding:'utf8',timeout:5000,cwd:config.repository_paths?.[name]}).status===0}));
const mounts = config.mounts.map(target => ({target, readable: (()=>{try { fs.accessSync(target,fs.constants.R_OK); return true;}catch{return false;}})()}));
console.log(JSON.stringify({node:version(['node','--version']),git:version(['git','--version']),agent_cli:version(config.version_argv),commands,mounts,no_database_credentials:!process.env.PGPASSWORD}));
'''


def startup_diagnostics(executor, workspace, *, logs=False):
    """Bounded diagnostics from this preflight's own recorded container only."""
    state = json.loads((workspace / "runtime.json").read_text(encoding="utf-8"))
    result = runtime.docker("inspect", state["container"], check=False, timeout=10)
    if result.returncode:
        return {"inspection_error": executor.redact(result.stderr, workspace)[-2000:]}
    details = json.loads(result.stdout)[0]
    observed = details.get("State", {})
    diagnostic = {"container_status": observed.get("Status"), "exit_code": observed.get("ExitCode"), "container_error": observed.get("Error", "")}
    config = details.get("Config") or {}
    environment = dict(item.split("=", 1) for item in config.get("Env") or [] if "=" in item)
    configured = state.get("serve_argv") or [details.get("Path") or ""]
    diagnostic.update(container_path=environment.get("PATH", "")[:4000], container_home=environment.get("HOME", "")[:2000],
        container_user=config.get("User", "")[:200], executable=configured[0][:2000],
        launcher=(details.get("Path") or "")[:2000])
    if logs:
        output = runtime.docker("logs", "--tail", "40", state["container"], check=False, timeout=10)
        diagnostic["log_tail"] = (output.stdout + "\n" + output.stderr)[-8000:]
    service = runtime.service_status(state, details=details, logs=logs)
    if service:
        diagnostic["container_exit_code"] = diagnostic["exit_code"]
        if logs: diagnostic["container_log_tail"] = diagnostic.get("log_tail", "")
        diagnostic.update(service)
        if service.get("agent_exit_code") is not None:
            diagnostic["exit_code"] = service["agent_exit_code"]
    return executor.redact_payload(diagnostic, workspace)


def execute(executor, job, workspace, emit, canceled):
    profile = job.get("profile")
    if not profile: raise WorkbenchError("请先保存项目运行配置，并创建使用该版本的新需求工作区")
    if (workspace / "runtime.json").exists(): raise WorkbenchError("请先停止当前开发环境，再进行独立预检")
    fixed = json.loads((workspace / "workspace.json").read_text(encoding="utf-8")).get("runtime_profile")
    if not fixed or fixed["content_digest"] != profile["content_digest"]: raise WorkbenchError("运行配置尚未准备完成")
    logs.progress(emit, "preflight.snapshot", "正在核对预检前的源码快照", detailed=True)
    original = snapshot(workspace / "repo")
    logs.progress(emit, "preflight.copy", "正在创建独立只读预检副本")
    created = create_workspace(workspace / "repo", executor.settings.data_dir / "attempts" / job["id"],
        job["requirement_id"], job["project"]["id"], include_dirty=True, include_untracked=True)
    isolated = Path(created["workspace"]); inherit(workspace, isolated, executor.settings.data_dir)
    from .agent_clis import builtin, checked, command as cli_command
    agent_cli = json.loads((isolated / "workspace.json").read_text(encoding="utf-8")).get("agent_cli") or builtin()
    cli_content = checked(agent_cli)
    cli_name = agent_cli["name"]
    serve_argv, version_argv = cli_command(agent_cli, "serve"), cli_command(agent_cli, "version")
    report = {"schema": "workbench-preflight/v1", "status": "FAILED", "profile_id": profile["id"], "profile_digest": profile["content_digest"],
        "image_id": fixed["image_id"], "code_digest": original["tree_digest"], "checked_at": time.time(), "model_execution": "NOT_ATTEMPTED",
        "business_tests": "NOT_RUN", "build": "NOT_RUN", "checks": [], "job_id": job["id"],
        "agent_cli": {key: agent_cli[key] for key in ("id", "name", "number", "content_digest")},
        "commands": executor.redact_payload({"serve": serve_argv, "version": version_argv}, isolated)}
    try:
        logs.progress(emit, "preflight.start", "只读副本已准备，正在启动固定 Agent CLI")
        emit("preflight", {"text": executor.redact(f"检查固定 Agent CLI：{cli_name}；服务：{shlex.join(serve_argv)}；版本：{shlex.join(version_argv)}", isolated)})
        ca = Path(os.environ["CODEX_PROXY_CERT"]) if os.environ.get("CODEX_PROXY_CERT") else None
        try:
            executor.start_runtime(isolated, profile["content"]["image"], ca, read_only_source=True, opencode_model="opencode/glm-5.3-flash-free")
        except Exception as error:
            report["checks"] = [{"name": "agent_cli_service", "passed": False}]
            report["diagnostics"] = {"launch_error": executor.redact(str(error), isolated)[:4000]}
            if (isolated / "runtime.json").exists():
                try:
                    report["diagnostics"].update(startup_diagnostics(executor, isolated, logs=True))
                except Exception as diagnostic_error:
                    # Preserve the startup error even if Docker cannot inspect a failed create.
                    report["diagnostics"]["inspection_error"] = executor.redact(str(diagnostic_error), isolated)[:2000]
            raise
        deadline = time.monotonic() + 90
        logs.progress(emit, "preflight.wait_ready", "容器已提交，正在等待 Agent CLI 控制服务就绪")
        inspected_at, control_error = 0, ""
        while True:
            if canceled.is_set(): raise Canceled("环境预检已取消")
            try:
                capabilities = runtime.probe(isolated)
                if capabilities["health"].get("healthy"):
                    report.pop("diagnostics", None)
                    break
            except WorkbenchError as error:
                control_error = executor.redact(str(error), isolated)[:2000]
            now = time.monotonic()
            if now >= inspected_at:
                diagnostic = startup_diagnostics(executor, isolated)
                report["diagnostics"] = {**diagnostic, "control_error": control_error}
                inspected_at = now + 2
                if (diagnostic.get("container_status") in ("exited", "dead") or
                    diagnostic.get("agent_status") in ("exited", "unavailable")):
                    report["diagnostics"].update(startup_diagnostics(executor, isolated, logs=True))
                    raise WorkbenchError(f"预检 Agent CLI「{cli_name}」服务已退出（退出码 {diagnostic.get('exit_code')}）；实际命令：{shlex.join(serve_argv)}。请查看预检启动诊断，核对镜像中的封装 CLI 和兼容控制接口")
            if now >= deadline:
                report["diagnostics"].update(startup_diagnostics(executor, isolated, logs=True))
                raise WorkbenchError(f"预检 Agent CLI「{cli_name}」服务未就绪；实际命令：{shlex.join(serve_argv)}。请核对镜像内的配置程序、Node 环境和兼容控制接口，并查看预检启动诊断")
            time.sleep(.5)
        state = json.loads((isolated / "runtime.json").read_text(encoding="utf-8"))
        logs.progress(emit, "preflight.tools", "Agent CLI 服务已就绪，正在检查版本、开发工具与挂载")
        details = json.loads(runtime.docker("inspect", state["container"]).stdout)[0]
        config = {"commands": {"build": command(profile["content"]["build_command"]), "verify": command(profile["content"]["verification_command"])},
            "mounts": [item["target"] for item in profile["content"]["mounts"]]}
        from .multi_repo import layout
        configured = layout(workspace / 'repo')
        if configured:
            config['repository_paths'] = {}
            selected = {item['path'] for item in configured['repositories']}
            for item in profile['content'].get('repository_commands', []):
                if item['path'] not in selected: continue
                for kind in ('build', 'verify'):
                    key = item['path'] + '/' + kind
                    config['commands'][key] = command(item['build_command' if kind == 'build' else 'verification_command'])
                    config['repository_paths'][key] = '/workspace/project/' + item['path']
        config["version_argv"] = version_argv
        if profile["content"].get("application"): config["commands"]["application"] = command(profile["content"]["application"]["start_command"])
        result = runtime.docker("exec", state["container"],
            *shell_argv(["node", "-e", CHECK_SCRIPT, json.dumps(config)], root=state.get("run_as_root", False)))
        tools = executor.redact_payload(json.loads(result.stdout), isolated)
        report["tools"] = tools; report["capabilities"] = executor.redact_payload(capabilities, isolated)
        checks = {
            "image_id": details["Image"] == fixed["image_id"],
            "agent_cli_service": bool(capabilities["health"].get("healthy")),
            "agent_cli_version": tools["agent_cli"]["ok"] and bool(tools["agent_cli"]["value"]) and (not cli_content["expected_version"] or tools["agent_cli"]["value"] == cli_content["expected_version"]),
            "superpowers_loaded": capabilities.get("superpowers_configured") and "using-superpowers" in capabilities.get("skill_names", []),
            "node_and_git": tools["node"]["ok"] and tools["git"]["ok"],
            "command_entries": all(item["available"] for item in tools["commands"]),
            "config_mounts": all(item["readable"] for item in tools["mounts"]),
            "readonly_source": capabilities["runtime"]["source_read_only"],
            "no_database_access": tools.get("no_database_credentials") is True and not any(value.startswith("PGPASSWORD=") for value in details["Config"].get("Env", [])) and not any(name.startswith("awb-data-") for name in details.get("NetworkSettings", {}).get("Networks", {})),
            "source_unchanged": snapshot(workspace / "repo") == original and snapshot(isolated / "repo")["tree_digest"] == original["tree_digest"],
        }
        report["checks"] = [{"name": key, "passed": bool(value)} for key, value in checks.items()]
        for name, passed in checks.items():
            logs.progress(emit, "preflight.check." + name, "检查项：" + name + ("（通过）" if passed else "（未通过）"), detailed=True)
        report["status"] = "PASSED" if all(checks.values()) else "FAILED"
        if report["status"] != "PASSED": raise WorkbenchError("环境预检存在阻塞项，请查看预检记录；未调用模型或执行项目测试")
        emit("preflight", {"text": "工具环境预检通过；模型连通性、构建及业务测试尚未验证"})
        return {**executor.observe(workspace, "STOPPED"), "preflight": report}
    except Exception as error:
        report["status"] = "FAILED"
        report["error"] = executor.redact(str(error), isolated)[:4000]
        raise
    finally:
        if (isolated / "runtime.json").exists():
            logs.event("preflight", "preflight.cleanup", "正在停止并核实本次预检容器")
            try:
                runtime.stop(isolated)
                logs.event("preflight", "preflight.cleaned", "本次预检容器已停止并核实")
            except Exception:
                report["status"] = "UNKNOWN"
                write_json(workspace / "preflight-report.json", report)
                raise Unknown("预检容器停止尚未核实，请停止并核实当前需求") from None
        write_json(workspace / "preflight-report.json", report)
