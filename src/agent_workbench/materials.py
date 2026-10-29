"""Versioned materials after M3 self-review; definitions never imply execution."""
import json
import time
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, insert, select, update

from .material_schemas import DOCUMENT_SCHEMAS, RECORD_SCHEMAS, Edit, document
from .store import (Conflict, Missing, documents, encoded, flows, identifier, jobs, material_attempts,
    material_exports, material_records, material_rounds, public, requirements, revisions, workspaces)
from .workflow import current, describe as describe_flow, digest, idle, revision
from .workspace import WorkbenchError


def invalidate(connection, requirement_id):
    connection.execute(update(material_rounds).where(and_(material_rounds.c.requirement_id == requirement_id,
        material_rounds.c.is_current.is_(True))).values(state="STALE"))


def rows(connection, table, round_id):
    return [public(row) for row in connection.execute(select(table).where(table.c.round_id == round_id)
        .order_by(table.c.created_at, list(table.primary_key)[0])).mappings()]


def source(row):
    return {"flow_id": row["flow_id"], "revision_id": row["revision_id"], "code_digest": row["basis_digest"]}


def record_basis(state, kind):
    versions = {name: item["id"] if item else None for name, item in state["documents"].items()}
    if kind == "summary_design":
        links = {"comment": state["comment_id"]}
    elif kind == "supervisor_review":
        links = {"summary_design": versions["summary_design"], "comment": state["comment_id"]}
    elif kind in ("performance_decision", "test_plan"):
        links = {"summary_design": versions["summary_design"], "supervisor": state["supervisor"]["id"] if state["supervisor"] else None}
        if kind == "test_plan":
            links["performance"] = state["performance"]["id"] if state["performance"] else None
    else:
        links = {**versions, "supervisor": state["supervisor"]["id"] if state["supervisor"] else None,
            "performance": state["performance"]["id"] if state["performance"] else None}
        if kind == "performance_plan":
            links.pop("performance_plan", None)
        if kind == "acceptance":
            links.update(test_records=state["effective_execution_ids"], resolutions=state["resolution_ids"], feedback=state["feedback_ids"])
    return {"source": source(state), **links}


