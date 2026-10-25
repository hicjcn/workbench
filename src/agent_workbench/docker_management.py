"""Image metadata and maintenance intent; Docker is only called by the Runner."""
import json
import re
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, insert, select, update

from . import store as t
from .store import Conflict, Missing, encoded, identifier, public


def validate_reference(value):
    # Explicit tags or digests only, never Docker options, URLs or implicit latest.
    if not re.fullmatch(r"(?:[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?/)*[a-z0-9][a-z0-9._-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})", value):
        raise ValueError("请填写带明确标签或 sha256 摘要的镜像引用，例如 node:22-bookworm-slim")
    return value


class RecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_recipe: str | None = None
    name: str = Field(min_length=1, max_length=160)
    reference: str = Field(min_length=1, max_length=240)
    dockerfile: str = Field(min_length=1, max_length=65536)
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)

    _reference = field_validator("reference")(validate_reference)

    @field_validator("name", "dockerfile", "actor", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("内容不能为空")
        return value.strip()


class ImageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(pull|build|remove)$")
    reference: str = Field(max_length=240)
    recipe_id: str | None = None
    expected_image_id: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)
    _reference = field_validator("reference")(validate_reference)

    @field_validator("actor", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("登记人员及原因不能为空")
        return value.strip()


class Acknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=4000)
    checked_daemon: bool


