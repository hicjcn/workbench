"""Explicit local application lifecycle; HTTP probes run inside its own container."""
import hashlib
import json
import os
import time

from . import locking
from . import dependencies, runtime
from .platform_support import bind_mount, container_ids, container_user, mount_matches
from .profiles import Content, command, digest, runtime_options
from .container_shell import git_safe_environment, shell_argv
from .workspace import WorkbenchError, load_workspace, safe_path, snapshot, write_json

JOURNAL = "application-runtime.json"
REPORT = "application-report.json"

# The target is constructed from a validated port and path. Never follow redirects,
# return response bodies/headers, or connect to a caller-supplied host.
HTTP_PROBE = r'''
const http=require('node:http'), crypto=require('node:crypto');
const cfg=JSON.parse(process.argv[1]), started=Date.now();
let done=false;
function finish(value){if(done)return;done=true;console.log(JSON.stringify({...value,latency_ms:Date.now()-started}));}
const req=http.get({hostname:'127.0.0.1',port:cfg.port,path:cfg.health_path,agent:false},res=>{
  let bytes=0;const hash=crypto.createHash('sha256');
  res.on('data',chunk=>{bytes+=chunk.length;if(bytes>65536){res.destroy();finish({status:res.statusCode,body_truncated:true});}else hash.update(chunk);});
  res.on('end',()=>finish({status:res.statusCode,body_bytes:bytes,body_sha256:hash.digest('hex'),body_truncated:false}));
  res.on('error',()=>finish({error:'response_incomplete'}));
});
req.on('error',()=>finish({error:'connection_failed'}));
setTimeout(()=>{finish({error:'timeout'});req.destroy();},2000).unref();
'''


def read(workspace, filename): return dependencies.read(workspace, filename)


def name(workspace): return "awb-app-" + hashlib.sha256(str(workspace).encode()).hexdigest()[:16]


def code_digest(executor, workspace):
    executor.guard_git(workspace / "repo")
    return snapshot(workspace / "repo")["tree_digest"]


def spec(workspace):
    if not (workspace / "workspace.json").exists(): return None
    _, manifest = load_workspace(workspace)
    profile = manifest.get("runtime_profile")
    if not profile or not profile["content"].get("application"): return None
    Content.model_validate(profile["content"])
    if digest(profile["content"]) != profile["content_digest"]: raise WorkbenchError("应用运行配置摘要不符")
    return profile["content"]["application"]


def labels(root, workspace):
    from .resources import controller_id
    return {"awb.managed": "true", "awb.controller": controller_id(root), "awb.kind": "application",
        "awb.workspace": hashlib.sha256(str(workspace).encode()).hexdigest()[:10], "awb.workspace_path": str(workspace)}


def checked_container(root, workspace):
    detail = dependencies.inspect("container", name(workspace))
    if detail is None: return None
    _, manifest = load_workspace(workspace)
    profile = manifest.get("runtime_profile")
    source = next((item for item in detail.get("Mounts", []) if item.get("Destination") == "/workspace/project"), {})
    from .runtime_ports import bindings_match
    journal = read(workspace, JOURNAL) or {}
    if (not spec(workspace) or not dependencies.labels_match(detail, labels(root, workspace)) or
        not mount_matches(source.get("Source"), workspace / "repo") or source.get("Type") != "bind" or source.get("RW") is not False or
        detail.get("Image") != profile["image_id"] or not bindings_match(detail.get("HostConfig", {}).get("PortBindings"), journal.get("port_mappings", []))):
        raise WorkbenchError("应用容器归属、镜像或挂载不符，拒绝接管或清理")
    if profile["content"].get("run_as_root", False) and detail.get("Config", {}).get("User") != f"0:{container_ids()[1]}":
        raise WorkbenchError("应用容器运行用户与固定的 root 模式不符，拒绝接管或清理")
    for expected in profile["content"].get("volume_mounts", []):
        actual = next((item for item in detail.get("Mounts", []) if item.get("Destination") == expected["target"]), {})
        if (actual.get("Type") != "bind" or not mount_matches(actual.get("Source"), expected["source"]) or
            actual.get("RW") is not (not expected.get("read_only", True))):
            raise WorkbenchError("应用容器挂载路径或读写权限不符，拒绝接管或清理")
    return detail


def status(workspace, current_digest=None):
    journal, report = read(workspace, JOURNAL), read(workspace, REPORT)
    if not journal and not report: return None
    phase = journal["phase"] if journal else "STOPPED"
    if journal and phase != "UNKNOWN" and current_digest and journal.get("code_digest") != current_digest: phase = "STALE"
    return {"phase": phase, "checked_at": journal.get("checked_at") if journal else None,
        "code_digest": journal.get("code_digest") if journal else None, "last_check": report,
        "port_mappings": journal.get("port_mappings", []) if journal else []}


