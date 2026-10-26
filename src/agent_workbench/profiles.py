"""Human-configured runtime profiles; no file contents in API or backup metadata."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, insert, select, update

from .platform_support import is_link
from . import runtime
from .checked_archive import file_hash
from .store import (Conflict, Missing, projects, requirements, workspaces, project_profiles as profiles,
    workspace_profiles as bindings, encoded, identifier, public)
from .workspace import WorkbenchError, safe_path, write_json


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


def command(value):
    if not value.strip(): return []
    if any(char in value for char in "\n\r\x00|;&<>"): raise ValueError("命令不支持管道、重定向或多条命令；可使用仓库内脚本")
    argv = shlex.split(value)
    if len(argv) > 64 or any(len(item) > 2000 for item in argv): raise ValueError("命令参数超过限制")
    if not argv or argv[0].startswith("-"): raise ValueError("命令入口不正确")
    return argv


class Mount(Input):
    source: str = Field(min_length=1, max_length=2000)
    target: str = Field(min_length=1, max_length=240)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("target")
    @classmethod
    def target_path(cls, value):
        path = PurePosixPath(value)
        if not value.startswith("/run/workbench-config/") or ".." in path.parts or value != str(path) or any(char in value for char in ",\n\r\x00"):
            raise ValueError("挂载目标必须位于 /run/workbench-config/，且不能包含特殊分隔符")
        return value


class RepositoryCommand(Input):
    path: str = Field(min_length=1, max_length=240)
    build_command: str = Field(default="", max_length=2000)
    verification_command: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def checked(self):
        from .multi_repo import relative_names
        try: relative_names([self.path])
        except WorkbenchError as error: raise ValueError(str(error)) from None
        command(self.build_command); command(self.verification_command)
        return self


class Content(Input):
    agent_cli_id: str | None = Field(default=None, max_length=40)
    image: str = Field(default="agent-workbench-m1:local", max_length=240, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$")
    cpus: float = Field(default=1, ge=0.25, le=16, allow_inf_nan=False)
    memory_mib: int = Field(default=1536, ge=256, le=32768)
    pids_limit: int = Field(default=256, ge=64, le=2048)
    build_command: str = Field(default="", max_length=2000)
    verification_command: str = Field(default="node --test", max_length=2000)
    repository_commands: list[RepositoryCommand] = Field(default_factory=list, max_length=16)
    mounts: list[Mount] = Field(default_factory=list, max_length=8)
    dependencies: list["Postgres"] = Field(default_factory=list, max_length=1)
    application: "Application | None" = None

    @model_validator(mode="after")
    def validate_content(self):
        command(self.build_command)
        if not command(self.verification_command) and not any(item.verification_command.strip() for item in self.repository_commands): raise ValueError("请填写验证命令")
        paths = [item.path for item in self.repository_commands]
        if len(set(paths)) != len(paths): raise ValueError("仓库命令不能重复")
        targets = [item.target for item in self.mounts]
        if len(set(targets)) != len(targets) or any(a != b and b.startswith(a + "/") for a in targets for b in targets):
            raise ValueError("配置挂载目标不能重复或互相覆盖")
        return self


class Postgres(Input):
    kind: Literal["postgres"] = "postgres"
    image: Literal["postgres:17-bookworm"] = "postgres:17-bookworm"
    cpus: float = Field(default=0.5, ge=0.25, le=4, allow_inf_nan=False)
    memory_mib: int = Field(default=512, ge=256, le=4096)
    pids_limit: int = Field(default=128, ge=64, le=512)


class Application(Input):
    start_command: str = Field(min_length=1, max_length=2000)
    port: int = Field(default=3000, ge=1024, le=65535)
    health_path: str = Field(default="/health", max_length=500, pattern=r"^/[A-Za-z0-9/_~.%-]*$")
    expected_status: int = Field(default=200, ge=200, le=299)
    ready_timeout_seconds: int = Field(default=30, ge=1, le=60)

    @field_validator("start_command")
    @classmethod
    def start_argv(cls, value):
        if not command(value): raise ValueError("请填写服务启动命令")
        return value


Content.model_rebuild()


class Save(Input):
    expected_profile: str | None = None
    content: Content
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("actor", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("登记人和原因不能为空")
        return value.strip()


def digest(content): return hashlib.sha256(encoded(content).encode()).hexdigest()


def checked_source(raw, data_dir):
    path = Path(raw).expanduser()
    if not path.is_absolute() or any(char in str(path) for char in ",\n\r\x00"):
        raise WorkbenchError("配置文件必须使用绝对路径，且不能包含特殊分隔符")
    current = path
    while True:
        if is_link(current):
            raise WorkbenchError("只允许普通配置文件，不支持符号链接、junction 和重解析点")
        if current.parent == current:
            break
        current = current.parent
    path = path.resolve()
    if not path.is_file() or path.is_relative_to(data_dir.resolve()):
        raise WorkbenchError("只允许工作台数据目录之外的普通配置文件，不支持符号链接和目录挂载")
    if path.stat().st_size > 1024 * 1024: raise WorkbenchError("单个配置文件不能超过 1 MiB")
    return path


def binding(connection, requirement_id):
    return public(connection.execute(select(profiles).join(bindings, bindings.c.profile_id == profiles.c.id)
        .where(bindings.c.requirement_id == requirement_id)).mappings().first())


def pin(connection, requirement_id):
    if binding(connection, requirement_id): return
    workspace = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)).mappings().one()
    if workspace["snapshot_json"]: return  # Existing workspaces keep their legacy contract.
    project_id = connection.execute(select(requirements.c.project_id).where(requirements.c.id == requirement_id)).scalar_one()
    connection.execute(select(projects.c.id).where(projects.c.id == project_id).with_for_update()).one()
    profile = connection.execute(select(profiles).where(and_(profiles.c.project_id == project_id, profiles.c.is_current.is_(True)))).mappings().first()
    if profile: connection.execute(insert(bindings).values(requirement_id=requirement_id, profile_id=profile["id"]))


class Profiles:
    def __init__(self, store, data_dir): self.store, self.data_dir = store, data_dir

    def read(self, project_id):
        self.store.get(projects, project_id)
        history = self.store.list(profiles, profiles.c.project_id == project_id)
        history.sort(key=lambda item: item["number"], reverse=True)
        return {"current": next((item for item in history if item["is_current"]), None), "history": history}

    def save(self, project_id, value):
        content = value["content"]
        for item in content["mounts"]:
            path = checked_source(item["source"], self.data_dir)
            item["sha256"] = file_hash(path)
        with self.store.engine.begin() as connection:
            if content.get("agent_cli_id"):
                from .agent_clis import get
                get(connection, content["agent_cli_id"])
            if not connection.execute(select(projects.c.id).where(projects.c.id == project_id).with_for_update()).first(): raise Missing("项目不存在")
            repositories = json.loads(connection.execute(select(projects.c.repositories_json).where(projects.c.id == project_id)).scalar_one())
            if any(item["path"] not in repositories for item in content.get("repository_commands", [])):
                raise Conflict("仓库命令必须对应本业务项目已登记的仓库")
            previous = connection.execute(select(profiles).where(and_(profiles.c.project_id == project_id, profiles.c.is_current.is_(True)))).mappings().first()
            if value["expected_profile"] != (previous["id"] if previous else None): raise Conflict("项目配置已经变化，请重新载入")
            connection.execute(update(profiles).where(profiles.c.project_id == project_id).values(is_current=False))
            row = dict(id=identifier("prf"), project_id=project_id, number=previous["number"] + 1 if previous else 1, is_current=True,
                content_json=encoded(content), content_digest=digest(content), actor=value["actor"], reason=value["reason"], created_at=time.time())
            connection.execute(insert(profiles).values(**row)); return public(row)


def repository_plans(content, selected, kind, *, require_all=True):
    configured = {item['path']: item for item in content.get('repository_commands', [])}
    result = []
    for path in selected:
        argv = command(configured.get(path, {}).get('build_command' if kind == 'build' else 'verification_command', ''))
        if not argv:
            if require_all: raise WorkbenchError(f'仓库 {path} 尚未配置验证命令，请修订项目运行配置后创建新需求工作区')
            continue
        result.append({'path': path, 'argv': argv})
    return result


def staged_mounts(workspace, profile, data_dir, source_workspace=None):
    result = []
    for index, mount in enumerate(profile["content"]["mounts"]):
        target = safe_path(workspace, f"runtime-config/{index}.config")
        if not target.exists():
            source = safe_path(source_workspace, f"runtime-config/{index}.config") if source_workspace else checked_source(mount["source"], data_dir)
            if is_link(source) or file_hash(source) != mount["sha256"]: raise WorkbenchError("配置文件与固定版本不一致，请还原文件或创建新版本和工作区")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as output: output.write(source.read_bytes())
                os.link(temporary, target)
            finally: Path(temporary).unlink(missing_ok=True)
        if is_link(target) or file_hash(target) != mount["sha256"]: raise WorkbenchError("工作区配置副本摘要不符")
        result.append({"source": str(target), "target": mount["target"]})
    return result


def configure(workspace, profile, data_dir):
    if not profile: return
    manifest = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    fixed = manifest.get("runtime_profile")
    if fixed:
        if fixed["id"] != profile["id"] or fixed["content_digest"] != profile["content_digest"]: raise WorkbenchError("工作区运行配置与固定版本不一致")
    else:
        inspected = json.loads(runtime.docker("image", "inspect", profile["content"]["image"]).stdout)[0]
        fixed = {key: profile[key] for key in ("id", "number", "content", "content_digest")}
        fixed["image_id"] = inspected["Id"]
        fixed["dependency_images"] = {item["kind"]: json.loads(runtime.docker("image", "inspect", item["image"]).stdout)[0]["Id"]
            for item in profile["content"].get("dependencies", [])}
        manifest["runtime_profile"] = fixed
        write_json(workspace / "workspace.json", manifest)
    staged_mounts(workspace, fixed, data_dir)


def inherit(source, target, data_dir):
    from .agent_clis import inherit as inherit_agent
    inherit_agent(source, target)
    manifest = json.loads((source / "workspace.json").read_text(encoding="utf-8"))
    if manifest.get("runtime_profile"):
        fixed = manifest["runtime_profile"]
        staged_mounts(target, fixed, data_dir, source)
        copied = json.loads((target / "workspace.json").read_text(encoding="utf-8")); copied["runtime_profile"] = fixed
        write_json(target / "workspace.json", copied)


def runtime_options(workspace, data_dir):
    manifest = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    profile = manifest.get("runtime_profile")
    agent_options = {"agent_cli": manifest["agent_cli"]} if manifest.get("agent_cli") else {}
    if not profile: return agent_options
    if digest(profile["content"]) != profile["content_digest"]: raise WorkbenchError("运行配置摘要不符")
    Content.model_validate(profile["content"])
    return {**agent_options, "image": profile["image_id"], "limits": {key: profile["content"][key] for key in ("cpus", "memory_mib", "pids_limit")},
        "config_mounts": staged_mounts(workspace, profile, data_dir)}