class Images:
    def __init__(self, store): self.store = store

    def expire(self):
        with self.store.engine.begin() as c:
            c.execute(update(t.docker_jobs).where(and_(t.docker_jobs.c.status == "RUNNING", t.docker_jobs.c.lease_until < time.time())).values(
                status="UNKNOWN", error="镜像操作心跳中断；不会自动重放，请核实 Docker 后人工结束核实", updated_at=time.time()))

    def recipes(self):
        rows = self.store.list(t.docker_recipes)
        return [{**row, "current": row["number"] == max(other["number"] for other in rows if other["family"] == row["family"])} for row in rows]

    def save(self, data):
        with self.store.engine.begin() as c:
            previous = None
            if data["expected_recipe"]:
                previous = c.execute(select(t.docker_recipes).where(t.docker_recipes.c.id == data["expected_recipe"]).with_for_update()).mappings().first()
                if not previous: raise Missing("Dockerfile 版本不存在")
                newest = c.execute(select(t.docker_recipes.c.number).where(t.docker_recipes.c.family == previous["family"]).order_by(t.docker_recipes.c.number.desc()).limit(1)).scalar_one()
                if newest != previous["number"]: raise Conflict("Dockerfile 已更新，请重新打开当前版本")
            value = {k: v for k, v in data.items() if k != "expected_recipe"}
            value.update(id=identifier("dkr"), family=previous["family"] if previous else identifier("img"), number=previous["number"] + 1 if previous else 1, created_at=time.time())
            c.execute(insert(t.docker_recipes).values(**value))
        return value

    def enqueue(self, data, key):
        if not key or not key.strip(): raise Conflict("镜像操作必须提供请求标识")
        self.expire()
        with self.store.engine.begin() as c:
            # Existing shared guard serializes maintenance submissions and claims on PostgreSQL too.
            guard(c, self.store)
            previous = c.execute(select(t.docker_jobs).where(t.docker_jobs.c.idempotency_key == key)).mappings().first()
            if previous:
                if previous["input_json"] != encoded(data): raise Conflict("同一请求标识对应不同操作")
                return public(previous)
            if c.execute(select(t.docker_jobs.c.id).where(t.docker_jobs.c.status.in_(["QUEUED", "RUNNING", "UNKNOWN"]))).first():
                raise Conflict("已有镜像维护任务执行或待核实，请先处理")
            if data["kind"] == "build":
                recipe = c.execute(select(t.docker_recipes).where(t.docker_recipes.c.id == data["recipe_id"])).mappings().first()
                if not recipe or recipe["reference"] != data["reference"]: raise Conflict("构建必须绑定匹配的 Dockerfile 固定版本")
                if "@" in data["reference"]: raise Conflict("构建目标需要镜像标签")
            if data["kind"] == "remove" and not data["expected_image_id"]: raise Conflict("移除镜像需绑定当前完整镜像 ID")
            value = dict(id=identifier("dkj"), kind=data["kind"], input_json=encoded(data), status="QUEUED", idempotency_key=key,
                owner=None, epoch=0, lease_until=None, result_json=None, error=None, created_at=time.time(), updated_at=time.time())
            c.execute(insert(t.docker_jobs).values(**value))
            return public(value)

    def claim(self, owner):
        self.expire()
        with self.store.engine.begin() as c:
            guard(c, self.store)
            if c.execute(select(t.docker_jobs.c.id).where(t.docker_jobs.c.status.in_(["RUNNING", "UNKNOWN"]))).first(): return None
            from sqlalchemy import func
            if c.execute(select(func.count()).select_from(t.jobs).where(t.jobs.c.status == "RUNNING")).scalar_one() >= self.store.max_active_jobs: return None
            row = c.execute(select(t.docker_jobs).where(t.docker_jobs.c.status == "QUEUED").order_by(t.docker_jobs.c.created_at).with_for_update().limit(1)).mappings().first()
            if not row: return None
            values = dict(status="RUNNING", owner=owner, epoch=row["epoch"] + 1, lease_until=time.time() + self.store.lease_seconds, updated_at=time.time())
            c.execute(update(t.docker_jobs).where(t.docker_jobs.c.id == row["id"]).values(**values))
            result = public(dict(row, **values))
            if result["kind"] == "build": result["recipe"] = public(c.execute(select(t.docker_recipes).where(t.docker_recipes.c.id == result["input"]["recipe_id"])).mappings().one())
            result["protected_references"] = protected(c)
            return result

    def owned(self, c, identity, owner, epoch):
        row = c.execute(select(t.docker_jobs).where(t.docker_jobs.c.id == identity).with_for_update()).mappings().first()
        if not row or row["status"] != "RUNNING" or row["owner"] != owner or row["epoch"] != epoch or row["lease_until"] < time.time(): raise Conflict("镜像操作租约已失效，禁止覆盖或重放")
        return row

    def heartbeat(self, identity, owner, epoch):
        with self.store.engine.begin() as c:
            self.owned(c, identity, owner, epoch)
            c.execute(update(t.docker_jobs).where(t.docker_jobs.c.id == identity).values(lease_until=time.time() + self.store.lease_seconds))
        return {"accepted": True}

    def finish(self, identity, owner, epoch, status, result, error):
        with self.store.engine.begin() as c:
            self.owned(c, identity, owner, epoch)
            c.execute(update(t.docker_jobs).where(t.docker_jobs.c.id == identity).values(status=status, result_json=encoded(result), error=error, lease_until=None, updated_at=time.time()))
        return {"accepted": True}

    def cancel(self, identity):
        with self.store.engine.begin() as c:
            row = c.execute(select(t.docker_jobs).where(t.docker_jobs.c.id == identity).with_for_update()).mappings().first()
            if not row: raise Missing("镜像任务不存在")
            if row["status"] != "QUEUED": raise Conflict("只能取消尚未执行的镜像任务；执行中的操作需等待完成或核实")
            c.execute(update(t.docker_jobs).where(t.docker_jobs.c.id == identity).values(status="CANCELED", updated_at=time.time()))
        return self.store.get(t.docker_jobs, identity)


def guard(c, store):
    if store.engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as upsert
    else:
        from sqlalchemy.dialects.sqlite import insert as upsert
    c.execute(upsert(t.runtime_guard).values(id=1).on_conflict_do_nothing())
    c.execute(select(t.runtime_guard).where(t.runtime_guard.c.id == 1).with_for_update()).one()


def protected(c):
    values = set(c.execute(select(t.projects.c.image)).scalars())
    for content in c.execute(select(t.project_profiles.c.content_json)).scalars():
        profile = json.loads(content); values.add(profile["image"])
        values.update(dep["image"] for dep in profile.get("dependencies", []))
    return sorted(values)