def stop(root, workspace):
    from .execution import Unknown
    if not spec(workspace) and not read(workspace, JOURNAL): return
    try:
        detail = checked_container(root, workspace)
        if detail:
            runtime.docker("rm", "--force", detail["Id"])
            if checked_container(root, workspace): raise WorkbenchError("应用容器仍存在")
        safe_path(workspace, JOURNAL).unlink(missing_ok=True)
    except Exception:
        journal = read(workspace, JOURNAL) or {}
        journal.update(phase="UNKNOWN", checked_at=time.time())
        write_json(workspace / JOURNAL, journal)
        raise Unknown("应用服务停止状态尚未核实，请停止并核实当前需求") from None


def start(executor, workspace, emit, canceled):
    from .execution import Canceled, Unknown
    from .resources import inventory
    configuration = spec(workspace)
    if not configuration: raise WorkbenchError("当前固定配置没有应用服务，请保存配置并创建新需求")
    root = executor.settings.data_dir
    if not workspace.is_relative_to(root / "workspaces"): raise WorkbenchError("只允许在开发工作区启动应用服务")
    state = read(workspace, "runtime.json")
    if not state or state.get("phase") != "STARTED": raise WorkbenchError("请先启动开发环境")
    development = dependencies.inspect("container", state["container"])
    expected = labels(root, workspace)
    expected["awb.kind"] = "agent"; expected.pop("awb.workspace_path")
    if not development or not dependencies.labels_match(development, expected) or not development["State"]["Running"]:
        raise Unknown("开发容器状态待核实，应用尚未启动")
    if read(workspace, JOURNAL) or checked_container(root, workspace): raise WorkbenchError("已有服务或运行记录，请先停止应用服务再启动")
    admission = root / "locks/runtime-admission.lock"; admission.parent.mkdir(parents=True, exist_ok=True)
    with admission.open("a+b") as lock:
        locking.flock(lock, locking.LOCK_EX)
        if len(inventory(root)) + 1 > executor.settings.max_containers: raise WorkbenchError("托管容器达到上限，请先停止其他环境")
        options = runtime_options(workspace, root)
        from .runtime_ports import allocate, publish_args
        mappings = options.get("port_mappings", [])
        reserved = [(item["host_port"], item.get("protocol", "tcp")) for item in mappings if item.get("service", "agent") == "agent" and item.get("host_port") is not None]
        port_mappings = allocate([item for item in mappings if item.get("service") == "application"], executor.settings, reserved=reserved)
        # No dependency provisioning here: the explicitly started dev environment must own it.
        network = state.get("network", "bridge") if port_mappings else "none"
        environment = dict(options.get("profile_environment", {}))
        if dependencies.spec(workspace):
            data = read(workspace, dependencies.STATE)
            database = dependencies.checked_container(root, workspace, data) if data else None
            private = read(workspace, dependencies.PRIVATE)
            if not database or not database["State"]["Running"] or not private or data["phase"] != "RUNNING":
                raise Unknown("开发数据库状态待核实，应用尚未启动")
            network = dependencies.names(workspace)["network"]
            environment.update(PGHOST="awb-postgres", PGPORT="5432", PGUSER="app", PGDATABASE="app", PGPASSWORD=private["password"])
        if canceled.is_set(): raise Canceled("应用启动已取消")
        baseline = code_digest(executor, workspace)
        run_as_root = options.get("run_as_root", False)
        journal = {"schema": "workbench-application/v1", "phase": "STARTING", "container": name(workspace),
            "code_digest": baseline, "checked_at": time.time(), "port_mappings": port_mappings, "run_as_root": run_as_root}
        write_json(workspace / JOURNAL, journal)
        limits = options["limits"]
        uid, gid = container_ids()
        if run_as_root: uid = 0
        args = ["run", "--detach", "--init", "--name", name(workspace), "--network", network,
            "--user", f"0:{gid}" if run_as_root else container_user(), "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--tmpfs", f"/home/node:rw,nosuid,nodev,size=64m,uid={uid},gid={gid}",
            "--cpus", str(limits["cpus"]), "--memory", str(limits["memory_mib"]) + "m", "--pids-limit", str(limits["pids_limit"]),
            "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=1", "--log-opt", "compress=false",
            "--workdir", "/workspace/project", "--mount", bind_mount(workspace / "repo", "/workspace/project", readonly=True),
            "--env", "HOME=/home/node", "--env", "PORT=" + str(configuration["port"])]
        if run_as_root:
            args += ["--cap-add", "DAC_OVERRIDE"]
            for key, value in git_safe_environment(workspace).items():
                args += ["--env", key + "=" + value]
        for key, value in labels(root, workspace).items(): args += ["--label", key + "=" + value]
        for mount in options["config_mounts"]:
            args += ["--mount", bind_mount(mount["source"], mount["target"], readonly=True)]
        for mount in options.get("volume_mounts", []):
            args += ["--mount", bind_mount(mount["source"], mount["target"], readonly=mount.get("read_only", True))]
        args += publish_args(port_mappings)
        for key in environment: args += ["--env", key]
        argv = command(configuration["start_command"])
        # Only the fixed initialization script is shell syntax; application argv stays separate.
        args += ["--entrypoint", "/bin/bash", options["image"], *shell_argv(argv, root=run_as_root)[1:]]
        try: runtime.docker(*args, environment=environment)
        except Exception:
            journal.update(phase="UNKNOWN"); write_json(workspace / JOURNAL, journal)
            raise Unknown("应用启动响应未核实，保留运行记录；请停止并核实，不能自动重发") from None
    emit("stage", {"text": "应用已提交，正在容器内检查固定 HTTP 路径"})
    deadline = time.monotonic() + configuration["ready_timeout_seconds"]
    while True:
        if canceled.is_set(): raise Canceled("应用启动已取消")
        report = check(executor, workspace)
        if report["passed"]: return report
        if report["phase"] in ("EXITED", "STALE") or time.monotonic() >= deadline:
            raise WorkbenchError("应用 HTTP 检查未通过；服务及日志已保留，请检查后明确停止或重试检查")
        time.sleep(.3)


