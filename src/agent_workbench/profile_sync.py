"""Explicit runtime rebinding with staged files and recoverable commit records."""
import json
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import and_, insert, select, update

from . import agent_clis, profiles
from .platform_support import is_link, retry_file_access
from .store import Conflict, Missing, public
from . import store as tables
from .workspace import WorkbenchError, load_workspace, safe_path, write_json

JOURNAL = "profile-sync.json"
REPORTS = ("preflight-report.json", "capability-report.json", "application-report.json", "model-catalog.json")


def latest(connection, requirement_id):
    requirement = connection.execute(select(tables.requirements).where(tables.requirements.c.id == requirement_id)).mappings().first()
    if not requirement: raise Missing("需求不存在")
    connection.execute(select(tables.projects.c.id).where(tables.projects.c.id == requirement["project_id"]).with_for_update()).one()
    row = connection.execute(select(tables.project_profiles).where(and_(tables.project_profiles.c.project_id == requirement["project_id"],
        tables.project_profiles.c.is_current.is_(True)))).mappings().first()
    profile = public(row)
    chosen = profile and profile["content"].get("agent_cli_id")
    agent = agent_clis.get(connection, chosen) if chosen else agent_clis.default(connection)
    return profile, agent


def describe(connection, requirement_id, current_profile, current_agent):
    profile, agent = latest(connection, requirement_id)
    return {"latest_profile_id": profile["id"] if profile else None, "latest_profile_number": profile["number"] if profile else None,
        "latest_agent_cli_id": agent["id"], "latest_agent_cli_number": agent["number"],
        "has_update": bool(profile and (not current_profile or current_profile["id"] != profile["id"] or
            current_agent["id"] != agent["id"] or current_agent["content_digest"] != agent["content_digest"]))}


def enqueue_target(connection, requirement_id):
    workspace = connection.execute(select(tables.workspaces).where(tables.workspaces.c.requirement_id == requirement_id)).mappings().one()
    if not workspace["snapshot_json"]: raise Conflict("请先创建需求工作区，再同步运行配置")
    if workspace["environment"] not in ("STOPPED", "ERROR"): raise Conflict("请先停止并核实当前环境，再同步运行配置")
    profile, agent = latest(connection, requirement_id)
    if not profile: raise Conflict("项目尚未保存运行配置，请先在项目登记中配置")
    return {"profile": profile, "agent_cli": agent}


def completed_job(connection, requirement_id):
    return connection.execute(select(tables.jobs.c.id).where(tables.jobs.c.requirement_id == requirement_id,
        tables.jobs.c.action == "sync_profile", tables.jobs.c.status == "SUCCEEDED").order_by(tables.jobs.c.created_at.desc()).limit(1)).scalar_one_or_none()


def finish(connection, job, status, result):
    if status != "SUCCEEDED": return
    target = json.loads(job["input_json"])["sync_target"]
    recorded = result.get("profile_sync") or {}
    profile, agent = target["profile"], target["agent_cli"]
    if recorded != {"job_id": job["id"], "profile_id": profile["id"], "profile_digest": profile["content_digest"],
        "agent_cli_id": agent["id"], "agent_cli_digest": agent["content_digest"]}:
        raise Conflict("配置同步结果与入队目标不一致，请停止并核实")
    for table, column, value in ((tables.workspace_profiles, "profile_id", profile["id"]),
        (tables.workspace_agents, "config_id", None if agent["id"] == agent_clis.BUILTIN else agent["id"])):
        found = connection.execute(select(table.c.requirement_id).where(table.c.requirement_id == job["requirement_id"])).first()
        if found: connection.execute(update(table).where(table.c.requirement_id == job["requirement_id"]).values(**{column: value}))
        else: connection.execute(insert(table).values(requirement_id=job["requirement_id"], **{column: value}))
    from .workflow import invalidate
    from .materials import invalidate as invalidate_materials
    invalidate(connection, job["requirement_id"])
    invalidate_materials(connection, job["requirement_id"])
    connection.execute(update(tables.workspaces).where(tables.workspaces.c.requirement_id == job["requirement_id"]).values(approval="STALE"))


def record(workspace):
    path = safe_path(workspace, JOURNAL)
    if not path.exists(): return None
    if path.stat().st_size > 32 * 1024 * 1024: raise WorkbenchError("配置同步恢复记录超过限制")
    value = json.loads(path.read_text(encoding="utf-8"))
    name = value.get("stage", "")
    if value.get("schema") != "workbench-profile-sync/v1" or not name.startswith(".profile-sync-") or "/" in name or "\\" in name:
        raise WorkbenchError("配置同步恢复记录无效，请核实工作区")
    return value


def _remove(path):
    if is_link(path): raise WorkbenchError("配置同步目录不能是链接")
    if path.exists(): shutil.rmtree(path)


