"""Offline, checked backups and restore to a new directory; no credential copying."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
import zipfile

from sqlalchemy import delete, insert, select, update

from .checked_archive import build, verify, name_safe, file_hash
from . import resources
from .execution import Executor
from .settings import Settings
from .store import Store, metadata, jobs, workspaces, projects, reservations, encoded
from .workspace import WorkbenchError, safe_path, snapshot, write_json
from .server import migrate


def rows_snapshot(store):
    with store.engine.connect() as connection:
        return {table.name: [dict(row) for row in connection.execute(select(table)).mappings()] for table in metadata.sorted_tables}


def metadata_digest(rows):
    return {name: {"rows": len(items), "sha256": hashlib.sha256(encoded(sorted(items, key=encoded)).encode()).hexdigest()} for name, items in rows.items()}


def stop(settings):
    with resources.controller_lock(settings.data_dir, exclusive=True):
        migrate(settings)
        report = resources.stop_all(settings.data_dir)
        store = Store(settings.database_url)
        try:
            with store.engine.begin() as connection:
                incomplete = connection.execute(select(jobs).where(jobs.c.status.in_(["QUEUED", "RUNNING", "UNKNOWN"]))).mappings().all()
                from .workflow import invalidate
                for job in incomplete:
                    if job["status"] != "QUEUED": invalidate(connection, job["requirement_id"])
                    connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="CANCELED", error="维护已停止并核实资源；需要明确提交新任务", lease_until=None, updated_at=time.time()))
                connection.execute(delete(reservations))
                for workspace in connection.execute(select(workspaces)).mappings().all():
                    observed = Executor(settings).observe(Path(workspace["path"]))
                    values = {"environment": "STOPPED" if "snapshot" in observed else "NOT_CREATED", "updated_at": time.time()}
                    if observed.get("snapshot"):
                        invalidate(connection, workspace["requirement_id"], observed["snapshot"]["tree_digest"])
                        values.update(snapshot_json=encoded(observed["snapshot"]), diff=observed["diff"], diff_truncated=observed["diff_truncated"])
                    connection.execute(update(workspaces).where(workspaces.c.requirement_id == workspace["requirement_id"]).values(**values))
                report["reconciled_jobs"] = len(incomplete)
        finally: store.engine.dispose()
        write_json(settings.data_dir / "maintenance-last.json", {**report, "at": time.time()})
        return report


def workspace_entries(root):
    entries, checkpoints = [], {}
    for workspace in resources.known_workspaces(root):
        Executor.guard_git(workspace / "repo")
        current = snapshot(workspace / "repo")
        relative = workspace.relative_to(root).as_posix()
        checkpoints[relative] = {"tree_digest": current["tree_digest"], "commit_sha": current["commit_sha"]}
        entries += [("files/" + relative + "/repo/" + item["path"], safe_path(workspace / "repo", item["path"])) for item in current["files"]]
        for path in (workspace / "repo/.git").rglob("*"):
            if path.is_symlink(): raise WorkbenchError("Git 元数据包含符号链接")
            if path.is_file(): entries.append(("files/" + path.relative_to(root).as_posix(), path))
        for filename in ("workspace.json", "capability-report.json", "preflight-report.json"):
            path = workspace / filename
            if path.exists(): entries.append(("files/" + path.relative_to(root).as_posix(), path))
    for folder in ("exports", "deliveries"):
        directory = root / folder
        if directory.is_symlink(): raise WorkbenchError("正式存储目录不可信")
        if directory.exists():
            entries += [("files/" + path.relative_to(root).as_posix(), path) for path in directory.iterdir() if path.is_file()]
    return entries, checkpoints


def backup(settings):
    with resources.controller_lock(settings.data_dir, exclusive=True):
        migrate(settings)
        if resources.inventory(settings.data_dir): raise WorkbenchError("仍有托管容器，请先运行维护 stop")
        if any((path / "runtime.json").exists() for path in resources.known_workspaces(settings.data_dir)): raise WorkbenchError("运行记录尚未核实，请先运行维护 stop")
        store = Store(settings.database_url)
        try:
            data = rows_snapshot(store)
            if any(row["status"] in ("QUEUED", "RUNNING", "UNKNOWN") for row in data["jobs"]): raise WorkbenchError("仍有排队、执行或未知任务，请先运行维护 stop")
            if data["workspace_reservations"]: raise WorkbenchError("工作区预留尚未解除")
            entries, checkpoints = workspace_entries(settings.data_dir)
            workspace_names = {name for name, _ in entries}
            for name, values in data.items(): entries.append(("database/" + name + ".json", encoded(values).encode()))
            identity = "backup_" + secrets.token_hex(16)
            target = settings.data_dir / "backups" / (identity + ".zip")
            header = {"schema": "workbench-backup/v1", "schema_revision": "0006", "created_at": time.time(),
                "source_root": str(settings.data_dir), "metadata": metadata_digest(data), "code_checkpoints": checkpoints,
                "excluded": ["access-token", "runner-token", "agent-home", "runtime-config", "staging", "runtime.json", "backups", "external original repositories", "database credentials"]}
            result = build(target, header, entries)
            after_entries, after = workspace_entries(settings.data_dir)
            if checkpoints != after or workspace_names != {name for name, _ in after_entries} or metadata_digest(rows_snapshot(store)) != header["metadata"]:
                raise WorkbenchError("备份期间代码或元数据变化，备份不能生效")
            verified = verify(target, result)
            for item in verified["entries"]:
                if item["path"].startswith("files/") and file_hash(safe_path(settings.data_dir, item["path"].removeprefix("files/"))) != item["sha256"]:
                    raise WorkbenchError("备份期间持久文件发生变化")
            index = {"id": identity, "sha256": result["sha256"], "size": result["size"], "created_at": header["created_at"], "workspace_count": len(checkpoints)}
            write_json(target.with_suffix(".json"), index)
            return {**index, "file": str(target), "control_credential_files_included": False}
        finally: store.engine.dispose()


def restore(archive_path, destination, project_maps=None):
    archive_path = archive_path.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink(): raise WorkbenchError("恢复目标必须是尚不存在的新目录")
    manifest = verify(archive_path)
    if manifest.get("schema") != "workbench-backup/v1" or manifest.get("schema_revision") not in ("0005", "0006"): raise WorkbenchError("备份格式或数据版本不支持")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        data = {}
        with zipfile.ZipFile(archive_path) as archive:
            for item in manifest["entries"]:
                name = item["path"]
                if name.startswith("database/"):
                    table = name.removeprefix("database/").removesuffix(".json")
                    if table not in metadata.tables or name != f"database/{table}.json": raise WorkbenchError("备份包含未知数据表")
                    data[table] = json.loads(archive.read(name))
                    continue
                if not name.startswith("files/"): raise WorkbenchError("备份包含未知目录")
                relative = name.removeprefix("files/")
                if relative.split("/")[0] not in ("workspaces", "attempts", "exports", "deliveries"): raise WorkbenchError("备份文件越过允许目录")
                parts = Path(relative).parts
                offset = 3 if parts[0] == "workspaces" else 4 if parts[0] == "attempts" else None
                if offset is not None:
                    suffix = parts[offset:]
                    if not suffix or (suffix[0] != "repo" and suffix not in [("workspace.json",), ("capability-report.json",), ("preflight-report.json",)]): raise WorkbenchError("备份包含禁止恢复的认证或运行状态")
                elif len(parts) != 2 or not parts[-1].endswith(".zip"): raise WorkbenchError("正式文件存储路径不正确")
                target = safe_path(staging, relative); target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as output: shutil.copyfileobj(source, output, length=1024 * 1024)
                target.chmod(item["mode"])
        expected_tables = set(metadata.tables)
        if manifest["schema_revision"] == "0005": expected_tables -= {"project_profiles", "workspace_profiles"}
        if set(data) != expected_tables or metadata_digest(data) != manifest["metadata"]: raise WorkbenchError("备份元数据摘要不完整")
        for name in set(metadata.tables) - set(data): data[name] = []
        if data["workspace_reservations"] or any(item["status"] in ("QUEUED", "RUNNING", "UNKNOWN") for item in data["jobs"]): raise WorkbenchError("备份含未经核实的任务，不能自动恢复")
        for relative, expected in manifest["code_checkpoints"].items():
            name_safe(relative)
            workspace = safe_path(staging, relative)
            Executor.guard_git(workspace / "repo")
            observed = snapshot(workspace / "repo")
            if {"tree_digest": observed["tree_digest"], "commit_sha": observed["commit_sha"]} != expected: raise WorkbenchError("恢复源码摘要与备份不一致")
            path = workspace / "workspace.json"; content = json.loads(path.read_text()); content["repo"] = str(destination / relative / "repo"); write_json(path, content)
        source_root = Path(manifest["source_root"])
        req_projects = {item["id"]: item["project_id"] for item in data["requirements"]}
        for row in data["workspaces"]:
            old = Path(row["path"])
            if not old.is_relative_to(source_root / "workspaces"): raise WorkbenchError("工作区路径不在原控制目录内")
            relative = old.relative_to(source_root)
            name_safe(relative.as_posix())
            if relative.as_posix() != f"workspaces/{row['requirement_id']}/{req_projects[row['requirement_id']]}": raise WorkbenchError("工作区成员路径不正确")
            if relative.as_posix() not in manifest["code_checkpoints"] and row["environment"] != "NOT_CREATED": raise WorkbenchError("必要工作区文件缺失")
            row["path"] = str(destination / relative)
        for row in data["projects"]:
            if project_maps and row["id"] in project_maps:
                from .workspace import git
                mapped = Path(project_maps[row["id"]]).resolve(strict=True)
                if Path(git(mapped, "rev-parse", "--show-toplevel").decode().strip()) != mapped: raise WorkbenchError("项目映射必须是 Git 根目录")
                row["source_path"] = str(mapped)
        settings = Settings(staging, f"sqlite:///{staging / 'workbench.db'}", "unused-restore-token" * 2, "unused-runner-token" * 2)
        migrate(settings)
        store = Store(settings.database_url)
        try:
            with store.engine.begin() as connection:
                for table in metadata.sorted_tables:
                    if data[table.name]: connection.execute(insert(table), data[table.name])
            actual = rows_snapshot(store)
            if metadata_digest(actual) != metadata_digest(data): raise WorkbenchError("恢复数据库校验失败")
        finally: store.engine.dispose()
        for name in ("access-token", "runner-token"):
            descriptor = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream: stream.write(secrets.token_urlsafe(32))
        info = {"schema": "workbench-restore/v1", "restored_at": time.time(), "archive_sha256": file_hash(archive_path),
            "source_root": str(source_root), "target_root": str(destination), "workspace_count": len(manifest["code_checkpoints"]), "credentials_regenerated": True,
            "sessions_replayed": False, "project_maps": project_maps or {}, "database": str(destination / "workbench.db")}
        write_json(staging / "restore-info.json", info)
        if destination.exists(): raise WorkbenchError("恢复目标已被创建，拒绝覆盖")
        staging.rename(destination)
        return info
    finally:
        if staging.exists(): shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inspect", "stop", "backup", "restore"])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--project-map", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.action == "restore":
            if not args.archive or not args.destination: raise WorkbenchError("恢复需要 --archive 和 --destination")
            report = restore(args.archive, args.destination, dict(item.split("=", 1) for item in args.project_map))
        else:
            settings = Settings.load(args.data_dir)
            report = {"containers": resources.inventory(settings.data_dir)} if args.action == "inspect" else stop(settings) if args.action == "stop" else backup(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except WorkbenchError as failure: parser.exit(1, str(failure) + "\n")
    except Exception: parser.exit(1, "维护操作失败；未覆盖已有目录，请检查本机配置和服务状态\n")


if __name__ == "__main__": main()
