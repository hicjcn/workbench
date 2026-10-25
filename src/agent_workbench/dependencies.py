"""One owned PostgreSQL dataset per workspace, checkpointed before ordinary stop."""
import hashlib
import fcntl
import json
from pathlib import Path
import secrets
import tempfile
import time
import uuid

from . import runtime
from .checked_archive import file_hash
from .workspace import WorkbenchError, safe_path, write_json

STATE = "dependency-state.json"
PRIVATE = "dependency-private.json"
JOURNAL = "dependency-runtime.json"
SNAPSHOT = "dependency-snapshot.json"
DUMP = "dependency-backups/postgres.dump"


def read(workspace, filename):
    path = safe_path(workspace, filename)
    if not path.exists(): return None
    if path.stat().st_size > 65536: raise WorkbenchError("依赖记录超过限制")
    return json.loads(path.read_text())


def spec(workspace):
    path = workspace / "workspace.json"
    if not path.exists(): return None
    from .workspace import load_workspace
    _, manifest = load_workspace(workspace)
    profile = manifest.get("runtime_profile")
    items = profile["content"].get("dependencies", []) if profile else []
    if not items: return None
    from .profiles import Content
    Content.model_validate(profile["content"])
    if not profile.get("dependency_images", {}).get("postgres"): raise WorkbenchError("依赖镜像尚未固定，请重新准备工作区")
    return {**items[0], "image_id": profile["dependency_images"]["postgres"]}


def names(workspace):
    suffix = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    return {"container": "awb-pg-" + suffix, "network": "awb-data-" + suffix, "volume": "awb-pgdata-" + suffix}


def owner_labels(root, workspace, data_id):
    from .resources import controller_id
    return {"awb.managed": "true", "awb.controller": controller_id(root), "awb.kind": "postgres",
        "awb.workspace": hashlib.sha256(str(workspace).encode()).hexdigest()[:10],
        "awb.workspace_path": str(workspace), "awb.dataset": data_id}


def labels_match(detail, expected, volume=False):
    labels = (detail.get("Labels") if volume else detail.get("Config", {}).get("Labels")) or {}
    return all(labels.get(key) == value for key, value in expected.items())


def inspect(kind, identity):
    result = runtime.docker(*( ["inspect", identity] if kind == "container" else [kind, "inspect", identity]), check=False)
    if result.returncode:
        if "no such" in result.stderr.lower() or "not found" in result.stderr.lower(): return None
        raise WorkbenchError("无法核实依赖资源是否存在，保留现场")
    return json.loads(result.stdout)[0]


def checked_container(root, workspace, data):
    identity = names(workspace)
    detail = inspect("container", identity["container"])
    if detail is None: return None
    expected = owner_labels(root, workspace, data["data_id"])
    volume = inspect("volume", identity["volume"])
    mounted = any(item.get("Type") == "volume" and item.get("Name") == identity["volume"] and item.get("Destination") == "/var/lib/postgresql/data" for item in detail.get("Mounts", []))
    if not labels_match(detail, expected) or not volume or not labels_match(volume, expected, True) or not mounted:
        raise WorkbenchError("依赖容器或数据卷归属不一致，拒绝接管或清理")
    return detail


def snapshot_info(workspace, required=False):
    info = read(workspace, SNAPSHOT)
    if not info:
        if required: raise WorkbenchError("已有依赖数据没有完整快照，不能创建空库替代")
        return None
    data = read(workspace, STATE)
    dump = safe_path(workspace, DUMP)
    if not data or info.get("data_id") != data["data_id"] or info.get("format") != "postgres17/custom" or not dump.is_file() or dump.stat().st_size != info["size"] or file_hash(dump) != info["sha256"]:
        raise WorkbenchError("数据库快照与数据集或文件摘要不一致")
    return info


def needed(root, workspace):
    if not spec(workspace): return 0
    data = read(workspace, STATE)
    detail = checked_container(root, workspace, data) if data else inspect("container", names(workspace)["container"])
    if detail and not data: raise WorkbenchError("发现没有数据集记录的依赖容器，不能接管")
    return 0 if detail else 1


