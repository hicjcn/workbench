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
revisions = Table("requirement_revisions", metadata,
    Column("id", String(40), primary_key=True), Column("requirement_id", ForeignKey("requirements.id"), nullable=False),
    Column("number", Integer, nullable=False), Column("content_json", Text, nullable=False),
    Column("reason", Text, nullable=False), Column("created_at", Float, nullable=False),
    UniqueConstraint("requirement_id", "number", name="uq_requirement_revision"))
flows = Table("workflow_runs", metadata,
    Column("id", String(40), primary_key=True), Column("requirement_id", ForeignKey("requirements.id"), nullable=False),
    Column("revision_id", ForeignKey("requirement_revisions.id"), nullable=False),
    Column("is_current", Boolean, nullable=False), Column("state", String(30), nullable=False),
    Column("basis_digest", String(64), nullable=False), Column("config_json", Text, nullable=False),
    Column("reason", Text, nullable=False), Column("created_at", Float, nullable=False))
Index("uq_current_workflow", flows.c.requirement_id, unique=True,
    sqlite_where=flows.c.is_current.is_(True), postgresql_where=flows.c.is_current.is_(True))
steps = Table("workflow_steps", metadata,
    Column("id", String(40), primary_key=True), Column("flow_id", ForeignKey("workflow_runs.id"), nullable=False),
    Column("step_key", String(40), nullable=False), Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True),
    Column("input_digest", String(64), nullable=False), Column("input_json", Text, nullable=False),
    Column("output_json", Text), Column("created_at", Float, nullable=False))
material_rounds = Table("material_rounds", metadata,
    Column("id", String(40), primary_key=True), Column("requirement_id", ForeignKey("requirements.id"), nullable=False),
    Column("flow_id", ForeignKey("workflow_runs.id"), nullable=False), Column("revision_id", ForeignKey("requirement_revisions.id"), nullable=False),
    Column("basis_digest", String(64), nullable=False), Column("is_current", Boolean, nullable=False),
    Column("state", String(30), nullable=False), Column("reason", Text, nullable=False),
    Column("start_json", Text, nullable=False), Column("idempotency_key", String(100)), Column("created_at", Float, nullable=False),
    UniqueConstraint("requirement_id", "idempotency_key", name="uq_material_start"))
Index("uq_current_material_round", material_rounds.c.requirement_id, unique=True,
    sqlite_where=material_rounds.c.is_current.is_(True), postgresql_where=material_rounds.c.is_current.is_(True))
documents = Table("material_versions", metadata,
    Column("id", String(40), primary_key=True), Column("round_id", ForeignKey("material_rounds.id"), nullable=False),
    Column("kind", String(40), nullable=False), Column("number", Integer, nullable=False),
    Column("content_json", Text, nullable=False), Column("content_digest", String(64), nullable=False),
    Column("basis_json", Text, nullable=False), Column("provenance", String(30), nullable=False),
    Column("reason", Text, nullable=False), Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True),
    Column("created_at", Float, nullable=False), UniqueConstraint("round_id", "kind", "number", name="uq_material_version"))
material_records = Table("material_records", metadata,
    Column("id", String(40), primary_key=True), Column("round_id", ForeignKey("material_rounds.id"), nullable=False),
    Column("kind", String(40), nullable=False), Column("payload_json", Text, nullable=False), Column("basis_json", Text, nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True), Column("created_at", Float, nullable=False))
material_attempts = Table("material_attempts", metadata,
    Column("job_id", ForeignKey("jobs.id"), primary_key=True), Column("round_id", ForeignKey("material_rounds.id"), nullable=False),
    Column("step_key", String(40), nullable=False), Column("input_digest", String(64), nullable=False), Column("created_at", Float, nullable=False))
material_exports = Table("material_exports", metadata,
    Column("id", String(40), primary_key=True), Column("round_id", ForeignKey("material_rounds.id"), nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True), Column("sha256", String(64), nullable=False),
    Column("size", Integer, nullable=False), Column("manifest_json", Text, nullable=False), Column("created_at", Float, nullable=False))
delivery_groups = Table("delivery_groups", metadata,
    Column("id", String(40), primary_key=True), Column("state", String(30), nullable=False), Column("created_at", Float, nullable=False))
delivery_revisions = Table("delivery_revisions", metadata,
    Column("id", String(40), primary_key=True), Column("group_id", ForeignKey("delivery_groups.id"), nullable=False),
    Column("number", Integer, nullable=False), Column("is_current", Boolean, nullable=False), Column("content_json", Text, nullable=False),
    Column("created_at", Float, nullable=False), UniqueConstraint("group_id", "number", name="uq_delivery_revision"))
