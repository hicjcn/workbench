"""Inspect and stop only verified resources owned by this data directory."""
import hashlib
import json
from pathlib import Path
from contextlib import contextmanager

from . import locking
from . import runtime
from .platform_support import mount_matches
from .workspace import WorkbenchError, load_workspace


def controller_id(root): return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


@contextmanager
def controller_lock(root, exclusive=False):
    directory = root / "locks"; directory.mkdir(parents=True, exist_ok=True)
    with (directory / "controller.lock").open("a+b") as handle:
        try: locking.flock(handle, (locking.LOCK_EX if exclusive else locking.LOCK_SH) | locking.LOCK_NB)
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
        path = known.get(labels.get("awb.workspace"))
        verified = bool(path and mount and mount.get("Type") == "bind" and mount_matches(mount.get("Source"), path / "repo") and
            path.is_relative_to(root) and path.resolve() == path and path.name and
            hashlib.sha256(str(path).encode()).hexdigest()[:10] == labels.get("awb.workspace") and
            (path.is_relative_to(root / "workspaces") or path.is_relative_to(root / "attempts")))
        kind = labels.get("awb.kind", "agent")
        journal = "runtime.json"
        if kind == "postgres":
            from . import dependencies
            path = known.get(labels.get("awb.workspace"))
            data = dependencies.read(path, dependencies.STATE) if path else None
            owned = dependencies.checked_container(root, path, data) if data else None
            verified = bool(owned and owned["Id"] == detail["Id"])
            journal = dependencies.JOURNAL
        elif kind == "application":
            from . import application
            path = known.get(labels.get("awb.workspace"))
            owned = application.checked_container(root, path) if path else None
            verified = bool(owned and owned["Id"] == detail["Id"] and path.is_relative_to(root / "workspaces"))
            journal = application.JOURNAL
        result.append({"id": identity, "name": detail["Name"].lstrip("/"), "workspace": str(path) if path else None,
            "kind": kind, "verified": verified, "orphan": bool(path and not (path / journal).exists()), "running": detail["State"]["Running"],
            "image_id": detail["Image"], "memory_limit": detail["HostConfig"].get("Memory"), "cpu_limit": detail["HostConfig"].get("NanoCpus", 0) / 1e9,
            "networks": list(detail.get("NetworkSettings", {}).get("Networks", {}))})
    return result


def stop_workspace(root, workspace):
    from .application import stop as stop_application
    stop_application(root, workspace)
    if (workspace / "runtime.json").exists(): runtime.stop(workspace)
    removed = False
    for item in inventory(root):
        if item["workspace"] == str(workspace) and item["kind"] != "postgres":
            if not item["verified"]: raise WorkbenchError("发现归属不明资源，不能自动清理")
            runtime.docker("rm", "--force", item["id"])
            removed = True
    from .dependencies import stop
    stop(root, workspace)
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
        from .application import stop as stop_application
        stop_application(root, workspace)
        if (workspace / "runtime.json").exists(): runtime.stop(workspace)
    for item in inventory(root):
        if item["kind"] != "postgres": runtime.docker("rm", "--force", item["id"])
    from .dependencies import stop
    for workspace in known_workspaces(root): stop(root, workspace)
    # A network without a container is also identified by controller label or known name.
    cleanup_networks(root, strict=True)
    if inventory(root): raise WorkbenchError("仍有托管容器，停止状态未核实")
    return {"stopped_containers": len(found), "code_and_evidence_preserved": True}