def check(executor, workspace):
    from .execution import Unknown
    configuration = spec(workspace)
    if not configuration: raise WorkbenchError("当前工作区未配置应用服务")
    journal = read(workspace, JOURNAL)
    if not journal: raise WorkbenchError("应用尚未启动，请明确启动服务")
    try:
        detail = checked_container(executor.settings.data_dir, workspace)
        if not detail: raise WorkbenchError("应用容器意外缺失")
    except WorkbenchError:
        journal.update(phase="UNKNOWN", checked_at=time.time()); write_json(workspace / JOURNAL, journal)
        raise Unknown("应用容器缺失或归属待核实，请停止并核实；不会自动重启") from None
    baseline = code_digest(executor, workspace)
    report = {"schema": "workbench-http-check/v1", "checked_at": time.time(), "code_digest": journal.get("code_digest"),
        "profile_digest": json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))["runtime_profile"]["content_digest"],
        "path": configuration["health_path"], "port": configuration["port"], "expected_status": configuration["expected_status"],
        "passed": False, "phase": "FAILED", "model_inference": False, "business_tests": "NOT_RUN"}
    if baseline != journal.get("code_digest"): report["phase"] = "STALE"
    elif not detail["State"]["Running"]: report.update(phase="EXITED", exit_code=detail["State"].get("ExitCode"))
    else:
        try: result = runtime.docker("exec", name(workspace),
            *shell_argv(["node", "-e", HTTP_PROBE, json.dumps(configuration)], root=journal.get("run_as_root", False)), check=False, timeout=5)
        except Exception:
            journal.update(phase="UNKNOWN", checked_at=time.time()); write_json(workspace / JOURNAL, journal)
            raise Unknown("HTTP 检查的 Docker 执行状态未核实，请停止并核实当前需求") from None
        try:
            response = json.loads(result.stdout) if result.returncode == 0 else {"error": "probe_failed"}
            # Only the fixed probe's bounded metadata is kept. No application body or stderr.
            response = {key: response[key] for key in ("status", "body_bytes", "body_sha256", "body_truncated", "latency_ms", "error") if key in response}
        except ValueError: response = {"error": "probe_failed"}
        report["http"] = response
        report["passed"] = response.get("status") == configuration["expected_status"] and not response.get("error") and not response.get("body_truncated")
        report["phase"] = "READY" if report["passed"] else "FAILED"
        if code_digest(executor, workspace) != baseline: report.update(passed=False, phase="STALE")
    journal.update(phase=report["phase"], checked_at=report["checked_at"])
    write_json(workspace / JOURNAL, journal); write_json(workspace / REPORT, report)
    return report


def execute(executor, job, workspace, emit, canceled):
    action = job["action"]
    if action == "app_stop":
        stop(executor.settings.data_dir, workspace)
        return {**executor.observe(workspace), "application": status(workspace)}
    if action == "app_logs":
        if not checked_container(executor.settings.data_dir, workspace): raise WorkbenchError("应用容器不存在")
        result = runtime.docker("logs", "--tail", "40", name(workspace), check=False)
        if result.returncode: raise WorkbenchError("应用日志读取失败；未记录原始错误")
        text = executor.redact(result.stdout + result.stderr, workspace)
        emit("output", {"text": text[-16000:]})
        return {"application_logs": text[-16000:], "tail_lines": 40, "truncated": len(text) > 16000}
    report = start(executor, workspace, emit, canceled) if action == "app_start" else check(executor, workspace)
    if canceled.is_set():
        from .execution import Canceled
        raise Canceled("应用操作已取消")
    if not report["passed"]: raise WorkbenchError("应用 HTTP 检查未通过，请查看最新检查结果和日志")
    return {**executor.observe(workspace), "application_check": report}
