"""Versioned OpenCode-compatible wrappers. Commands run exclusively in Docker."""
import hashlib
import json
import re
import shlex
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import insert, select

from . import store as t
from .store import Conflict, Missing, encoded, identifier, public
from .workspace import WorkbenchError, write_json

BUILTIN = "builtin-opencode"
RESERVED = ("AWB_", "DOCKER_", "LD_", "PYTHON", "OPENCODE_SERVER_")


def argv(value, *, placeholders=()):
    if not isinstance(value, str) or any(char in value for char in "\n\r\x00|;&<>"):
        raise ValueError("命令不支持 Shell、换行、管道和重定向；可调用镜像内的包装脚本")
    try: parts = shlex.split(value)
    except ValueError: raise ValueError("命令引号不完整") from None
    if not parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", parts[0]) or parts[0].startswith("-") or len(parts) > 64 or any(len(part) > 2000 for part in parts):
        raise ValueError("命令入口或参数不正确")
    for part in parts:
        remaining = part
        for name in placeholders: remaining = remaining.replace("{" + name + "}", "")
        if "{" in remaining or "}" in remaining: raise ValueError("命令包含不支持的占位符")
    return parts


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["opencode"] = "opencode"
    serve_command: str = Field(default="opencode serve --hostname {host} --port {port}", min_length=1, max_length=4000)
    run_command: str = Field(default="opencode run --attach {endpoint} --model {model} --format json", min_length=1, max_length=4000)
    version_command: str = Field(default="opencode --version", min_length=1, max_length=2000)
    list_models_command: str | None = Field(default=None, max_length=4000)
    expected_version: str = Field(default="", max_length=200)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    credential_env: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def commands(self):
        argv(self.serve_command, placeholders=("host", "port"))
        argv(self.run_command, placeholders=("endpoint", "model"))
        argv(self.version_command)
        if self.list_models_command is not None:
            argv(self.list_models_command)
        if "{model}" not in self.run_command or "{endpoint}" not in self.run_command:
            raise ValueError("任务命令必须显式传递 {model} 和 {endpoint}，提示词固定通过标准输入传递")
        return self

    @field_validator("credential_env")
    @classmethod
    def safe_environment(cls, values):
        if len(set(values)) != len(values): raise ValueError("凭据变量名不能重复")
        for name in values:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name) or name.startswith(RESERVED) or not (name.endswith(("_API_KEY", "_TOKEN", "_BASE_URL")) or name.startswith("AGENT_")):
                raise ValueError("只允许 API_KEY、TOKEN、BASE_URL 或 AGENT_ 开头的模型/封装配置变量名，不能传递系统及工作台变量")
        return values


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_config: str | None = None
    name: str = Field(min_length=1, max_length=160)
    content: Content
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)
    set_default: bool = False

    @field_validator("name", "actor", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("名称、登记人和原因不能为空")
        return value.strip()


def digest(content): return hashlib.sha256(encoded(content).encode()).hexdigest()


def builtin():
    # The built-in v1 identity predates model discovery; preserve its digest so
    # prepared workspaces keep their immutable binding after an upgrade.
    content = Content(expected_version="1.19.4").model_dump(exclude={"list_models_command"})
    return {"id": BUILTIN, "family": BUILTIN, "number": 1, "name": "OpenCode（内置）", "content": content,
        "content_digest": digest(content), "actor": "系统", "reason": "保留既有 OpenCode 行为", "created_at": 0, "builtin": True}


def get(connection, identity):
    if not identity or identity == BUILTIN: return builtin()
    row = connection.execute(select(t.agent_cli_configs).where(t.agent_cli_configs.c.id == identity)).mappings().first()
    if not row: raise Missing("Agent CLI 配置版本不存在")
    return public(row)


def default(connection):
    return get(connection, connection.execute(select(t.agent_cli_default.c.config_id).where(t.agent_cli_default.c.id == 1)).scalar_one_or_none())


def selection(connection, requirement_id):
    ws = connection.execute(select(t.workspaces).where(t.workspaces.c.requirement_id == requirement_id)).mappings().first()
    if not ws: raise Missing("需求工作区不存在")
    bound = connection.execute(select(t.workspace_agents).where(t.workspace_agents.c.requirement_id == requirement_id)).mappings().first()
    if bound: return {**get(connection, bound["config_id"]), "fixed": True}
    if ws["snapshot_json"]: return {**builtin(), "fixed": True}  # Never rebind legacy workspaces.
    from .profiles import binding
    profile = binding(connection, requirement_id)
    if not profile:
        project_id = connection.execute(select(t.requirements.c.project_id).where(t.requirements.c.id == requirement_id)).scalar_one()
        row = connection.execute(select(t.project_profiles).where((t.project_profiles.c.project_id == project_id) & t.project_profiles.c.is_current.is_(True))).mappings().first()
        profile = public(row)
    chosen = profile and profile["content"].get("agent_cli_id")
    return {**(get(connection, chosen) if chosen else default(connection)), "fixed": False}


def pin(connection, requirement_id):
    if connection.execute(select(t.workspace_agents).where(t.workspace_agents.c.requirement_id == requirement_id)).first(): return
    chosen = selection(connection, requirement_id)
    if chosen["fixed"]: return
    connection.execute(insert(t.workspace_agents).values(requirement_id=requirement_id, config_id=None if chosen["id"] == BUILTIN else chosen["id"]))


def configure(workspace, selected):
    if not selected: return
    checked(selected)
    fixed = {key: selected[key] for key in ("id", "name", "number", "content", "content_digest")}
    path = workspace / "workspace.json"; manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("agent_cli") and manifest["agent_cli"] != fixed: raise WorkbenchError("工作区 Agent CLI 已固定，不能静默切换")
    manifest["agent_cli"] = fixed; write_json(path, manifest)


def inherit(source, target):
    source_manifest = json.loads((source / "workspace.json").read_text(encoding="utf-8"))
    if source_manifest.get("agent_cli"):
        path = target / "workspace.json"; manifest = json.loads(path.read_text(encoding="utf-8")); manifest["agent_cli"] = source_manifest["agent_cli"]; write_json(path, manifest)


def verify_workspace(workspace, selected=None):
    selected = selected or builtin()
    configured = json.loads((workspace / "workspace.json").read_text(encoding="utf-8")).get("agent_cli")
    if not configured:
        if selected["id"] != BUILTIN: raise WorkbenchError("工作区尚未固定所选 Agent CLI，请重新准备工作区")
        return
    checked(configured)
    if configured["id"] != selected["id"] or configured["content_digest"] != selected["content_digest"]:
        raise WorkbenchError("工作区 Agent CLI 与控制服务固定版本不一致，禁止启动或传递凭据")


def checked(selected):
    selected = selected or builtin()
    content = Content.model_validate(selected["content"]).model_dump()
    # Validate the stored document, before applying defaults added in newer code.
    if digest(selected["content"]) != selected["content_digest"]: raise WorkbenchError("Agent CLI 配置摘要不符")
    return content


def command(selected, kind, *, model=None):
    content = checked(selected)
    allowed = {"serve": ("host", "port"), "run": ("endpoint", "model"), "version": (), "list_models": ()}[kind]
    value = content[kind + "_command"]
    if kind == "list_models" and value is None:
        value = shlex.join([argv(content["version_command"])[0], "models"])
    parts = argv(value, placeholders=allowed)
    replacements = {"host": "0.0.0.0", "port": "4096", "endpoint": "http://127.0.0.1:4096", "model": model or ""}
    return [replace(part, replacements, allowed) for part in parts]


def replace(part, values, names):
    for name in names: part = part.replace("{" + name + "}", values[name])
    return part


class Configurations:
    def __init__(self, store): self.store = store

    def read(self):
        with self.store.engine.connect() as c: selected = default(c)
        rows = self.store.list(t.agent_cli_configs)
        rows = [{**row, "builtin": False, "current": not any(other["family"] == row["family"] and other["number"] > row["number"] for other in rows)} for row in rows]
        return {"default_id": selected["id"], "configs": [{**builtin(), "current": True}, *rows]}

    def activate(self, identity, connection=None):
        if connection is None:
            with self.store.engine.begin() as c: return self.activate(identity, c)
        from .docker_management import guard
        guard(connection, self.store); get(connection, identity)
        if self.store.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as upsert
        else:
            from sqlalchemy.dialects.sqlite import insert as upsert
        value = {"id": 1, "config_id": None if identity == BUILTIN else identity}
        connection.execute(upsert(t.agent_cli_default).values(**value).on_conflict_do_update(index_elements=[t.agent_cli_default.c.id], set_=value))
        return {"default_id": identity, "applies_to": "newly_prepared_workspaces"}

    def save(self, data):
        with self.store.engine.begin() as c:
            previous = get(c, data["expected_config"]) if data["expected_config"] else None
            if previous and not previous.get("builtin"):
                c.execute(select(t.agent_cli_configs).where(t.agent_cli_configs.c.id == previous["id"]).with_for_update()).one()
                latest = c.execute(select(t.agent_cli_configs.c.number).where(t.agent_cli_configs.c.family == previous["family"]).order_by(t.agent_cli_configs.c.number.desc()).limit(1)).scalar_one()
                if latest != previous["number"]: raise Conflict("Agent CLI 配置已更新，请重新打开当前版本")
            clone = not previous or previous.get("builtin")
            row = dict(id=identifier("acl"), family=identifier("acf") if clone else previous["family"], number=1 if clone else previous["number"] + 1,
                name=data["name"], content_json=encoded(data["content"]), content_digest=digest(data["content"]), actor=data["actor"], reason=data["reason"], created_at=time.time())
            c.execute(insert(t.agent_cli_configs).values(**row))
            if data["set_default"]: self.activate(row["id"], c)
            return public(row)