Index("uq_current_delivery_revision", delivery_revisions.c.group_id, unique=True,
    sqlite_where=delivery_revisions.c.is_current.is_(True), postgresql_where=delivery_revisions.c.is_current.is_(True))
delivery_members = Table("delivery_members", metadata,
    Column("revision_id", ForeignKey("delivery_revisions.id"), primary_key=True),
    Column("requirement_id", ForeignKey("requirements.id"), primary_key=True),
    Column("project_id", ForeignKey("projects.id"), nullable=False), Column("position", Integer, nullable=False),
    Column("required", Boolean, nullable=False), Column("skip_reason", Text, nullable=False),
    UniqueConstraint("revision_id", "project_id", name="uq_delivery_project"))
delivery_decisions = Table("delivery_decisions", metadata,
    Column("id", String(40), primary_key=True), Column("group_id", ForeignKey("delivery_groups.id"), nullable=False),
    Column("revision_id", ForeignKey("delivery_revisions.id"), nullable=False), Column("kind", String(30), nullable=False),
    Column("input_digest", String(64), nullable=False), Column("payload_json", Text, nullable=False), Column("created_at", Float, nullable=False))
delivery_attempts = Table("delivery_attempts", metadata,
    Column("job_id", ForeignKey("jobs.id"), primary_key=True), Column("group_id", ForeignKey("delivery_groups.id"), nullable=False),
    Column("revision_id", ForeignKey("delivery_revisions.id"), nullable=False), Column("kind", String(30), nullable=False),
    Column("input_digest", String(64), nullable=False), Column("created_at", Float, nullable=False))
delivery_exports = Table("delivery_exports", metadata,
    Column("id", String(40), primary_key=True), Column("group_id", ForeignKey("delivery_groups.id"), nullable=False),
    Column("revision_id", ForeignKey("delivery_revisions.id"), nullable=False), Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True),
    Column("input_digest", String(64), nullable=False), Column("sha256", String(64), nullable=False), Column("size", Integer, nullable=False),
    Column("manifest_json", Text, nullable=False), Column("created_at", Float, nullable=False))
reservations = Table("workspace_reservations", metadata,
    Column("requirement_id", ForeignKey("requirements.id"), primary_key=True), Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("group_id", ForeignKey("delivery_groups.id"), nullable=False))
lifecycles = Table("workspace_lifecycles", metadata,
    Column("requirement_id", ForeignKey("requirements.id"), primary_key=True), Column("group_id", ForeignKey("delivery_groups.id"), nullable=False))
runtime_guard = Table("runtime_guard", metadata, Column("id", Integer, primary_key=True))
runner_reports = Table("runner_reports", metadata,
    Column("owner", String(80), primary_key=True), Column("report_json", Text, nullable=False), Column("updated_at", Float, nullable=False))
project_profiles = Table("project_profiles", metadata,
    Column("id", String(40), primary_key=True), Column("project_id", ForeignKey("projects.id"), nullable=False),
    Column("number", Integer, nullable=False), Column("is_current", Boolean, nullable=False),
    Column("content_json", Text, nullable=False), Column("content_digest", String(64), nullable=False),
    Column("actor", String(160), nullable=False), Column("reason", Text, nullable=False), Column("created_at", Float, nullable=False),
    UniqueConstraint("project_id", "number", name="uq_project_profile_version"))
Index("uq_current_project_profile", project_profiles.c.project_id, unique=True,
    sqlite_where=project_profiles.c.is_current.is_(True), postgresql_where=project_profiles.c.is_current.is_(True))
workspace_profiles = Table("workspace_profiles", metadata,
    Column("requirement_id", ForeignKey("requirements.id"), primary_key=True),
    Column("profile_id", ForeignKey("project_profiles.id"), nullable=False))
