"""Discover model IDs through the pinned CLI, exclusively inside its container."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from . import runtime
from .agent_clis import builtin, checked, command
from .container_shell import shell_argv
from .platform_support import mount_matches
from .workspace import WorkbenchError, load_workspace, safe_path, write_json

REPORT = "model-catalog.json"
MAX_OUTPUT = 256 * 1024
MAX_MODELS = 2000
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:/+-]{0,238}\Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# The child timeout/output limit is enforced in the container as well, so a
# timed-out Docker client does not leave an unbounded model-listing process.
LIST_SCRIPT = r'''
const {spawnSync}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path');
const argv=JSON.parse(process.argv[1]);
let temporary;
try {
  temporary=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'awb-models-'));
  const configHome=path.join(temporary,'config');fs.mkdirSync(configHome);
  const original=path.resolve(process.env.XDG_CONFIG_HOME || path.join(process.env.HOME || '', '.config'));
  const managed='/home/node/.config/opencode/opencode.json';
  function managedFile(filename){
    try{const a=fs.statSync(filename),b=fs.statSync(managed);return a.dev===b.dev && a.ino===b.ino;}
    catch{return false;}
  }
  function link(source,target){
    const directory=fs.existsSync(source) && fs.statSync(source).isDirectory();
    fs.symlinkSync(source,target,directory?'junction':'file');
  }
  // Preserve credential/config locations through symlinks. Only the generated
  // workbench config is omitted; genuine image/user OpenCode config remains.
  if(fs.existsSync(original))for(const entry of fs.readdirSync(original)){
    const source=path.join(original,entry),target=path.join(configHome,entry);
    if(entry!=='opencode'){link(source,target);continue;}
    fs.mkdirSync(target);
    for(const child of fs.readdirSync(source)){
      const from=path.join(source,child);
      if(!managedFile(from))link(from,path.join(target,child));
    }
  }
  const neutral=path.join(temporary,'opencode.json');fs.writeFileSync(neutral,'{}');
  const env={...process.env,XDG_CONFIG_HOME:configHome,OPENCODE_CONFIG:neutral};
  const result=spawnSync(argv[0],argv.slice(1),{env,encoding:'utf8',timeout:30000,maxBuffer:262144,killSignal:'SIGKILL'});
  if(result.error){console.log(JSON.stringify({ok:false,error:result.error.code==='ETIMEDOUT'?'timeout':'start_or_output_limit'}));}
  else if(result.status!==0){console.log(JSON.stringify({ok:false,error:'exit',exit_code:result.status}));}
  else{console.log(JSON.stringify({ok:true,stdout:result.stdout}));}
}catch{console.log(JSON.stringify({ok:false,error:'configuration_or_start'}));}
finally{if(temporary)fs.rmSync(temporary,{recursive:true,force:true});}
'''


def parse(output):
    """Accept CLI lines or JSON arrays of IDs / objects with an id and name."""
    if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT:
        raise WorkbenchError("模型列表输出超过大小限制")
    clean = ANSI.sub("", output).strip()
    if not clean: return []
    structured = clean.startswith(("[", "{"))
    if structured:
        try: rows = json.loads(clean)
        except ValueError: raise WorkbenchError("CLI 模型列表不是有效 JSON") from None
        if isinstance(rows, dict): rows = rows.get("models", rows.get("data"))
        if not isinstance(rows, list): raise WorkbenchError("CLI 模型 JSON 需要模型数组")
    else:
        rows = clean.splitlines()
    result, seen = [], set()
    for row in rows:
        identity = row.get("id") if isinstance(row, dict) else row
        if not isinstance(identity, str):
            if structured: raise WorkbenchError("CLI 模型列表包含无效条目")
            continue
        identity = identity.strip()
        if len(identity) > 240 or not MODEL_ID.fullmatch(identity):
            # Plain command banners are ignored, never offered as model IDs.
            if structured: raise WorkbenchError("CLI 模型列表包含无效模型标识")
            continue
        if identity in seen: continue
        label = row.get("name", row.get("label", identity)) if isinstance(row, dict) else identity
        if not isinstance(label, str) or len(label) > 240 or any(ord(char) < 32 for char in label): label = identity
        result.append({"id": identity, "label": label.strip() or identity})
        seen.add(identity)
        if len(result) > MAX_MODELS: raise WorkbenchError("CLI 模型数量超过上限")
    if not result and rows: raise WorkbenchError("CLI 输出中没有可解析的 provider/model 模型标识")
    return result


def _journal(workspace):
    path = safe_path(workspace, "runtime.json")
    if not path.exists(): return None
    if path.stat().st_size > 128 * 1024: raise WorkbenchError("运行记录超过大小限制")
    return json.loads(path.read_text(encoding="utf-8"))


def _identity(workspace):
    workspace, manifest = load_workspace(workspace)
    selected = manifest.get("agent_cli") or builtin()
    checked(selected)
    state = _journal(workspace)
    image = (manifest.get("runtime_profile") or {}).get("image_id") or (state or {}).get("image", "")
    return workspace, selected, image, (manifest.get("runtime_profile") or {}).get("content_digest")


def read(workspace):
    """Return only a catalog still bound to this workspace's pinned CLI/image."""
    empty = {"status": "NOT_LOADED", "models": []}
    try:
        workspace, selected, image, profile_digest = _identity(workspace)
        path = safe_path(workspace, REPORT)
        if not path.exists() or path.stat().st_size > 4 * MAX_OUTPUT: return empty
        report = json.loads(path.read_text(encoding="utf-8"))
        if (report.get("agent_cli_id") != selected["id"] or report.get("agent_cli_digest") != selected["content_digest"]
                or report.get("image") != image or report.get("profile_digest") != profile_digest): return empty
        if report.get("status") not in ("READY", "EMPTY", "FAILED"): return empty
        rows = report.get("models")
        if not isinstance(rows, list) or len(rows) > MAX_MODELS: return empty
        if any(not isinstance(row, dict) or not MODEL_ID.fullmatch(row.get("id", "")) or len(row.get("id", "")) > 240 for row in rows): return empty
        return {key: report[key] for key in ("status", "models", "agent_cli_id", "agent_cli_digest", "image", "profile_digest", "checked_at", "error") if key in report}
    except (OSError, ValueError, TypeError, AttributeError, WorkbenchError):
        return empty