def ensure(root, workspace):
    configuration = spec(workspace)
    if not configuration: return {}
    identity = names(workspace)
    data = read(workspace, STATE) or {"schema": "workbench-postgres/v1", "data_id": uuid.uuid4().hex, "initialized": False, "phase": "NOT_CREATED"}
    write_json(workspace / STATE, data)
    expected = owner_labels(root, workspace, data["data_id"])
    volume = inspect("volume", identity["volume"])
    if volume and not labels_match(volume, expected, True): raise WorkbenchError("数据卷属于其他工作区，拒绝使用")
    if not volume and data["initialized"] and not data.get("restore_required") and data["phase"] not in ("STOPPED", "RESTORED", "RESTORING", "RESTORE_PENDING"):
        from .execution import Unknown
        raise Unknown("运行中的数据卷意外缺失，不能用旧快照或空库替代，请从备份恢复到新目录")
    restore_dump = snapshot_info(workspace, required=True) if not volume and data["initialized"] else None
    private = read(workspace, PRIVATE)
    if volume and data["initialized"] and not private: raise WorkbenchError("已有数据卷的认证记录缺失，请核实或从备份恢复到新目录")
    if not private:
        private = {"password": secrets.token_urlsafe(32)}
        write_json(workspace / PRIVATE, private, private=True)
    if not volume:
        args = ["volume", "create"]
        for key, value in expected.items(): args += ["--label", key + "=" + value]
        runtime.docker(*args, identity["volume"])
        data.update(initialized=True, phase="RESTORING" if restore_dump else "INITIALIZING")
        if restore_dump: data["restore_required"] = True
        write_json(workspace / STATE, data)
    network = inspect("network", identity["network"])
    if network:
        labels = network.get("Labels") or {}
        if not all(labels.get(k) == v for k, v in expected.items()) or not network.get("Internal"): raise WorkbenchError("依赖网络归属或隔离设置不正确")
    else:
        args = ["network", "create", "--internal"]
        for key, value in expected.items(): args += ["--label", key + "=" + value]
        runtime.docker(*args, identity["network"])
    detail = checked_container(root, workspace, data)
    write_json(workspace / JOURNAL, {**identity, "phase": "STARTING"}, private=True)
    if not detail:
        args = ["run", "--detach", "--name", identity["container"], "--network", identity["network"], "--network-alias", "awb-postgres",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "DAC_OVERRIDE",
            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,size=16m",
            "--cpus", str(configuration["cpus"]), "--memory", str(configuration["memory_mib"]) + "m", "--pids-limit", str(configuration["pids_limit"]),
            "--mount", f"type=volume,src={identity['volume']},dst=/var/lib/postgresql/data", "--env", "POSTGRES_USER=app", "--env", "POSTGRES_DB=app", "--env", "POSTGRES_PASSWORD"]
        for key, value in expected.items(): args += ["--label", key + "=" + value]
        runtime.docker(*args, configuration["image_id"], environment={"POSTGRES_PASSWORD": private["password"]})
    elif not detail["State"]["Running"]: runtime.docker("start", identity["container"])
    deadline = time.monotonic() + 60
    while True:
        # Verify actual TCP readiness and authentication, after temporary initialization servers exit.
        result = runtime.docker("exec", "--env", "PGPASSWORD", identity["container"], "psql", "--host", "127.0.0.1", "--username", "app", "--dbname", "app", "--no-password", "--tuples-only", "--command", "SELECT 1", environment={"PGPASSWORD": private["password"]}, check=False, timeout=10)
        if result.returncode == 0: break
        if time.monotonic() >= deadline: raise WorkbenchError("PostgreSQL 未就绪；保留数据及运行记录，请停止并核实")
        time.sleep(.5)
    if data.get("restore_required") or data["phase"] in ("RESTORING", "RESTORE_PENDING"):
        snapshot_info(workspace, required=True)
        data["phase"] = "RESTORING"; write_json(workspace / STATE, data)
        with safe_path(workspace, DUMP).open("rb") as source:
            runtime.docker_stream("exec", "-i", identity["container"], "pg_restore", "--username", "app", "--dbname", "app", "--no-owner", "--no-acl", "--clean", "--if-exists", "--exit-on-error", source=source)
        data["restored_from"] = read(workspace, SNAPSHOT)["sha256"]
        data["restore_required"] = False
    data.update(phase="RUNNING", checked_at=time.time())
    write_json(workspace / STATE, data)
    write_json(workspace / JOURNAL, {**identity, "phase": "STARTED"}, private=True)
    return {"dependency_network": identity["network"], "dependency_environment": {
        "PGHOST": "awb-postgres", "PGPORT": "5432", "PGUSER": "app", "PGDATABASE": "app", "PGPASSWORD": private["password"]}}


