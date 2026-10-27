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
from .workspace import WorkbenchError, create_workspace, snapshot, write_json

SUPERPOWERS = "2ce8aa15d05bd57213e6f746f953aafb7ded7a8b"


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
console.log(JSON.stringify({node:version(['node','--version']),git:version(['git','--version']),agent_cli:version(config.version_argv),commands,mounts}));
'''


def startup_diagnostics(executor, workspace, *, logs=False):
    """Bounded diagnostics from this preflight's own recorded container only."""
    state = json.loads((workspace / "runtime.json").read_text(encoding="utf-8"))
    result = runtime.docker("inspect", state["container"], check=False, timeout=10)
    if result.returncode:
        return {"inspection_error": executor.redact(result.stderr, workspace)[-2000:]}
    observed = json.loads(result.stdout)[0].get("State", {})
    diagnostic = {"container_status": observed.get("Status"), "exit_code": observed.get("ExitCode"), "container_error": observed.get("Error", "")}
    if logs:
        output = runtime.docker("logs", "--tail", "40", state["container"], check=False, timeout=10)
        diagnostic["log_tail"] = (output.stdout + "\n" + output.stderr)[-8000:]
    return executor.redact_payload(diagnostic, workspace)


def execute(executor, job, workspace, emit, canceled):
    profile = job.get("profile")
    if not profile: raise WorkbenchError("请先保存项目运行配置，并创建使用该版本的新需求工作区")
    if (workspace / "runtime.json").exists(): raise WorkbenchError("请先停止当前开发环境，再进行独立预检")
    fixed = json.loads((workspace / "workspace.json").read_text(encoding="utf-8")).get("runtime_profile")
    if not fixed or fixed["content_digest"] != profile["content_digest"]: raise WorkbenchError("运行配置尚未准备完成")
    original = snapshot(workspace / "repo")
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
        emit("preflight", {"text": "创建独立只读预检环境；不调用模型，不执行项目测试"})
        emit("preflight", {"text": executor.redact(f"检查固定 Agent CLI：{cli_name}；服务：{shlex.join(serve_argv)}；版本：{shlex.join(version_argv)}", isolated)})
        ca = Path(os.environ["CODEX_PROXY_CERT"]) if os.environ.get("CODEX_PROXY_CERT") else None
        executor.start_runtime(isolated, profile["content"]["image"], ca, read_only_source=True, opencode_model="opencode/glm-5.3-flash-free")
        deadline = time.monotonic() + 90
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
                if diagnostic.get("container_status") in ("exited", "dead"):
                    report["diagnostics"].update(startup_diagnostics(executor, isolated, logs=True))
                    raise WorkbenchError(f"预检 Agent CLI「{cli_name}」服务已退出（退出码 {diagnostic.get('exit_code')}）；实际命令：{shlex.join(serve_argv)}。请查看预检启动诊断，核对镜像中的封装 CLI 和兼容控制接口")
            if now >= deadline:
                report["diagnostics"].update(startup_diagnostics(executor, isolated, logs=True))
                raise WorkbenchError(f"预检 Agent CLI「{cli_name}」服务未就绪；实际命令：{shlex.join(serve_argv)}。请核对镜像内的配置程序、Node 环境和兼容控制接口，并查看预检启动诊断")
            time.sleep(.5)
        state = json.loads((isolated / "runtime.json").read_text(encoding="utf-8"))
        details = json.loads(runtime.docker("inspect", state["container"]).stdout)[0]
        labels = details["Config"].get("Labels") or {}
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
        result = runtime.docker("exec", state["container"], "node", "-e", CHECK_SCRIPT, json.dumps(config))
        tools = executor.redact_payload(json.loads(result.stdout), isolated)
        report["tools"] = tools; report["capabilities"] = executor.redact_payload(capabilities, isolated)
        checks = {
            "image_id": details["Image"] == fixed["image_id"],
            "agent_cli_service": bool(capabilities["health"].get("healthy")),
            "agent_cli_version": tools["agent_cli"]["ok"] and bool(tools["agent_cli"]["value"]) and (not cli_content["expected_version"] or tools["agent_cli"]["value"] == cli_content["expected_version"]),
            "superpowers_revision": labels.get("org.agent-workbench.superpowers-commit") == SUPERPOWERS,
            "superpowers_loaded": capabilities.get("superpowers_configured") and "using-superpowers" in capabilities.get("skill_names", []),
            "node_and_git": tools["node"]["ok"] and tools["git"]["ok"],
            "command_entries": all(item["available"] for item in tools["commands"]),
            "config_mounts": all(item["readable"] for item in tools["mounts"]),
            "readonly_source": capabilities["runtime"]["source_read_only"],
            "no_database_access": not any(value.startswith("PGPASSWORD=") for value in details["Config"].get("Env", [])) and not any(name.startswith("awb-data-") for name in details.get("NetworkSettings", {}).get("Networks", {})),
            "source_unchanged": snapshot(workspace / "repo") == original and snapshot(isolated / "repo")["tree_digest"] == original["tree_digest"],
        }
        report["checks"] = [{"name": key, "passed": bool(value)} for key, value in checks.items()]
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
            try: runtime.stop(isolated)
            except Exception:
                report["status"] = "UNKNOWN"
                write_json(workspace / "preflight-report.json", report)
                raise Unknown("预检容器停止尚未核实，请停止并核实当前需求") from None
        write_json(workspace / "preflight-report.json", report)