docker_recipes = Table("docker_recipes", metadata,
    Column("id", String(40), primary_key=True), Column("family", String(40), nullable=False),
    Column("number", Integer, nullable=False), Column("name", String(160), nullable=False),
    Column("reference", String(240), nullable=False), Column("dockerfile", Text, nullable=False),
    Column("actor", String(160), nullable=False), Column("reason", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("family", "number", name="uq_docker_recipe_version"))
docker_jobs = Table("docker_jobs", metadata,
    Column("id", String(40), primary_key=True), Column("kind", String(30), nullable=False),
    Column("input_json", Text, nullable=False), Column("status", String(30), nullable=False),
    Column("idempotency_key", String(100), nullable=False, unique=True),
    Column("owner", String(80)), Column("epoch", Integer, nullable=False, default=0),
    Column("lease_until", Float), Column("result_json", Text), Column("error", Text),
    Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False))


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
    def __init__(self, url, lease_seconds=45, max_active_jobs=2):
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        self.lease_seconds = lease_seconds
        self.max_active_jobs = max_active_jobs
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def configure(connection, _):
                # Explicit transactions also cover SELECT, DDL and nested savepoints.
                connection.isolation_level = None
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

            @event.listens_for(self.engine, "begin")
            def begin(connection):
                # Local control transactions are short; serialize SQLite mutations and reads.
                connection.exec_driver_sql("BEGIN IMMEDIATE")

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
            expired = connection.execute(select(jobs.c.id, jobs.c.requirement_id, jobs.c.action).where(
                and_(jobs.c.status == "RUNNING", jobs.c.lease_until < now))).all()
            for job_id, requirement_id, action in expired:
                changed = connection.execute(update(jobs).where(and_(jobs.c.id == job_id,
                    jobs.c.status == "RUNNING", jobs.c.lease_until < now)).values(
                        status="UNKNOWN", error="执行器心跳中断；需要停止并核实，任务不会自动重放", updated_at=now))
                if changed.rowcount and action != "delivery":
                    connection.execute(select(requirements.c.id).where(requirements.c.id == requirement_id).with_for_update()).one()
                    connection.execute(update(workspaces).where(workspaces.c.requirement_id == requirement_id)
                        .values(environment="UNKNOWN", approval="STALE", updated_at=now))
                    from .workflow import invalidate
                    invalidate(connection, requirement_id)

    def enqueue(self, requirement_id, action, payload, key, connection=None, allow_reserved=False):
        if connection is not None:
            return self._enqueue(connection, requirement_id, action, payload, key, allow_reserved)
        self.expire()
        self.get(requirements, requirement_id)
        try:
            with self.engine.begin() as connection:
                return self._enqueue(connection, requirement_id, action, payload, key)
        except IntegrityError:
            # A concurrent request may have won the same idempotency key.
            if key:
                with self.engine.connect() as connection:
                    previous = connection.execute(select(jobs).where(and_(jobs.c.requirement_id == requirement_id,
                        jobs.c.idempotency_key == key))).mappings().first()
                if previous and previous["action"] == action and previous["input_json"] == encoded(payload):
                    return public(previous)
            raise Conflict("此需求已有活动任务") from None

    def _enqueue(self, connection, requirement_id, action, payload, key, allow_reserved=False):
        # Share the member-row lock with group reservations, including legacy actions.
        connection.execute(select(requirements.c.id).where(requirements.c.id == requirement_id).with_for_update()).one()
        if key:
            previous = connection.execute(select(jobs).where(and_(jobs.c.requirement_id == requirement_id,
                jobs.c.idempotency_key == key))).mappings().first()
            if previous:
                if previous["action"] != action or previous["input_json"] != encoded(payload):
                    raise Conflict("同一个请求标识对应了不同操作")
                return public(previous)
        from .delivery import guard_workspace
        if not allow_reserved:
            guard_workspace(connection, requirement_id, allow_archived=action in ("stop", "refresh", "reconcile", "delivery", "app_stop", "app_logs"))
        statuses = ["QUEUED", "RUNNING"] if action == "reconcile" or (action == "delivery" and payload["request"]["kind"] == "reconcile") else ["QUEUED", "RUNNING", "UNKNOWN"]
        if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == requirement_id,
                jobs.c.status.in_(statuses)))).first():
            raise Conflict("此需求有活动任务或待核实任务，请先处理后再操作")
        if action == "prepare":
            from .profiles import pin
            pin(connection, requirement_id)
        now = time.time()
        value = dict(id=identifier("job"), requirement_id=requirement_id, action=action,
            input_json=encoded(payload), status="QUEUED", idempotency_key=key,
            cancel_requested=False, epoch=0, created_at=now, updated_at=now)
        connection.execute(insert(jobs).values(**value))
        return public(value)


    def claim(self, owner):
        self.expire()
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as upsert
            else:
                from sqlalchemy.dialects.sqlite import insert as upsert
            connection.execute(upsert(runtime_guard).values(id=1).on_conflict_do_nothing())
            connection.execute(select(runtime_guard).where(runtime_guard.c.id == 1).with_for_update()).one()
            from sqlalchemy import func
            running_images = connection.execute(select(func.count()).select_from(docker_jobs).where(docker_jobs.c.status == "RUNNING")).scalar_one()
            if connection.execute(select(func.count()).select_from(jobs).where(jobs.c.status == "RUNNING")).scalar_one() + running_images >= self.max_active_jobs:
                return None
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
            from .profiles import binding
            return {**public(result), "requirement": public(requirement), "project": public(project), "workspace": public(workspace),
                "profile": binding(connection, requirement["id"])}

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
            if job["action"] == "delivery":
                from pathlib import Path
                from .delivery import complete as complete_delivery, lock_requirements
                payload = json.loads(job["input_json"])
                lock_requirements(connection, [item["requirement_id"] for item in payload["members"]])
                path = connection.execute(select(workspaces.c.path).where(workspaces.c.requirement_id == job["requirement_id"])).scalar_one()
                status, error = complete_delivery(connection, job, status, result, error, Path(path).parents[2])
                connection.execute(update(jobs).where(jobs.c.id == job_id).values(status=status, result_json=encoded(result), error=error, updated_at=time.time(), lease_until=None))
                return
            # A consistent requirement -> flow/material order avoids cross-resource deadlocks.
            connection.execute(select(requirements.c.id).where(requirements.c.id == job["requirement_id"]).with_for_update()).one()
            from .workflow import complete, invalidate
            if job["action"] == "workflow":
                status, error = complete(connection, job, status, result, error)
            elif job["action"] == "materials":
                from pathlib import Path
                from .materials import complete as complete_materials
                if result.get("snapshot"):
                    invalidate(connection, job["requirement_id"], result["snapshot"]["tree_digest"])
                path = connection.execute(select(workspaces.c.path).where(workspaces.c.requirement_id == job["requirement_id"])).scalar_one()
                status, error = complete_materials(connection, job, status, result, error, Path(path).parents[2])
            elif result.get("snapshot"):
                invalidate(connection, job["requirement_id"], result["snapshot"]["tree_digest"])
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
                if job["action"] == "delivery":
                    from sqlalchemy import delete
                    connection.execute(delete(reservations).where(reservations.c.job_id == job_id))
            elif job["status"] == "RUNNING":
                connection.execute(update(jobs).where(jobs.c.id == job_id).values(cancel_requested=True, updated_at=time.time()))
            else:
                raise Conflict("任务已结束或需要核实")
        return self.get(jobs, job_id)

    def event_list(self, job_id, after=0):
        with self.engine.connect() as connection:
            return [public(row) for row in connection.execute(select(events).where(and_(events.c.job_id == job_id,
                events.c.id > after)).order_by(events.c.id).limit(200)).mappings()]

    def job_history(self, requirement_id):
        columns = [column for column in jobs.c if column.name not in ("input_json", "result_json", "owner", "epoch", "lease_until")]
        with self.engine.connect() as connection:
            from sqlalchemy import func
            return [public(row) for row in connection.execute(select(*columns, func.coalesce(steps.c.step_key, material_attempts.c.step_key).label("step_key"))
                .outerjoin(steps, steps.c.job_id == jobs.c.id).outerjoin(material_attempts, material_attempts.c.job_id == jobs.c.id).where(jobs.c.requirement_id == requirement_id)
                .order_by(jobs.c.created_at.desc())).mappings()]

    def idle_observation(self, requirement_id, snapshot_value, diff, unreadable=False, diff_truncated=False):
        with self.engine.begin() as connection:
            connection.execute(select(requirements.c.id).where(requirements.c.id == requirement_id).with_for_update()).one()
            from .delivery import guard_workspace
            guard_workspace(connection, requirement_id, allow_archived=True)
            if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == requirement_id,
                    jobs.c.status.in_(["QUEUED", "RUNNING", "UNKNOWN"])))).first():
                raise Conflict("工作区正在执行或待核实，暂不接受观察结果")
            row = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)
                .with_for_update()).mappings().first()
            if row is None:
                raise Missing("工作区不存在")
            previous = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
            if unreadable or (snapshot_value and previous and previous["tree_digest"] != snapshot_value["tree_digest"]):
                from .workflow import invalidate
                invalidate(connection, requirement_id, snapshot_value["tree_digest"] if snapshot_value else None)
                values = {"approval": "STALE", "updated_at": time.time()}
                if snapshot_value:
                    values.update(snapshot_json=encoded(snapshot_value), diff=diff, diff_truncated=diff_truncated)
                connection.execute(update(workspaces).where(workspaces.c.requirement_id == requirement_id).values(**values))

