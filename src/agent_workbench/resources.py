"""Inspect and stop only verified resources owned by this data directory."""
import fcntl
import hashlib
import json
from pathlib import Path
from contextlib import contextmanager

from . import runtime
from .workspace import WorkbenchError, load_workspace


def controller_id(root): return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


@contextmanager
def controller_lock(root, exclusive=False):
    directory = root / "locks"; directory.mkdir(parents=True, exist_ok=True)
    with (directory / "controller.lock").open("a") as handle:
        try: fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError: raise WorkbenchError("工作台仍在运行或正在维护，请停止 API/Runner 后再重试") from None
        yield


def known_workspaces(root):
    paths = [path.parent for pattern in ("workspaces/*/*/workspace.json", "attempts/*/*/*/workspace.json") for path in root.glob(pattern)]
    for path in paths:
        resolved, _ = load_workspace(path)
        if resolved != path or not resolved.is_relative_to(root): raise WorkbenchError("工作区指向控制目录之外")
    return paths


def inventory(root):
    known = {hashlib.sha256(str(path).encode()).hexdigest()[:10]: path for path in known_workspaces(root)}
    ids = runtime.docker("ps", "-a", "--filter", "label=awb.managed=true", "--format", "{{.ID}}").stdout.split()
    result = []
    for identity in ids:
        detail = json.loads(runtime.docker("inspect", identity).stdout)[0]
        labels = detail["Config"].get("Labels") or {}
        if labels.get("awb.controller") != controller_id(root) and labels.get("awb.workspace") not in known: continue
        mount = next((item for item in detail.get("Mounts", []) if item["Destination"] == "/workspace/project"), None)
        path = Path(mount["Source"]).parent if mount else None
        verified = bool(path and path.is_relative_to(root) and path.resolve() == path and path.name and
            hashlib.sha256(str(path).encode()).hexdigest()[:10] == labels.get("awb.workspace") and
            (path.is_relative_to(root / "workspaces") or path.is_relative_to(root / "attempts")))
        result.append({"id": identity, "name": detail["Name"].lstrip("/"), "workspace": str(path) if path else None,
            "verified": verified, "orphan": bool(path and not (path / "runtime.json").exists()), "running": detail["State"]["Running"],
            "image_id": detail["Image"], "memory_limit": detail["HostConfig"].get("Memory"), "cpu_limit": detail["HostConfig"].get("NanoCpus", 0) / 1e9,
            "networks": list(detail.get("NetworkSettings", {}).get("Networks", {}))})
    return result


def stop_workspace(root, workspace):
    if (workspace / "runtime.json").exists(): runtime.stop(workspace)
    removed = False
    for item in inventory(root):
        if item["workspace"] == str(workspace):
            if not item["verified"]: raise WorkbenchError("发现归属不明资源，不能自动清理")
            runtime.docker("rm", "--force", item["id"])
            removed = True
    if removed: cleanup_networks(root)


def cleanup_networks(root, strict=False):
    expected = {"awb-req-" + hashlib.sha256(str(path.parent).encode()).hexdigest()[:10] for path in known_workspaces(root)}
    for identity in runtime.docker("network", "ls", "--filter", "label=awb.managed=true", "--format", "{{.ID}}").stdout.split():
        network = json.loads(runtime.docker("network", "inspect", identity).stdout)[0]
        if (network.get("Labels") or {}).get("awb.controller") == controller_id(root) or network["Name"] in expected:
            if network.get("Containers"):
                if strict: raise WorkbenchError("网络仍被使用，不能清理")
                continue
            runtime.docker("network", "rm", identity)


def stop_all(root):
    found = inventory(root)
    if any(not item["verified"] for item in found): raise WorkbenchError("存在无法核实归属的容器，停止自动清理")
    for workspace in known_workspaces(root):
        if (workspace / "runtime.json").exists(): runtime.stop(workspace)
    for item in inventory(root):
        runtime.docker("rm", "--force", item["id"])
    # A network without a container is also identified by controller label or known name.
    cleanup_networks(root, strict=True)
    if inventory(root): raise WorkbenchError("仍有托管容器，停止状态未核实")
    return {"stopped_containers": len(found), "code_and_evidence_preserved": True}
