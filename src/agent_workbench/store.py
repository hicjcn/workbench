"""Persistent control plane. External processes are never replayed after lease loss."""
import json
import time
import uuid

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Index, Integer, MetaData,
                        String, Table, Text, UniqueConstraint, and_, create_engine,
                        event, insert, select, update)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

metadata = MetaData()
projects = Table("projects", metadata,
    Column("id", String(40), primary_key=True), Column("name", String(160), nullable=False),
    Column("source_path", Text, nullable=False), Column("image", String(240), nullable=False),
    Column("created_at", Float, nullable=False))
requirements = Table("requirements", metadata,
    Column("id", String(40), primary_key=True), Column("project_id", ForeignKey("projects.id"), nullable=False),
    Column("title", String(200), nullable=False), Column("description", Text, nullable=False),
    Column("acceptance", Text, nullable=False), Column("created_at", Float, nullable=False))
workspaces = Table("workspaces", metadata,
    Column("requirement_id", ForeignKey("requirements.id"), primary_key=True),
    Column("path", Text, nullable=False), Column("environment", String(30), nullable=False),
    Column("diff_truncated", Boolean, nullable=False, default=False),
    Column("snapshot_json", Text), Column("diff", Text), Column("approval", String(30), nullable=False),
    Column("updated_at", Float, nullable=False))
jobs = Table("jobs", metadata,
    Column("id", String(40), primary_key=True), Column("requirement_id", ForeignKey("requirements.id"), nullable=False),
    Column("action", String(30), nullable=False), Column("input_json", Text, nullable=False),
    Column("status", String(30), nullable=False), Column("idempotency_key", String(100)),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("owner", String(80)), Column("epoch", Integer, nullable=False, default=0),
    Column("lease_until", Float), Column("result_json", Text), Column("error", Text),
    Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
    UniqueConstraint("requirement_id", "idempotency_key", name="uq_job_idempotency"))
active = jobs.c.status.in_(["QUEUED", "RUNNING"])
Index("uq_active_requirement", jobs.c.requirement_id, unique=True, sqlite_where=active, postgresql_where=active)
events = Table("execution_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", ForeignKey("jobs.id"), nullable=False), Column("seq", Integer, nullable=False),
    Column("kind", String(60), nullable=False), Column("payload_json", Text, nullable=False),
    Column("created_at", Float, nullable=False), UniqueConstraint("job_id", "seq", name="uq_event_sequence"))
reviews = Table("reviews", metadata,
    Column("id", String(40), primary_key=True), Column("requirement_id", ForeignKey("requirements.id"), nullable=False),
    Column("digest", String(64), nullable=False), Column("decision", String(30), nullable=False),
    Column("reason", Text, nullable=False), Column("created_at", Float, nullable=False))