def describe(connection, row):
    state = public(row)
    parent = connection.execute(select(flows).where(flows.c.id == row["flow_id"])).mappings().one()
    ws = connection.execute(select(workspaces).where(workspaces.c.requirement_id == row["requirement_id"])).mappings().one()
    code = json.loads(ws["snapshot_json"])["tree_digest"] if ws["snapshot_json"] else None
    valid = row["is_current"] and row["state"] == "ACTIVE" and parent["is_current"] and parent["state"] == "COMPLETE" and code == row["basis_digest"] and ws["approval"] == "APPROVED"
    state.update(context_valid=bool(valid), state="ACTIVE" if valid else "STALE")
    versions = rows(connection, documents, row["id"])
    records = rows(connection, material_records, row["id"])
    latest = {item["kind"]: item for item in records}
    docs = {kind: max((item for item in versions if item["kind"] == kind), key=lambda item: item["number"], default=None) for kind in DOCUMENT_SCHEMAS}
    related = [public(item) for item in connection.execute(select(material_records, material_rounds.c.flow_id)
        .join(material_rounds, material_rounds.c.id == material_records.c.round_id)
        .where(and_(material_rounds.c.requirement_id == row["requirement_id"], material_records.c.kind.in_(["feedback", "resolve_feedback"])))
        .order_by(material_records.c.created_at, material_records.c.id)).mappings()]
    comments = [item for item in related if item["kind"] == "feedback" and item["payload"]["kind"] == "DOCUMENT_COMMENT" and item["flow_id"] == row["flow_id"]]
    state.update(documents=docs, records=records, supervisor=latest.get("supervisor_review"), performance=latest.get("performance_decision"),
        comment_id=comments[-1]["id"] if comments else None, effective_execution_ids=[], resolution_ids=[], feedback_ids=[])
    def matches(item, kind):
        return bool(valid and item and item["basis"] == record_basis(state, kind))
    def doc_current(kind):
        item = docs[kind]
        return bool(item and item["content_digest"] == digest(item["content"]) and matches(item, kind))
    summary_current = doc_current("summary_design")
    supervisor_current = summary_current and matches(state["supervisor"], "supervisor_review")
    supervisor_approved = supervisor_current and state["supervisor"]["payload"]["decision"] == "APPROVED"
    performance_current = supervisor_approved and matches(state["performance"], "performance_decision")
    decision = state["performance"]["payload"]["decision"] if performance_current else "UNDECIDED"
    functional_current = supervisor_approved and decision != "UNDECIDED" and doc_current("test_plan")
    performance_plan_current = functional_current and decision == "REQUIRED" and doc_current("performance_plan")
    plans_ready = functional_current and (decision == "NOT_REQUIRED" or performance_plan_current)
    effective = {}
    execution_basis = record_basis(state, "test_execution")
    executions = [item for item in records if item["kind"] == "test_execution" and item["basis"] == execution_basis and valid]
    for execution in sorted(executions, key=lambda item: (datetime.fromisoformat(item["payload"]["occurred_at"]).timestamp(), item["created_at"], item["id"])):
        for result in execution["payload"]["results"]:
            effective[(result["version_id"], result["case_id"])] = {**result, "record_id": execution["id"]}
    cases = []
    for kind, enabled in (("test_plan", functional_current), ("performance_plan", performance_plan_current)):
        if enabled:
            for case in docs[kind]["content"]["cases"]:
                result = effective.get((docs[kind]["id"], case["id"]))
                cases.append({**case, "plan_kind": kind, "version_id": docs[kind]["id"], "result": result, "status": result["status"] if result else "NOT_RUN"})
    required = [case for case in cases if case["required"]]
    test_status = "PASS" if plans_ready and required and all(case["status"] == "PASS" for case in required) else "FAIL" if any(case["status"] == "FAIL" for case in required) else "BLOCKED" if any(case["status"] == "BLOCKED" for case in required) else "NOT_RUN"
    state["effective_execution_ids"] = sorted({case["result"]["record_id"] for case in cases if case["result"]})
    feedback = [item for item in related if item["kind"] == "feedback" and item["payload"]["kind"] != "DOCUMENT_COMMENT"]
    resolutions = {item["payload"]["feedback_id"]: item for item in related if item["kind"] == "resolve_feedback" and item["basis"] == {"source": source(state)} and valid}
    outstanding = [item for item in feedback if item["id"] not in resolutions]
    state.update(resolution_ids=sorted(item["id"] for item in resolutions.values()), feedback_ids=sorted(item["id"] for item in feedback))
    acceptance = latest.get("acceptance")
    accepted = bool(test_status == "PASS" and not outstanding and matches(acceptance, "acceptance"))
    stage = "ACCEPTED" if accepted else "SUMMARY_DESIGN" if not summary_current else "SUPERVISOR_REVIEW" if not supervisor_approved else "PERFORMANCE_DECISION" if decision == "UNDECIDED" else "TEST_PLAN" if not functional_current else "PERFORMANCE_PLAN" if decision == "REQUIRED" and not performance_plan_current else "TEST_EXECUTION" if test_status != "PASS" else "FEEDBACK_RESOLUTION" if outstanding else "ACCEPTANCE"
    state.update(stage=stage if valid else "STALE", performance_decision=decision, test_status=test_status, cases=cases,
        outstanding_feedback=outstanding, feedback_history=related, accepted=accepted, next_step=stage.lower() if valid and not accepted else None,
        supervisor_valid=bool(supervisor_current), performance_valid=bool(performance_current))
    for kind, item in docs.items():
        if item:
            item["validity"] = "CURRENT" if {"summary_design": summary_current, "test_plan": functional_current, "performance_plan": performance_plan_current}[kind] else "STALE"
    state["version_history"] = [{key: value for key, value in item.items() if key not in ("content", "basis")} for item in versions]
    for item in state["version_history"]:
        item.setdefault("validity", "HISTORICAL")
    for item in records:
        item["validity"] = "CURRENT" if valid and (
            (item["kind"] == "supervisor_review" and supervisor_current and item["id"] == state["supervisor"]["id"])
            or (item["kind"] == "performance_decision" and performance_current and item["id"] == state["performance"]["id"])
            or (item["kind"] == "test_execution" and item in executions)
            or (item["kind"] == "acceptance" and accepted and item["id"] == acceptance["id"])
            or (item["kind"] == "resolve_feedback" and item["id"] in state["resolution_ids"])) else "HISTORICAL"
    state["exports"] = rows(connection, material_exports, row["id"])
    state["attempts"] = [dict(item) for item in connection.execute(select(material_attempts, jobs.c.status, jobs.c.error)
        .join(jobs, jobs.c.id == material_attempts.c.job_id).where(material_attempts.c.round_id == row["id"])
        .order_by(material_attempts.c.created_at)).mappings()]
    state["context_digest"] = digest({"source": source(state), "round": state["id"], "valid": valid,
        "documents": {kind: item["id"] if item else None for kind, item in docs.items()}, "records": [item["id"] for item in records],
        "related": [item["id"] for item in related]})
    return state


