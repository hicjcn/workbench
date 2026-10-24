"""Versioned fixed workflow; all transitions run in control-plane transactions."""
import hashlib
import json
import shlex
import time

from sqlalchemy import and_, insert, select, update

from . import runtime
from .store import (Conflict, Missing, encoded, flows, identifier, jobs, public,
                    requirements, revisions, steps, workspaces)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def current(connection, requirement_id):
    return connection.execute(select(flows).where(and_(flows.c.requirement_id == requirement_id,
        flows.c.is_current.is_(True))).with_for_update()).mappings().first()


def invalidate(connection, requirement_id, code_digest=None):
    row = current(connection, requirement_id)
    if row and (code_digest is None or row["basis_digest"] != code_digest):
        connection.execute(update(flows).where(flows.c.id == row["id"]).values(state="STALE"))
        connection.execute(update(workspaces).where(workspaces.c.requirement_id == requirement_id).values(approval="STALE"))
        from .materials import invalidate as invalidate_materials
        invalidate_materials(connection, requirement_id)


def idle(connection, requirement_id):
    if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == requirement_id,
            jobs.c.status.in_(["QUEUED", "RUNNING", "UNKNOWN"])))).first():
        raise Conflict("请先完成、取消或核实当前执行")


def revision(connection, requirement_id):
    requirement = connection.execute(select(requirements).where(requirements.c.id == requirement_id).with_for_update()).mappings().first()
    if requirement is None:
        raise Missing("需求不存在")
    row = connection.execute(select(revisions).where(revisions.c.requirement_id == requirement_id)
        .order_by(revisions.c.number.desc()).limit(1)).mappings().first()
    if row:
        return public(row)
    value = dict(id=identifier("rr"), requirement_id=requirement_id, number=1,
        content_json=encoded({key: requirement[key] for key in ("title", "description", "acceptance")}),
        reason="初始需求", created_at=time.time())
    connection.execute(insert(revisions).values(**value))
    return public(value)


def definitions(config):
    nodes = [
        ("analysis", "只读全景分析", config["development_model"]),
        ("scope", "范围与方案确认", None),
        ("development", "Superpowers 开发", config["development_model"]),
        ("verification", "执行验证命令", None),
        *[(f"model_review_{i}", f"模型评审 {i + 1}", model) for i, model in enumerate(config["review_models"])],
        ("resolution", "问题处理确认", None),
        ("self_review", "开发者自审", None),
    ]
    return nodes[2:] if config.get("entry_step") == "development" else nodes


def attempts(connection, flow_id, include_inputs=False):
    query = select(*(column for column in steps.c if include_inputs or column.name != "input_json"),
        jobs.c.status, jobs.c.error).join(jobs, jobs.c.id == steps.c.job_id)
    return [public(row) for row in connection.execute(query.where(steps.c.flow_id == flow_id)
        .order_by(steps.c.created_at, steps.c.id)).mappings()]


def describe(connection, row, include_inputs=False):
    flow = public(row)
    history = attempts(connection, flow["id"], include_inputs)
    latest = {attempt["step_key"]: attempt for attempt in history}
    nodes = []
    next_key = None
    for key, label, model in definitions(flow["config"]):
        attempt = latest.get(key)
        success = attempt and attempt["status"] == "SUCCEEDED" and attempt["output"]
        status = attempt["status"] if attempt else "PENDING"
        if not success and next_key is None:
            next_key = key
            if status not in ("QUEUED", "RUNNING", "UNKNOWN"):
                status = "READY"
        nodes.append({"key": key, "label": label, "model": model, "status": status,
            "error": attempt["error"] if attempt else None,
            "output": attempt["output"] if attempt else None, "job_id": attempt["job_id"] if attempt else None})
    flow.update(nodes=nodes, attempts=history, next_step=next_key if flow["state"] == "ACTIVE" else None)
    return flow