def require_models(workspace, models):
    report = read(Path(workspace))
    if report["status"] != "READY": raise WorkbenchError("请先从当前 Agent CLI 读取可用模型列表")
    available = {item["id"] for item in report["models"]}
    if any(model not in available for model in models): raise WorkbenchError("所选模型不在当前固定 Agent CLI 的模型列表中，请刷新后重新选择")
    return report


def _container(workspace, state, image):
    suffix = hashlib.sha256(str(workspace).encode()).hexdigest()[:10]
    _, manifest = load_workspace(workspace)
    expected = f"awb-{manifest['requirement'][:20]}-{manifest['project'][:20]}-{suffix}"
    if state.get("phase") not in ("STARTED", "DEBUG_RUNNING") or state.get("container") != expected:
        raise WorkbenchError("请先启动当前需求环境，再读取模型列表")
    inspected = runtime.docker("inspect", expected, check=False, timeout=10)
    if inspected.returncode: raise WorkbenchError("无法核实当前需求容器，请检查环境状态")
    detail = json.loads(inspected.stdout)[0]
    labels = detail.get("Config", {}).get("Labels") or {}
    mount = next((item for item in detail.get("Mounts", []) if item.get("Destination") == "/workspace/project"), {})
    if (labels.get("awb.managed") != "true" or labels.get("awb.workspace") != suffix or labels.get("awb.kind", "agent") != "agent"
            or (detail.get("Image") != image and detail.get("Config", {}).get("Image") != image) or mount.get("Type") != "bind" or not mount_matches(mount.get("Source"), workspace / "repo")):
        raise WorkbenchError("容器归属、镜像或工作区挂载不符，不能读取模型列表")
    if not detail.get("State", {}).get("Running"): raise WorkbenchError("当前需求容器未运行，请先启动环境")
    return expected


def execute(executor, job, workspace, emit, canceled):
    from .execution import Canceled
    workspace, selected, image, profile_digest = _identity(workspace)
    report = {"schema": "workbench-model-catalog/v1", "status": "FAILED", "models": [],
        "agent_cli_id": selected["id"], "agent_cli_digest": selected["content_digest"],
        "image": image, "profile_digest": profile_digest, "checked_at": time.time()}
    path = safe_path(workspace, REPORT)
    try:
        if canceled.is_set(): raise Canceled("模型列表读取已取消")
        state = _journal(workspace)
        if not state: raise WorkbenchError("请先启动当前需求环境，再读取模型列表")
        runtime_cli = state.get("agent_cli")
        if runtime_cli and (runtime_cli.get("id"), runtime_cli.get("content_digest")) != (selected["id"], selected["content_digest"]):
            raise WorkbenchError("当前容器与固定 Agent CLI 不一致，请停止并重新启动环境")
        container = _container(workspace, state, image)
        argv = command(selected, "list_models")
        emit("stage", {"text": "正在当前需求容器中读取 Agent CLI 模型列表，不调用模型"})
        result = runtime.docker("exec", container,
            *shell_argv(["node", "-e", LIST_SCRIPT, json.dumps(argv)], root=state.get("run_as_root", False)),
            check=False, timeout=40)
        if canceled.is_set(): raise Canceled("模型列表读取已取消")
        if result.returncode or len(result.stdout.encode("utf-8")) > 2 * MAX_OUTPUT:
            raise WorkbenchError("模型列表命令执行失败，请检查 CLI 配置、Node 环境及登录状态")
        try: payload = json.loads(result.stdout)
        except ValueError: raise WorkbenchError("模型列表命令未返回有效结果，请检查 CLI 和 Node 环境") from None
        if not payload.get("ok"):
            if payload.get("error") == "timeout": raise WorkbenchError("模型列表命令在 30 秒内未完成")
            raise WorkbenchError("模型列表命令失败或输出超过限制，请检查 CLI 命令及登录状态")
        report["models"] = parse(executor.redact(payload.get("stdout", ""), workspace))
        report["status"] = "READY" if report["models"] else "EMPTY"
        write_json(path, report)
        return {**executor.observe(workspace), "model_catalog": report}
    except Canceled:
        report["error"] = "模型列表读取已取消"
        write_json(path, report)
        raise
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired, WorkbenchError) as error:
        # CLI stderr and Docker diagnostics may contain credentials. Only our
        # own bounded messages are persisted; never copy raw command output.
        public_error = str(error) if isinstance(error, WorkbenchError) and not str(error).startswith("Docker operation") else "无法读取 Agent CLI 模型列表，请检查当前环境"
        report["error"] = public_error
        write_json(path, report)
        raise WorkbenchError(public_error) from None