def identifier(prefix):
    return prefix + "_" + uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def public(row):
    if row is None:
        return None
    value = dict(row)
    for name in list(value):
        if name.endswith("_json"):
            content = value.pop(name)
            value[name[:-5]] = json.loads(content) if content else None
    return value


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class Store:
    def __init__(self, url, lease_seconds=45):
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        self.lease_seconds = lease_seconds
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def configure(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

    def get(self, table, identity, exclude=()):
        key = list(table.primary_key.columns)[0]
        with self.engine.connect() as connection:
            row = connection.execute(select(*(column for column in table.c if column.name not in exclude)).where(key == identity)).mappings().first()
        if row is None:
            raise Missing("记录不存在")
        return public(row)

    def list(self, table, predicate=None, exclude=()):
        query = select(*(column for column in table.c if column.name not in exclude))
        if predicate is not None:
            query = query.where(predicate)
        if "created_at" in table.c:
            query = query.order_by(table.c.created_at.desc())
        with self.engine.connect() as connection:
            return [public(row) for row in connection.execute(query).mappings()]

    def expire(self):
        now = time.time()
        with self.engine.begin() as connection:
            expired = connection.execute(select(jobs.c.id, jobs.c.requirement_id).where(
                and_(jobs.c.status == "RUNNING", jobs.c.lease_until < now))).all()
            for job_id, requirement_id in expired:
                changed = connection.execute(update(jobs).where(and_(jobs.c.id == job_id,
                    jobs.c.status == "RUNNING", jobs.c.lease_until < now)).values(
                        status="UNKNOWN", error="执行器心跳中断；需要停止并核实，任务不会自动重放", updated_at=now))
                if changed.rowcount:
                    connection.execute(update(workspaces).where(workspaces.c.requirement_id == requirement_id)
                        .values(environment="UNKNOWN", approval="STALE", updated_at=now))

    def enqueue(self, requirement_id, action, payload, key):
        self.expire()
        self.get(requirements, requirement_id)
        try:
            with self.engine.begin() as connection:
                if key:
                    previous = connection.execute(select(jobs).where(and_(jobs.c.requirement_id == requirement_id,
                        jobs.c.idempotency_key == key))).mappings().first()
                    if previous:
                        if previous["action"] != action or previous["input_json"] != encoded(payload):
                            raise Conflict("同一个请求标识对应了不同操作")
                        return public(previous)
                statuses = ["QUEUED", "RUNNING"] if action == "reconcile" else ["QUEUED", "RUNNING", "UNKNOWN"]
                if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == requirement_id,
                        jobs.c.status.in_(statuses)))).first():
                    raise Conflict("此需求有活动任务或待核实任务，请先处理后再操作")
                now = time.time()
                value = dict(id=identifier("job"), requirement_id=requirement_id, action=action,
                    input_json=encoded(payload), status="QUEUED", idempotency_key=key,
                    cancel_requested=False, epoch=0, created_at=now, updated_at=now)
                connection.execute(insert(jobs).values(**value))
                return public(value)
        except IntegrityError:
            # A concurrent request may have won the same idempotency key.
            if key:
                with self.engine.connect() as connection:
                    previous = connection.execute(select(jobs).where(and_(jobs.c.requirement_id == requirement_id,
                        jobs.c.idempotency_key == key))).mappings().first()
                if previous and previous["action"] == action and previous["input_json"] == encoded(payload):
                    return public(previous)
            raise Conflict("此需求已有活动任务") from None

    def claim(self, owner):
        self.expire()
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs).where(jobs.c.status == "QUEUED")
                .order_by(jobs.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row:
                return None
            now = time.time()
            changed = connection.execute(update(jobs).where(and_(jobs.c.id == row["id"], jobs.c.status == "QUEUED"))
                .values(status="RUNNING", owner=owner, epoch=row["epoch"] + 1,
                    lease_until=now + self.lease_seconds, updated_at=now))
            if not changed.rowcount:
                return None
            result = dict(row, status="RUNNING", owner=owner, epoch=row["epoch"] + 1)
            requirement = connection.execute(select(requirements).where(requirements.c.id == row["requirement_id"])).mappings().one()
            project = connection.execute(select(projects).where(projects.c.id == requirement["project_id"])).mappings().one()
            workspace = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement["id"])).mappings().one()
            return {**public(result), "requirement": public(requirement), "project": public(project), "workspace": public(workspace)}

    def owned(self, connection, job_id, owner, epoch):
        row = connection.execute(select(jobs).where(jobs.c.id == job_id).with_for_update()).mappings().first()
        if not row or row["status"] != "RUNNING" or row["owner"] != owner or row["epoch"] != epoch or row["lease_until"] < time.time():
            raise Conflict("执行租约已失效；禁止重放或覆盖结果")
        return row

    def heartbeat(self, job_id, owner, epoch):
        with self.engine.begin() as connection:
            row = self.owned(connection, job_id, owner, epoch)
            connection.execute(update(jobs).where(jobs.c.id == job_id).values(lease_until=time.time() + self.lease_seconds))
            return {"cancel_requested": row["cancel_requested"]}

    def append_event(self, job_id, owner, epoch, seq, kind, payload):
        with self.engine.begin() as connection:
            self.owned(connection, job_id, owner, epoch)
            previous = connection.execute(select(events).where(and_(events.c.job_id == job_id, events.c.seq == seq))).mappings().first()
            if previous:
                if previous["kind"] != kind or previous["payload_json"] != encoded(payload):
                    raise Conflict("事件序号对应不同内容")
                return public(previous)
            value = dict(job_id=job_id, seq=seq, kind=kind, payload_json=encoded(payload), created_at=time.time())
            result = connection.execute(insert(events).values(**value))
            return public(dict(value, id=result.inserted_primary_key[0]))

    def finish(self, job_id, owner, epoch, status, result, error):
        with self.engine.begin() as connection:
            job = self.owned(connection, job_id, owner, epoch)
            now = time.time()
            workspace = connection.execute(select(workspaces).where(workspaces.c.requirement_id == job["requirement_id"])).mappings().one()
            values = {"updated_at": now}
            if job["action"] == "task" or status == "UNKNOWN":
                values["approval"] = "STALE"
            snapshot_value = result.get("snapshot")
            if snapshot_value:
                previous = json.loads(workspace["snapshot_json"]) if workspace["snapshot_json"] else None
                if previous and previous["tree_digest"] != snapshot_value["tree_digest"]:
                    values["approval"] = "STALE"
                values.update(snapshot_json=encoded(snapshot_value), diff=result.get("diff", ""), diff_truncated=result.get("diff_truncated", False))
            if "environment" in result:
                values["environment"] = result["environment"]
            if job["action"] == "review" and status == "SUCCEEDED":
                requested = json.loads(job["input_json"])
                if not snapshot_value or snapshot_value["tree_digest"] != requested["digest"] or result.get("diff_truncated"):
                    raise Conflict("代码已变化，请刷新差异后重新确认")
                connection.execute(insert(reviews).values(id=identifier("rev"), requirement_id=job["requirement_id"],
                    digest=requested["digest"], decision=requested["decision"], reason=requested["reason"], created_at=now))
                values["approval"] = requested["decision"]
            if job["action"] == "reconcile" and status == "SUCCEEDED":
                connection.execute(update(jobs).where(and_(jobs.c.requirement_id == job["requirement_id"],
                    jobs.c.status == "UNKNOWN")).values(status="CANCELED", error="已停止并核实旧执行", updated_at=now))
            connection.execute(update(workspaces).where(workspaces.c.requirement_id == job["requirement_id"]).values(**values))
            connection.execute(update(jobs).where(jobs.c.id == job_id).values(status=status,
                result_json=encoded(result), error=error, updated_at=now, lease_until=None))

    def cancel(self, job_id):
        with self.engine.begin() as connection:
            job = connection.execute(select(jobs).where(jobs.c.id == job_id).with_for_update()).mappings().first()
            if job is None:
                raise Missing("任务不存在")
            if job["status"] == "QUEUED":
                connection.execute(update(jobs).where(jobs.c.id == job_id).values(status="CANCELED", cancel_requested=True, updated_at=time.time()))
            elif job["status"] == "RUNNING":
                connection.execute(update(jobs).where(jobs.c.id == job_id).values(cancel_requested=True, updated_at=time.time()))
            else:
                raise Conflict("任务已结束或需要核实")
        return self.get(jobs, job_id)

    def event_list(self, job_id, after=0):
        with self.engine.connect() as connection:
            return [public(row) for row in connection.execute(select(events).where(and_(events.c.job_id == job_id,
                events.c.id > after)).order_by(events.c.id).limit(200)).mappings()]

    def idle_observation(self, requirement_id, snapshot_value, diff, unreadable=False, diff_truncated=False):
        with self.engine.begin() as connection:
            if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == requirement_id,
                    jobs.c.status.in_(["QUEUED", "RUNNING", "UNKNOWN"])))).first():
                raise Conflict("工作区正在执行或待核实，暂不接受观察结果")
            row = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)
                .with_for_update()).mappings().first()
            if row is None:
                raise Missing("工作区不存在")
            previous = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
            if unreadable or (snapshot_value and previous and previous["tree_digest"] != snapshot_value["tree_digest"]):
                values = {"approval": "STALE", "updated_at": time.time()}
                if snapshot_value:
                    values.update(snapshot_json=encoded(snapshot_value), diff=diff, diff_truncated=diff_truncated)
                connection.execute(update(workspaces).where(workspaces.c.requirement_id == requirement_id).values(**values))