class Workflow:
    def __init__(self, store, model):
        self.store, self.model = store, model

    def read(self, requirement_id):
        self.store.get(requirements, requirement_id)
        self.store.expire()
        with self.store.engine.begin() as connection:
            rev = revision(connection, requirement_id)
            row = current(connection, requirement_id)
            described = describe(connection, row) if row else None
            if described:
                described["attempts"] = [{key: value for key, value in item.items() if key not in ("input", "output")}
                    for item in described["attempts"]]
            return {"revision": rev, "revisions": [public(item) for item in connection.execute(
                select(revisions).where(revisions.c.requirement_id == requirement_id).order_by(revisions.c.number.desc())).mappings()],
                "current": described,
                "history": [public(item) for item in connection.execute(select(flows).where(
                    flows.c.requirement_id == requirement_id).order_by(flows.c.created_at.desc())).mappings()]}

    def archived(self, requirement_id, flow_id):
        with self.store.engine.connect() as connection:
            row = connection.execute(select(flows).where(and_(flows.c.requirement_id == requirement_id, flows.c.id == flow_id))).mappings().first()
            if not row:
                raise Missing("流程记录不存在")
            value = describe(connection, row, include_inputs=True)
            value["revision"] = public(connection.execute(select(revisions).where(revisions.c.id == row["revision_id"])).mappings().one())
            return value

    def revise(self, requirement_id, expected_revision, content, reason):
        if not reason.strip():
            raise Conflict("请填写变更原因")
        with self.store.engine.begin() as connection:
            before = revision(connection, requirement_id)
            idle(connection, requirement_id)
            if before["id"] != expected_revision:
                raise Conflict("需求已更新，请重新载入后编辑")
            if before["content"] == content:
                raise Conflict("需求内容没有变化")
            value = dict(id=identifier("rr"), requirement_id=requirement_id, number=before["number"] + 1,
                content_json=encoded(content), reason=reason, created_at=time.time())
            connection.execute(insert(revisions).values(**value))
            connection.execute(update(requirements).where(requirements.c.id == requirement_id).values(**content))
            invalidate(connection, requirement_id)
            return public(value)

    def start(self, requirement_id, models, command, reason, expected_digest):
        if not reason.strip():
            raise Conflict("请填写开启本轮的原因")
        if len(set(models)) != len(models) or not 2 <= len(models) <= 4:
            raise Conflict("请选择 2 至 4 个不同的评审模型")
        for model in [self.model, *models]:
            (runtime.openrouter_config if model.startswith("openrouter/") else runtime.opencode_free_config)(model)
        try:
            argv = shlex.split(command)
        except ValueError:
            raise Conflict("验证命令的引号不完整") from None
        if not argv or any(part in ("|", "&&", ";", ">", "<") for part in argv):
            raise Conflict("请填写单条验证命令；不支持管道或重定向")
        with self.store.engine.begin() as connection:
            rev = revision(connection, requirement_id)
            idle(connection, requirement_id)
            ws = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)).mappings().one()
            observed = json.loads(ws["snapshot_json"]) if ws["snapshot_json"] else None
            if not observed or observed["tree_digest"] != expected_digest:
                raise Conflict("请先创建工作区并刷新代码快照")
            connection.execute(update(flows).where(and_(flows.c.requirement_id == requirement_id,
                flows.c.state == "ACTIVE")).values(state="SUPERSEDED"))
            connection.execute(update(flows).where(flows.c.requirement_id == requirement_id).values(is_current=False))
            connection.execute(update(workspaces).where(workspaces.c.requirement_id == requirement_id).values(approval="PENDING"))
            from .materials import invalidate as invalidate_materials
            invalidate_materials(connection, requirement_id)
            value = dict(id=identifier("flow"), requirement_id=requirement_id, revision_id=rev["id"],
                is_current=True, state="ACTIVE", basis_digest=expected_digest,
                config_json=encoded({"template": "backend-development-m3/v1", "development_model": self.model,
                    "review_models": models, "verification_argv": argv}), reason=reason, created_at=time.time())
            connection.execute(insert(flows).values(**value))
            return public(value)

    def enqueue(self, requirement_id, flow_id, step_key, supplied, key):
        self.store.expire()
        with self.store.engine.begin() as connection:
            revision(connection, requirement_id)
            # The request key is checked before the moving workflow position.
            if key:
                previous = connection.execute(select(jobs).where(and_(jobs.c.requirement_id == requirement_id,
                    jobs.c.idempotency_key == key))).mappings().first()
                if previous:
                    stored = json.loads(previous["input_json"])
                    if previous["action"] != "workflow" or stored.get("request") != supplied or stored.get("flow_id") != flow_id or stored.get("step_key") != step_key:
                        raise Conflict("同一请求标识对应了不同的流程操作")
                    return public(previous)
            row = current(connection, requirement_id)
            if not row or row["id"] != flow_id or row["state"] != "ACTIVE":
                raise Conflict("本轮已失效或结束，请开启新一轮")
            flow = describe(connection, row)
            if step_key != flow["next_step"]:
                raise Conflict("请按顺序完成前置步骤")
            idle(connection, requirement_id)
            latest = {item["step_key"]: item for item in flow["attempts"]}
            if step_key in ("scope", "resolution", "self_review"):
                if supplied["decision"] not in ("APPROVED", "REJECTED") or not supplied["reason"].strip():
                    raise Conflict("请记录确认结论及原因")
                if step_key == "resolution" and supplied["decision"] == "APPROVED":
                    findings = blocking_findings(flow)
                    dispositions = supplied["dispositions"]
                    if set(dispositions) != set(findings) or any(
                            item.get("decision") not in ("ACCEPTED", "FALSE_POSITIVE") or not item.get("reason", "").strip()
                            for item in dispositions.values()):
                        raise Conflict("每个阻断或主要问题都需要逐项处置和原因；代码修复后应重新评审")
            rev = public(connection.execute(select(revisions).where(revisions.c.id == row["revision_id"])).mappings().one())
            bundle = {"template": flow["config"]["template"], "requirement_revision": rev,
                "round_reason": flow["reason"],
                "code_digest": row["basis_digest"], "config": flow["config"],
                "upstream": {name: item["output"] for name, item in latest.items() if item["status"] == "SUCCEEDED"
                    and not (step_key.startswith("model_review_") and name.startswith("model_review_"))}}
            carried = flow["config"].get("carried_scope")
            if carried:
                bundle["upstream"].update(analysis=carried["analysis"], scope=carried["scope"])
            if step_key.startswith("model_review_"):
                ws = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)).mappings().one()
                bundle["code_diff"] = ws["diff"]
                bundle["diff_truncated"] = ws["diff_truncated"]
            payload = {"flow_id": flow_id, "step_key": step_key, "request": supplied,
                "bundle": bundle, "input_digest": digest(bundle)}
            job = self.store.enqueue(requirement_id, "workflow", payload, key, connection=connection)
            connection.execute(insert(steps).values(id=identifier("step"), flow_id=flow_id, step_key=step_key,
                job_id=job["id"], input_digest=payload["input_digest"], input_json=encoded(payload), created_at=time.time()))
            return job