def resolve(workspace, committed_job_id=None, *, rollback=False, expected_job_id=None):
    """The acknowledged job id distinguishes same-version image refreshes too."""
    value = record(workspace)
    if not value: return
    if expected_job_id is not None and value.get("job_id") != expected_job_id: return
    stage = safe_path(workspace, value["stage"])
    committed = value.get("job_id") == committed_job_id
    if not committed and not rollback:
        from .execution import Unknown
        raise Unknown("配置同步结果尚未确认，请先停止并核实，再重新同步")
    if not stage.is_dir():
        if committed or json.loads(safe_path(workspace, "workspace.json").read_text(encoding="utf-8")) == value["before"]:
            safe_path(workspace, JOURNAL).unlink()
            return
        raise WorkbenchError("配置同步恢复副本缺失，请核实工作区")
    if not committed:
        backup = safe_path(stage, "previous-runtime-config")
        current = safe_path(workspace, "runtime-config")
        if backup.exists():
            _remove(current)
            retry_file_access(lambda: backup.replace(current))
        elif not value["had_config"]:
            _remove(current)
        write_json(safe_path(workspace, "workspace.json"), value["before"], private=True)
        for name in REPORTS:
            saved = safe_path(stage, "previous-" + name)
            if saved.exists(): retry_file_access(lambda: saved.replace(safe_path(workspace, name)))
            elif name not in value["reports"]: safe_path(workspace, name).unlink(missing_ok=True)
    _remove(stage)
    safe_path(workspace, JOURNAL).unlink()


def verify_stopped(executor, workspace, target):
    from . import dependencies, resources
    for name in ("runtime.json", "application-runtime.json", dependencies.JOURNAL):
        if safe_path(workspace, name).exists(): raise WorkbenchError("请先停止并核实开发环境、应用和数据库，再同步运行配置")
    if any(item.get("workspace") == str(workspace) for item in resources.inventory(executor.settings.data_dir)):
        raise WorkbenchError("当前工作区仍有容器，请先停止并核实后再同步")
    data = dependencies.read(workspace, dependencies.STATE)
    if data:
        if data["phase"] not in ("STOPPED", "RESTORED", "NOT_CREATED"): raise WorkbenchError("数据库状态尚未核实，请先停止并核实后同步")
        if data.get("initialized"): dependencies.snapshot_info(workspace, required=True)


def execute(executor, job, workspace, emit, canceled):
    from .execution import Canceled
    _, before = load_workspace(workspace)
    target = job["input"]["sync_target"]
    verify_stopped(executor, workspace, target)
    stage = Path(tempfile.mkdtemp(prefix=".profile-sync-", dir=workspace))
    try:
        draft = {key: value for key, value in before.items() if key not in ("runtime_profile", "agent_cli")}
        write_json(stage / "workspace.json", draft, private=True)
        emit("stage", {"text": "正在核实最新配置的镜像、挂载和 Agent CLI；已有代码与会话保留"})
        profiles.configure(stage, target["profile"], executor.settings.data_dir)
        agent_clis.configure(stage, target["agent_cli"])
        profiles.runtime_options(stage, executor.settings.data_dir)
        after = json.loads((stage / "workspace.json").read_text(encoding="utf-8"))
        # A directory is installed even for an empty mount set, so rollback is uniform.
        (stage / "runtime-config").mkdir(exist_ok=True)
        reports = []
        for name in REPORTS:
            path = safe_path(workspace, name)
            if path.exists():
                shutil.copyfile(path, stage / ("previous-" + name)); reports.append(name)
        if canceled.is_set(): raise Canceled("配置同步已取消，原配置保留")
        value = {"schema": "workbench-profile-sync/v1", "job_id": job["id"], "stage": stage.name, "before": before,
            "had_config": safe_path(workspace, "runtime-config").exists(), "reports": reports}
        write_json(workspace / JOURNAL, value, private=True)
        if value["had_config"]:
            retry_file_access(lambda: safe_path(workspace, "runtime-config").replace(stage / "previous-runtime-config"))
        retry_file_access(lambda: (stage / "runtime-config").replace(workspace / "runtime-config"))
        write_json(workspace / "workspace.json", after, private=True)
        for name in REPORTS: safe_path(workspace, name).unlink(missing_ok=True)
        if canceled.is_set(): raise Canceled("配置同步已取消，原配置保留")
        result = executor.observe(workspace, "STOPPED")
        result["profile_sync"] = {"job_id": job["id"], "profile_id": target["profile"]["id"],
            "profile_digest": target["profile"]["content_digest"], "agent_cli_id": target["agent_cli"]["id"],
            "agent_cli_digest": target["agent_cli"]["content_digest"]}
        return result
    except Exception:
        if (workspace / JOURNAL).exists(): resolve(workspace, rollback=True)
        else: _remove(stage)
        raise