def validate_action(state, step, data):
    if step == "export":
        return data
    if not state["context_valid"]:
        raise Conflict("材料轮次已失效，请在当前代码自审后开启新一轮")
    kind = data.get("kind") if step == "edit_document" else step
    if step == "edit_document":
        try:
            data = Edit.model_validate(data).model_dump()
        except ValidationError:
            raise Conflict("请提供文档版本、正文及修改原因") from None
        existing = state["documents"][kind]
        if not existing or existing["id"] != data["expected_version"] or not data["reason"].strip():
            raise Conflict("文档已更新，请重新载入后编辑")
        data["content"] = document(kind, data["content"])
    if kind in ("test_plan", "performance_plan"):
        if not state["supervisor_valid"] or state["supervisor"]["payload"]["decision"] != "APPROVED" or state["performance_decision"] == "UNDECIDED":
            raise Conflict("请先取得当前概要设计的主管通过，并明确性能测试判断")
        if kind == "performance_plan" and (state["performance_decision"] != "REQUIRED" or not state["documents"]["test_plan"] or state["documents"]["test_plan"]["validity"] != "CURRENT"):
            raise Conflict("当前不需要性能测试单，或功能测试单尚未就绪")
    if step in RECORD_SCHEMAS:
        try:
            data = RECORD_SCHEMAS[step].model_validate(data).model_dump(mode="json")
        except ValidationError:
            raise Conflict("请完整填写人员、带时区的发生时间、实际结论及证据引用") from None
    if step == "supervisor_review":
        if not state["documents"]["summary_design"] or state["documents"]["summary_design"]["validity"] != "CURRENT":
            raise Conflict("请先生成或更新概要设计")
        if datetime.fromisoformat(data["occurred_at"]).timestamp() + 1 < state["documents"]["summary_design"]["created_at"]:
            raise Conflict("评审时间不能早于所评文档版本")
    if step == "performance_decision" and (not state["supervisor_valid"] or state["supervisor"]["payload"]["decision"] != "APPROVED"):
        raise Conflict("请先登记当前版本的主管通过")
    if step == "test_execution":
        if not state["cases"] or state["performance_decision"] == "UNDECIDED" or state["stage"] in ("SUMMARY_DESIGN", "SUPERVISOR_REVIEW", "TEST_PLAN", "PERFORMANCE_PLAN"):
            raise Conflict("当前测试定义和评审尚未就绪")
        allowed = {(case["version_id"], case["id"]) for case in state["cases"]}
        if any((result["version_id"], result["case_id"]) not in allowed for result in data["results"]):
            raise Conflict("测试结果引用了其他版本或不存在的用例")
        if datetime.fromisoformat(data["occurred_at"]).timestamp() + 1 < max(item["created_at"] for item in state["documents"].values() if item and item["validity"] == "CURRENT"):
            raise Conflict("执行时间不能早于所引用的测试定义")
    if step == "acceptance" and (state["test_status"] != "PASS" or state["outstanding_feedback"]):
        raise Conflict("必需用例尚未通过或仍有未处理反馈，不能确认材料阶段验收")
    if step == "resolve_feedback" and data["feedback_id"] not in {item["id"] for item in state["outstanding_feedback"]}:
        raise Conflict("该反馈不存在或已在当前基线处理")
    if step == "feedback" and data["kind"] == "REQUIREMENT_CHANGE":
        from .api import RevisionInput
        try:
            content = RevisionInput.model_validate({**(data["new_requirement"] or {}), "expected_revision": state["revision_id"], "reason": data["reason"]})
            data["new_requirement"] = content.model_dump(include={"title", "description", "acceptance"})
        except ValidationError:
            raise Conflict("需求变更必须填写完整的新需求正文") from None
    return data