def blocking_findings(flow):
    return {f"{node['key']}:{index}": finding
        for node in flow["nodes"] if node["key"].startswith("model_review_") and node["output"]
        for index, finding in enumerate(node["output"].get("findings", []))
        if finding["severity"] in ("BLOCKER", "MAJOR")}


def complete(connection, job, status, result, error):
    payload = json.loads(job["input_json"])
    row = current(connection, job["requirement_id"])
    step_key = payload["step_key"]
    report = result.get("workflow_output")
    observed = result.get("snapshot", {}).get("tree_digest")
    if status == "SUCCEEDED":
        if not row or row["id"] != payload["flow_id"] or row["state"] != "ACTIVE" or not report or not observed:
            status, error = "FAILED", "流程上下文已变化或缺少有效产物"
        elif step_key != "development" and observed != payload["bundle"]["code_digest"]:
            invalidate(connection, job["requirement_id"])
            status, error = "FAILED", "代码基线已变化，请重新开启流程"
        elif step_key == "development":
            connection.execute(update(flows).where(flows.c.id == row["id"]).values(basis_digest=observed))
        elif step_key in ("scope", "resolution", "self_review"):
            if report != payload["request"]:
                status, error = "FAILED", "确认结论与请求不一致"
            elif report["decision"] == "REJECTED":
                connection.execute(update(flows).where(flows.c.id == row["id"]).values(state="REJECTED"))
            elif step_key == "self_review":
                connection.execute(update(flows).where(flows.c.id == row["id"]).values(state="COMPLETE"))
                connection.execute(update(workspaces).where(workspaces.c.requirement_id == job["requirement_id"]).values(approval="APPROVED"))
        if status == "SUCCEEDED" and (step_key in ("analysis", "development") or step_key.startswith("model_review_")):
            from .workflow_reports import validate_report
            from .workspace import WorkbenchError
            try:
                report = validate_report(step_key, report)
            except WorkbenchError as failure:
                status, error = "FAILED", str(failure)
        if status == "SUCCEEDED" and step_key == "verification" and (
                report.get("exit_code") != 0 or report.get("command") != payload["bundle"]["config"]["verification_argv"]):
            status, error = "FAILED", "验证命令没有实际成功记录"
    if status != "SUCCEEDED" and observed and row and observed != row["basis_digest"]:
        invalidate(connection, job["requirement_id"])
    if status == "SUCCEEDED":
        connection.execute(update(steps).where(steps.c.job_id == job["id"]).values(output_json=encoded(report)))
    return status, error