def stop(root, workspace):
    if not spec(workspace): return
    from .execution import Unknown
    data = read(workspace, STATE)
    if not data: return
    identity = names(workspace)
    try:
        detail = checked_container(root, workspace, data)
        if data.get("restore_required") or data["phase"] in ("RESTORING", "RESTORE_PENDING"):
            data["restore_required"] = True
            # Never replace the certified input snapshot with a partially restored database.
            snapshot_info(workspace, required=True)
            if detail: runtime.docker("rm", "--force", identity["container"])
            network = inspect("network", identity["network"])
            if network:
                expected = owner_labels(root, workspace, data["data_id"])
                if not all((network.get("Labels") or {}).get(k) == v for k, v in expected.items()) or network.get("Containers"): raise WorkbenchError("恢复网络归属或占用状态待核实")
                runtime.docker("network", "rm", identity["network"])
            safe_path(workspace, JOURNAL).unlink(missing_ok=True)
            data.update(phase="RESTORE_PENDING", checked_at=time.time()); write_json(workspace / STATE, data)
            return
        if (detail and not detail["State"]["Running"]) or (not detail and data["initialized"] and data["phase"] not in ("STOPPED", "RESTORED")):
            # An explicit reconciliation may reopen the owned database long enough to checkpoint it.
            from .resources import inventory
            from .settings import positive_int
            admission = root / "locks/runtime-admission.lock"; admission.parent.mkdir(parents=True, exist_ok=True)
            with admission.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                extra = needed(root, workspace)
                if extra and len(inventory(root)) + extra > positive_int("AWB_MAX_CONTAINERS", 4): raise WorkbenchError("没有容量核实数据库，请先停止其他环境")
                ensure(root, workspace)
            data = read(workspace, STATE)
            detail = checked_container(root, workspace, data)
        if detail:
            if not detail["State"]["Running"]: raise WorkbenchError("依赖已离线，请明确启动后再停止以采集数据快照")
            data["phase"] = "CHECKPOINTING"; write_json(workspace / STATE, data)
            quiesced = runtime.docker("exec", identity["container"], "psql", "--username", "app", "--dbname", "app", "--no-password", "--tuples-only", "--no-align", "--command",
                "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();")
            if "f" in quiesced.stdout.split(): raise WorkbenchError("仍有数据库写入进程未确认退出")
            directory = safe_path(workspace, "dependency-backups"); directory.mkdir(mode=0o700, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=directory)
            import os
            os.close(descriptor)
            try:
                with Path(temporary).open("wb") as output:
                    runtime.docker_stream("exec", identity["container"], "pg_dump", "--username", "app", "--dbname", "app", "--format=custom", "--no-owner", "--no-acl", destination=output)
                path = Path(temporary); path.chmod(0o600)
                if path.stat().st_size > 256 * 1024 * 1024: raise WorkbenchError("数据库快照超过 256 MiB 限制")
                info = {"schema": "workbench-postgres-snapshot/v1", "format": "postgres17/custom", "data_id": data["data_id"],
                    "sha256": file_hash(path), "size": path.stat().st_size, "created_at": time.time(), "image_id": spec(workspace)["image_id"]}
                path.replace(safe_path(workspace, DUMP)); write_json(workspace / SNAPSHOT, info)
            finally: Path(temporary).unlink(missing_ok=True)
            runtime.docker("rm", "--force", identity["container"])
        elif data["initialized"]:
            if data["phase"] not in ("STOPPED", "RESTORED"): raise WorkbenchError("依赖容器意外缺失，无法证明现有快照覆盖最后写入")
            snapshot_info(workspace, required=True)
        network = inspect("network", identity["network"])
        if network:
            expected = owner_labels(root, workspace, data["data_id"])
            if not all((network.get("Labels") or {}).get(k) == v for k, v in expected.items()) or network.get("Containers"): raise WorkbenchError("依赖网络归属待核实或仍被使用")
            runtime.docker("network", "rm", identity["network"])
        safe_path(workspace, JOURNAL).unlink(missing_ok=True)
        data.update(phase="STOPPED", checked_at=time.time()); write_json(workspace / STATE, data)
    except Exception:
        data.update(phase="UNKNOWN", checked_at=time.time()); write_json(workspace / STATE, data)
        raise Unknown("PostgreSQL 数据快照或资源停止尚未核实，保留数据卷，请检查并核实依赖") from None


def status(workspace):
    data = read(workspace, STATE)
    if not data: return None
    info = read(workspace, SNAPSHOT)
    return {"kind": "postgres", "phase": data["phase"], "initialized": data["initialized"], "checked_at": data.get("checked_at"),
        "snapshot": info, "restored_from": data.get("restored_from"), "volume": names(workspace)["volume"]}


def backup_files(workspace):
    if not spec(workspace): return []
    if read(workspace, JOURNAL): raise WorkbenchError("依赖运行记录尚未核实，请先停止")
    data = read(workspace, STATE)
    if not data: return []
    if data["phase"] not in ("STOPPED", "RESTORED", "RESTORE_PENDING"): raise WorkbenchError("依赖数据状态尚未核实，拒绝备份")
    if data["initialized"]: snapshot_info(workspace, required=True)
    return [STATE, SNAPSHOT, DUMP] if data["initialized"] else [STATE]