class Materials:
    def __init__(self, store):
        self.store = store

    def read(self, requirement_id, round_id=None):
        self.store.get(requirements, requirement_id)
        self.store.expire()
        with self.store.engine.begin() as connection:
            parent = current(connection, requirement_id)
            query = select(material_rounds).where(material_rounds.c.requirement_id == requirement_id)
            selected = query.where(material_rounds.c.id == round_id) if round_id else query.where(material_rounds.c.is_current.is_(True))
            row = connection.execute(selected).mappings().first()
            if round_id and not row:
                raise Missing("材料轮次不存在")
            return {"can_start": bool(parent and parent["state"] == "COMPLETE"), "current": describe(connection, row) if row else None,
                "history": [public(item) for item in connection.execute(query.order_by(material_rounds.c.created_at.desc())).mappings()]}

    def start(self, requirement_id, supplied, key):
        self.store.expire()
        with self.store.engine.begin() as connection:
            revision(connection, requirement_id)
            if key:
                previous = connection.execute(select(material_rounds).where(and_(material_rounds.c.requirement_id == requirement_id, material_rounds.c.idempotency_key == key))).mappings().first()
                if previous:
                    if previous["start_json"] != encoded(supplied):
                        raise Conflict("同一请求标识对应了不同材料轮次")
                    return public(previous)
            idle(connection, requirement_id)
            parent = current(connection, requirement_id)
            ws = connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)).mappings().one()
            if not parent or parent["id"] != supplied["flow_id"] or parent["state"] != "COMPLETE" or ws["approval"] != "APPROVED" or parent["basis_digest"] != supplied["digest"]:
                raise Conflict("请先完成当前代码的 M3 自审")
            if not supplied["reason"].strip():
                raise Conflict("请填写开启材料轮次的原因")
            invalidate(connection, requirement_id)
            connection.execute(update(material_rounds).where(material_rounds.c.requirement_id == requirement_id).values(is_current=False))
            value = dict(id=identifier("mat"), requirement_id=requirement_id, flow_id=parent["id"], revision_id=parent["revision_id"],
                basis_digest=parent["basis_digest"], is_current=True, state="ACTIVE", reason=supplied["reason"], start_json=encoded(supplied),
                idempotency_key=key, created_at=time.time())
            connection.execute(insert(material_rounds).values(**value))
            return public(value)

    def enqueue(self, requirement_id, supplied, key):
        self.store.expire()
        with self.store.engine.begin() as connection:
            revision(connection, requirement_id)
            if key:
                previous = connection.execute(select(jobs).where(and_(jobs.c.requirement_id == requirement_id, jobs.c.idempotency_key == key))).mappings().first()
                if previous:
                    if previous["action"] != "materials" or json.loads(previous["input_json"])["request"] != supplied:
                        raise Conflict("同一请求标识对应了不同材料操作")
                    return public(previous)
            idle(connection, requirement_id)
            row = connection.execute(select(material_rounds).where(and_(material_rounds.c.id == supplied["round_id"], material_rounds.c.requirement_id == requirement_id)).with_for_update()).mappings().first()
            if row is None:
                raise Missing("材料轮次不存在")
            state = describe(connection, row)
            if state["context_digest"] != supplied["context_digest"]:
                raise Conflict("材料版本或记录已变化，请重新载入表单")
            normalized = validate_action(state, supplied["step_key"], supplied["data"])
            parent = connection.execute(select(flows).where(flows.c.id == row["flow_id"])).mappings().one()
            rev = public(connection.execute(select(revisions).where(revisions.c.id == row["revision_id"])).mappings().one())
            flow = describe_flow(connection, parent)
            bundle = {"template": "backend-materials-m4/v1", "code_digest": row["basis_digest"], "requirement_revision": rev,
                "model": flow["config"]["development_model"],
                **({"model_source": "agent_cli"} if flow["config"].get("model_source") == "agent_cli" else {}),
                "upstream": {node["key"]: node["output"] for node in flow["nodes"] if node["output"]},
                "materials": {kind: item for kind, item in state["documents"].items() if item},
                "supervisor": state["supervisor"], "performance": state["performance"], "feedback": state["feedback_history"]}
            payload = {"round_id": row["id"], "step_key": supplied["step_key"], "request": supplied,
                "data": normalized, "bundle": bundle, "input_digest": digest(bundle)}
            if supplied["step_key"] == "export":
                payload["export_state"] = state
                payload["input_digest"] = digest({"bundle": bundle, "export_state": state})
            job = self.store.enqueue(requirement_id, "materials", payload, key, connection=connection)
            connection.execute(insert(material_attempts).values(job_id=job["id"], round_id=row["id"], step_key=supplied["step_key"], input_digest=payload["input_digest"], created_at=time.time()))
            return job


