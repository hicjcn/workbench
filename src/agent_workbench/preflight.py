"""Real environment checks without model inference or business test assertions."""
import json
import os
from pathlib import Path
import time

from . import runtime
from .execution import Canceled, Unknown
from .profiles import command, inherit, runtime_options
from .workspace import WorkbenchError, create_workspace, snapshot, write_json

OPENCODE = "1.19.4"
SUPERPOWERS = "2ce8aa15d05bd57213e6f746f953aafb7ded7a8b"


def read_report(workspace):
    path = workspace / "preflight-report.json"
    if not path.exists(): return None
    if path.is_symlink() or path.stat().st_size > 128 * 1024: raise WorkbenchError("预检记录不可信")
    return json.loads(path.read_text())


def require_passed(workspace, profile):
    report = read_report(workspace)
    if not report or report["status"] != "PASSED" or report["profile_digest"] != profile["content_digest"]:
        raise WorkbenchError("请先对当前固定运行配置执行环境预检并处理阻塞项")


CHECK_SCRIPT = r'''
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const config = JSON.parse(process.argv[1]);
function version(exe) { const result = spawnSync(exe, ['--version'], {encoding:'utf8', timeout:20000, maxBuffer:65536}); return {ok:result.status===0, value:(result.stdout||'').trim().slice(0,200)}; }
function cliVersion() { const args = config.version_argv || ['opencode','--version']; const result = spawnSync(args[0],args.slice(1),{encoding:'utf8',timeout:20000,maxBuffer:65536}); return {ok:result.status===0,value:(result.stdout||'').trim().slice(0,200)}; }
const commands = Object.entries(config.commands).map(([name, argv]) => ({name, argv, available:!argv.length || spawnSync('sh',['-c','command -v "$1"','awb',argv[0]],{encoding:'utf8',timeout:5000}).status===0}));
const mounts = config.mounts.map(target => ({target, readable: (()=>{try { fs.accessSync(target,fs.constants.R_OK); return true;}catch{return false;}})()}));
console.log(JSON.stringify({node:version('node'),git:version('git'),opencode:cliVersion(),commands,mounts}));
'''


def execute(executor, job, workspace, emit, canceled):
    profile = job.get("profile")
    if not profile: raise WorkbenchError("请先保存项目运行配置，并创建使用该版本的新需求工作区")
    if (workspace / "runtime.json").exists(): raise WorkbenchError("请先停止当前开发环境，再进行独立预检")
    fixed = json.loads((workspace / "workspace.json").read_text()).get("runtime_profile")
    if not fixed or fixed["content_digest"] != profile["content_digest"]: raise WorkbenchError("运行配置尚未准备完成")
    original = snapshot(workspace / "repo")
    created = create_workspace(workspace / "repo", executor.settings.data_dir / "attempts" / job["id"],
        job["requirement_id"], job["project"]["id"], include_dirty=True, include_untracked=True)
    isolated = Path(created["workspace"]); inherit(workspace, isolated, executor.settings.data_dir)
    report = {"schema": "workbench-preflight/v1", "status": "FAILED", "profile_id": profile["id"], "profile_digest": profile["content_digest"],
        "image_id": fixed["image_id"], "code_digest": original["tree_digest"], "checked_at": time.time(), "model_execution": "NOT_ATTEMPTED",
        "business_tests": "NOT_RUN", "build": "NOT_RUN", "checks": [], "job_id": job["id"]}
    try:
        emit("preflight", {"text": "创建独立只读预检环境；不调用模型，不执行项目测试"})
        ca = Path(os.environ["CODEX_PROXY_CERT"]) if os.environ.get("CODEX_PROXY_CERT") else None
        executor.start_runtime(isolated, profile["content"]["image"], ca, read_only_source=True, opencode_model="opencode/glm-5.3-flash-free")
        deadline = time.monotonic() + 90
        while True:
            if canceled.is_set(): raise Canceled("环境预检已取消")
            try:
                capabilities = runtime.probe(isolated)
                if capabilities["health"].get("healthy"): break
            except WorkbenchError: pass
            if time.monotonic() >= deadline: raise WorkbenchError("预检服务未就绪，请检查镜像是否包含所需 OpenCode 和 Node 环境")
            time.sleep(.5)
        state = json.loads((isolated / "runtime.json").read_text())
        details = json.loads(runtime.docker("inspect", state["container"]).stdout)[0]
        labels = details["Config"].get("Labels") or {}
        config = {"commands": {"build": command(profile["content"]["build_command"]), "verify": command(profile["content"]["verification_command"])},
            "mounts": [item["target"] for item in profile["content"]["mounts"]]}
        from .agent_clis import checked, command as cli_command
        agent_cli = json.loads((isolated / "workspace.json").read_text()).get("agent_cli")
        cli_content = checked(agent_cli)
        config["version_argv"] = cli_command(agent_cli, "version")
        if agent_cli: report["agent_cli"] = {key: agent_cli[key] for key in ("id", "name", "number", "content_digest")}
        if profile["content"].get("application"): config["commands"]["application"] = command(profile["content"]["application"]["start_command"])
        result = runtime.docker("exec", state["container"], "node", "-e", CHECK_SCRIPT, json.dumps(config))
        tools = json.loads(executor.redact(result.stdout, isolated))
        report["tools"] = tools; report["capabilities"] = capabilities
        checks = {
            "image_id": details["Image"] == fixed["image_id"],
            "opencode_version": tools["opencode"]["ok"] and bool(tools["opencode"]["value"]) and (not cli_content["expected_version"] or tools["opencode"]["value"] == cli_content["expected_version"]),
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
    except Exception:
        report["status"] = "FAILED"
        raise
    finally:
        if (isolated / "runtime.json").exists():
            try: runtime.stop(isolated)
            except Exception:
                report["status"] = "UNKNOWN"
                write_json(workspace / "preflight-report.json", report)
                raise Unknown("预检容器停止尚未核实，请停止并核实当前需求") from None
        write_json(workspace / "preflight-report.json", report)