def fork_for_feedback(connection, state, data):
    parent = current(connection, state["requirement_id"])
    before = revision(connection, state["requirement_id"])
    if not parent or parent["id"] != state["flow_id"] or before["id"] != state["revision_id"]:
        raise Conflict("反馈对应的需求或流程已变化")
    config = json.loads(parent["config_json"])
    if data["kind"] in ("REQUIREMENT_CHANGE", "ARCHITECTURE_CHANGE"):
        content = data["new_requirement"] if data["kind"] == "REQUIREMENT_CHANGE" else before["content"]
        if data["kind"] == "REQUIREMENT_CHANGE" and content == before["content"]:
            raise Conflict("需求内容没有变化")
        value = dict(id=identifier("rr"), requirement_id=state["requirement_id"], number=before["number"] + 1,
            content_json=encoded(content), reason=f"{data['kind']}: {data['reason']}", created_at=time.time())
        connection.execute(insert(revisions).values(**value))
        connection.execute(update(requirements).where(requirements.c.id == state["requirement_id"]).values(**content))
        revision_id = value["id"]
        config.pop("entry_step", None); config.pop("carried_scope", None)
    else:
        revision_id = before["id"]
        if data["scope_unchanged"]:
            previous = describe_flow(connection, parent)
            upstream = {node["key"]: node["output"] for node in previous["nodes"]}
            carried = config.get("carried_scope", {})
            analysis, scope = upstream.get("analysis") or carried.get("analysis"), upstream.get("scope") or carried.get("scope")
            if not analysis or not scope or scope.get("decision") != "APPROVED":
                raise Conflict("没有可沿用的已确认范围，请返回完整分析流程")
            config.update(entry_step="development", carried_scope={"flow_id": parent["id"], "analysis": analysis, "scope": scope, "reason": data["reason"]})
        else:
            config.pop("entry_step", None); config.pop("carried_scope", None)
    connection.execute(update(flows).where(flows.c.requirement_id == state["requirement_id"]).values(is_current=False))
    value = dict(id=identifier("flow"), requirement_id=state["requirement_id"], revision_id=revision_id,
        is_current=True, state="ACTIVE", basis_digest=state["basis_digest"], config_json=encoded(config),
        reason=f"{data['kind']}: {data['reason']}", created_at=time.time())
    connection.execute(insert(flows).values(**value))
    connection.execute(update(workspaces).where(workspaces.c.requirement_id == state["requirement_id"]).values(approval="PENDING"))
    invalidate(connection, state["requirement_id"])
    return value["id"]


def complete(connection, job, status, result, error, data_dir):
    if status != "SUCCEEDED":
        return status, error
    payload = json.loads(job["input_json"])
    row = connection.execute(select(material_rounds).where(material_rounds.c.id == payload["round_id"]).with_for_update()).mappings().one()
    state = describe(connection, row)
    step, output = payload["step_key"], result.get("material_output")
    if not result.get("snapshot") or (step != "export" and result["snapshot"]["tree_digest"] != row["basis_digest"]):
        return "FAILED", "没有核实当前代码基线，材料结果不能生效"
    if not isinstance(output, dict):
        return "FAILED", "缺少有效材料结果"
    if payload["request"]["context_digest"] != state["context_digest"]:
        return "FAILED", "材料版本或基线已变化，结果不能生效"
    try:
        with connection.begin_nested():
            validate_action(state, step, payload["data"])
            if step in DOCUMENT_SCHEMAS or step == "edit_document":
                kind = payload["data"]["kind"] if step == "edit_document" else step
                content = document(kind, output["content"] if step == "edit_document" else output)
                if step == "edit_document" and output != payload["data"]:
                    raise Conflict("人工修改与登记请求不一致")
                previous = state["documents"][kind]
                connection.execute(insert(documents).values(id=identifier("doc"), round_id=row["id"], kind=kind,
                    number=previous["number"] + 1 if previous else 1, content_json=encoded(content), content_digest=digest(content),
                    basis_json=encoded(record_basis(state, kind)), provenance="HUMAN" if step == "edit_document" else "AGENT",
                    reason=payload["data"]["reason"] if step == "edit_document" else "Agent 按固定模板生成，待人工检查", job_id=job["id"], created_at=time.time()))
            elif step in RECORD_SCHEMAS:
                if output != payload["data"]:
                    raise Conflict("人工记录与登记请求不一致")
                basis = {"source": source(state)} if step in ("feedback", "resolve_feedback") else record_basis(state, step)
                connection.execute(insert(material_records).values(id=identifier("mr"), round_id=row["id"], kind=step,
                    payload_json=encoded(output), basis_json=encoded(basis), job_id=job["id"], created_at=time.time()))
                if step == "feedback" and output["kind"] != "DOCUMENT_COMMENT":
                    fork_for_feedback(connection, state, output)
            elif step == "export":
                from .material_export import verify_export
                verify_export(data_dir, job["id"], output, payload["input_digest"])
                connection.execute(insert(material_exports).values(id=identifier("exp"), round_id=row["id"], job_id=job["id"],
                    sha256=output["sha256"], size=output["size"], manifest_json=encoded(output["manifest"]), created_at=time.time()))
    except (Conflict, WorkbenchError, KeyError, ValueError) as failure:
        return "FAILED", str(failure)
    return status, error
