"""Cross-project scope, all-member reservations and immutable delivery packages."""
import json
import time
from sqlalchemy import and_, delete, insert, select, update

from . import materials
from .store import (Conflict, Missing, delivery_groups as groups, delivery_revisions as revisions,
    delivery_members as members, delivery_decisions as decisions, delivery_attempts as attempts,
    delivery_exports as exports, reservations, lifecycles, requirements, projects, workspaces, flows,
    material_rounds, documents, material_records, jobs, encoded, identifier, public)
from .workflow import digest, describe as describe_flow


ACTIVE = ["QUEUED", "RUNNING", "UNKNOWN"]


def lock_requirements(connection, ids):
    for identity in sorted(set(ids)):
        if not connection.execute(select(requirements.c.id).where(requirements.c.id == identity).with_for_update()).first():
            raise Missing("成员需求不存在")


def guard_workspace(connection, requirement_id, allow_archived=False):
    if connection.execute(select(reservations).where(reservations.c.requirement_id == requirement_id)).first():
        raise Conflict("跨项目操作正在使用或待核实，请从交付面板处理")
    if not allow_archived and connection.execute(select(lifecycles).where(lifecycles.c.requirement_id == requirement_id)).first():
        raise Conflict("需求已随跨项目交付归档，请先重新打开交付")


def current_revision(connection, group_id):
    row = connection.execute(select(revisions).where(and_(revisions.c.group_id == group_id, revisions.c.is_current.is_(True)))).mappings().first()
    if row is None:
        raise Missing("交付范围不存在")
    return row


def member_rows(connection, revision_id):
    return [dict(row) for row in connection.execute(select(members).where(members.c.revision_id == revision_id).order_by(members.c.position)).mappings()]


def child(connection, membership, ignore_job=None):
    req = public(connection.execute(select(requirements).where(requirements.c.id == membership["requirement_id"])).mappings().one())
    project = public(connection.execute(select(projects).where(projects.c.id == req["project_id"])).mappings().one())
    ws = public(connection.execute(select(workspaces).where(workspaces.c.requirement_id == req["id"])).mappings().one())
    parent = connection.execute(select(flows).where(and_(flows.c.requirement_id == req["id"], flows.c.is_current.is_(True)))).mappings().first()
    flow = describe_flow(connection, parent) if parent else None
    analysis_ref = next((item["job_id"] for item in flow["nodes"] if item["key"] == "analysis" and item["status"] == "SUCCEEDED"), None) if flow else None
    analysis = next((item["output"] for item in flow["nodes"] if item["key"] == "analysis" and item["status"] == "SUCCEEDED"), None) if flow else None
    if flow and flow["state"] not in ("STALE", "SUPERSEDED"):
        analysis = analysis or flow["config"].get("carried_scope", {}).get("analysis")
        analysis_ref = analysis_ref or flow["config"].get("carried_scope", {}).get("flow_id")
    else:
        analysis = None
    material_row = connection.execute(select(material_rounds).where(and_(material_rounds.c.requirement_id == req["id"], material_rounds.c.is_current.is_(True)))).mappings().first()
    material = materials.describe(connection, material_row) if material_row else None
    busy = connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == req["id"], jobs.c.status.in_(ACTIVE), jobs.c.id != (ignore_job or "")))).first()
    reserved = connection.execute(select(reservations.c.job_id).where(and_(reservations.c.requirement_id == req["id"], reservations.c.job_id != (ignore_job or "")))).first()
    reasons = []
    if not flow or flow["state"] != "COMPLETE" or ws["approval"] != "APPROVED": reasons.append("当前代码尚未完成自审")
    if not material or not material["accepted"]: reasons.append("材料、必需测试或反馈尚未完成")
    if busy or reserved: reasons.append("存在执行或待核实任务")
    baseline = {"requirement_id": req["id"], "revision_id": flow["revision_id"] if flow else None,
        "code_digest": ws["snapshot"]["tree_digest"] if ws["snapshot"] else None,
        "commit_sha": ws["snapshot"]["commit_sha"] if ws["snapshot"] else None,
        "flow_id": flow["id"] if flow else None, "flow_state": flow["state"] if flow else None,
        "approval": ws["approval"], "material_id": material["id"] if material else None,
        "material_context": material["context_digest"] if material else None, "accepted": bool(material and material["accepted"])}
    return {**membership, "requirement": req, "project": project, "workspace_path": ws["path"],
        "analysis": analysis, "analysis_ref": analysis_ref, "baseline": baseline, "ready": not reasons, "blocking_reasons": reasons,
        "material": material, "flow": flow}


def describe(connection, group_id, ignore_job=None, full=False):
    group = public(connection.execute(select(groups).where(groups.c.id == group_id)).mappings().first())
    if not group: raise Missing("跨项目交付不存在")
    rev = current_revision(connection, group_id)
    children = [child(connection, item, ignore_job) for item in member_rows(connection, rev["id"])]
    scope_items = [{"requirement_id": item["requirement_id"], "required": item["required"], "skip_reason": item["skip_reason"],
        "revision": item["baseline"]["revision_id"] if item["required"] else digest(item["requirement"]),
        "analysis": item["analysis"] if item["required"] else None, "analysis_ref": item["analysis_ref"] if item["required"] else None} for item in children]
    scope_digest = digest({"revision": rev["id"], "members": scope_items})
    from .panorama import describe as describe_panorama
    panorama = describe_panorama(connection, group_id, rev["id"], children)
    if panorama["analysis"]:
        scope_digest = digest({"scope": scope_digest, "panorama": panorama["analysis"]["id"]})
    decision = connection.execute(select(decisions).where(and_(decisions.c.revision_id == rev["id"], decisions.c.kind == "SCOPE"))
        .order_by(decisions.c.created_at.desc()).limit(1)).mappings().first()
    decision = public(decision)
    scope_ready = all(item["analysis"] for item in children if item["required"])
    if panorama["analysis_required"] and not panorama["analysis"]: scope_ready = False
    if panorama["analysis"] and not panorama["analysis"]["requirements_current"]: scope_ready = False
    scope_approved = bool(scope_ready and decision and decision["input_digest"] == scope_digest and decision["payload"]["decision"] == "APPROVED")
    baseline_digest = digest({"scope": scope_digest, "decision": decision["id"] if scope_approved else None,
        "baselines": [item["baseline"] for item in children if item["required"]]})
    integration = public(connection.execute(select(decisions).where(and_(decisions.c.revision_id == rev["id"], decisions.c.kind == "INTEGRATION"))
        .order_by(decisions.c.created_at.desc()).limit(1)).mappings().first())
    integration_valid = bool(integration and integration["input_digest"] == baseline_digest and integration["payload"]["result"] in ("PASS", "NOT_REQUIRED"))
    context = digest({"baseline": baseline_digest, "integration": integration["id"] if integration_valid else None})
    result = {**group, "revision": public(rev), "members": children, "scope_digest": scope_digest, "panorama": panorama,
        "scope_ready": bool(scope_ready), "scope_approved": scope_approved, "scope_decision": decision,
        "context_digest": context, "baseline_digest": baseline_digest, "integration": integration, "integration_valid": integration_valid,
        "members_ready": all(item["ready"] for item in children if item["required"]),
        "ready": integration_valid and scope_approved and all(item["ready"] for item in children if item["required"])}
    result["exports"] = [{**public(row), "validity": "CURRENT" if row["input_digest"] == context and result["ready"] else "STALE"}
        for row in connection.execute(select(exports).where(exports.c.group_id == group_id).order_by(exports.c.created_at.desc())).mappings()]
    result["operations"] = [dict(row) for row in connection.execute(select(attempts, jobs.c.status, jobs.c.error)
        .join(jobs, jobs.c.id == attempts.c.job_id).where(attempts.c.group_id == group_id).order_by(attempts.c.created_at.desc())).mappings()]
    result["history"] = [public(row) for row in connection.execute(select(revisions).where(revisions.c.group_id == group_id).order_by(revisions.c.number.desc())).mappings()]
    if not full:
        for item in children:
            if item["material"]:
                item["material"] = {key: item["material"][key] for key in ("id", "stage", "accepted", "test_status")}
            item.pop("flow", None)
    return result


def scope_gate(connection, requirement_id):
    linked = connection.execute(select(members.c.revision_id, revisions.c.group_id).join(revisions, revisions.c.id == members.c.revision_id)
        .join(groups, groups.c.id == revisions.c.group_id).where(and_(members.c.requirement_id == requirement_id,
            members.c.required.is_(True), revisions.c.is_current.is_(True), groups.c.state == "ACTIVE"))).first()
    if linked and not describe(connection, linked.group_id)["scope_approved"]:
        raise Conflict("请先完成跨项目分析汇总与范围确认，再开始开发")


class Delivery:
    def __init__(self, store): self.store = store

    def list(self):
        self.store.expire()
        with self.store.engine.connect() as connection:
            return [{"id": row["id"], "state": row["state"], "revision": public(current_revision(connection, row["id"]))}
                for row in connection.execute(select(groups).order_by(groups.c.created_at.desc())).mappings()]

    def read(self, group_id):
        self.store.expire()
        with self.store.engine.connect() as connection: return describe(connection, group_id)

    def save(self, supplied, group_id=None):
        ids = [item["requirement_id"] for item in supplied["members"]]
        with self.store.engine.begin() as connection:
            if group_id:
                before = current_revision(connection, group_id)
                ids += [item["requirement_id"] for item in member_rows(connection, before["id"])]
            lock_requirements(connection, ids)
            if group_id:
                group = connection.execute(select(groups).where(groups.c.id == group_id).with_for_update()).mappings().one()
                before = current_revision(connection, group_id)
                if group["state"] != "ACTIVE" or supplied["expected_revision"] != before["id"]: raise Conflict("交付已归档或范围已变化")
            for identity in set(ids):
                guard_workspace(connection, identity)
                if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == identity, jobs.c.status.in_(ACTIVE)))).first(): raise Conflict("成员仍在执行或待核实")
            project_ids = [connection.execute(select(requirements.c.project_id).where(requirements.c.id == item["requirement_id"])).scalar_one() for item in supplied["members"]]
            if len(set(project_ids)) != len(project_ids): raise Conflict("同一交付中每个项目只关联一个需求")
            for item in supplied["members"]:
                linked = connection.execute(select(revisions.c.group_id).join(members, members.c.revision_id == revisions.c.id).join(groups, groups.c.id == revisions.c.group_id)
                    .where(and_(members.c.requirement_id == item["requirement_id"], revisions.c.is_current.is_(True), groups.c.state == "ACTIVE", groups.c.id != (group_id or "")))).first()
                if linked: raise Conflict("成员已经属于其他活动交付")
            if not group_id:
                group_id = identifier("del")
                connection.execute(insert(groups).values(id=group_id, state="ACTIVE", created_at=time.time()))
                number = 1
            else:
                number = before["number"] + 1
                connection.execute(update(revisions).where(revisions.c.group_id == group_id).values(is_current=False))
            value = dict(id=identifier("dr"), group_id=group_id, number=number, is_current=True, content_json=encoded(supplied), created_at=time.time())
            connection.execute(insert(revisions).values(**value))
            for position, (item, project_id) in enumerate(zip(supplied["members"], project_ids)):
                connection.execute(insert(members).values(revision_id=value["id"], project_id=project_id, position=position, **item))
            return {"id": group_id, "revision": public(value)}

    def decide(self, group_id, supplied):
        with self.store.engine.begin() as connection:
            rev = current_revision(connection, group_id)
            lock_requirements(connection, [item["requirement_id"] for item in member_rows(connection, rev["id"])])
            if current_revision(connection, group_id)["id"] != rev["id"]: raise Conflict("交付范围已变化，请重新载入")
            state = describe(connection, group_id)
            for item in state["members"]:
                guard_workspace(connection, item["requirement_id"])
                if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == item["requirement_id"], jobs.c.status.in_(ACTIVE)))).first(): raise Conflict("成员仍有执行或待核实")
            if not state["scope_ready"] or supplied["expected_scope"] != state["scope_digest"]: raise Conflict("请先取得全部必要项目分析，并重新检查范围")
            if supplied["decision"] == "APPROVED" and state["panorama"]["analysis"] and not state["panorama"]["analysis"]["code_current"]:
                raise Conflict("全景分析的代码基线已变化，请重新分析后再确认范围")
            if state["state"] != "ACTIVE": raise Conflict("归档交付不能修改范围决定")
            value = dict(id=identifier("dd"), group_id=group_id, revision_id=state["revision"]["id"], kind="SCOPE", input_digest=state["scope_digest"], payload_json=encoded(supplied), created_at=time.time())
            connection.execute(insert(decisions).values(**value)); return public(value)

    def reopen(self, group_id, supplied):
        with self.store.engine.begin() as connection:
            rev = current_revision(connection, group_id)
            lock_requirements(connection, [item["requirement_id"] for item in member_rows(connection, rev["id"])])
            if current_revision(connection, group_id)["id"] != rev["id"]: raise Conflict("交付范围已变化，请重新载入")
            for item in member_rows(connection, rev["id"]):
                guard_workspace(connection, item["requirement_id"], allow_archived=True)
                if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == item["requirement_id"], jobs.c.status.in_(ACTIVE)))).first(): raise Conflict("成员尚有执行或待核实任务")
            if connection.execute(select(groups.c.state).where(groups.c.id == group_id).with_for_update()).scalar_one() != "ARCHIVED": raise Conflict("交付尚未归档")
            connection.execute(delete(lifecycles).where(lifecycles.c.group_id == group_id))
            connection.execute(update(groups).where(groups.c.id == group_id).values(state="ACTIVE"))
            connection.execute(insert(decisions).values(id=identifier("dd"), group_id=group_id, revision_id=rev["id"], kind="REOPEN", input_digest=digest(supplied), payload_json=encoded(supplied), created_at=time.time()))
            return {"state": "ACTIVE"}

    def integrate(self, group_id, supplied):
        with self.store.engine.begin() as connection:
            rev = current_revision(connection, group_id)
            lock_requirements(connection, [item["requirement_id"] for item in member_rows(connection, rev["id"])])
            if current_revision(connection, group_id)["id"] != rev["id"]: raise Conflict("交付范围已变化，请重新载入")
            state = describe(connection, group_id)
            if state["state"] != "ACTIVE" or not state["scope_approved"] or not state["members_ready"] or supplied["expected_baseline"] != state["baseline_digest"]:
                raise Conflict("请先完成当前必要项目，并核对联调基线")
            if not supplied["actual"].strip() or not supplied["environment"].strip(): raise Conflict("请填写实际联调环境和结果")
            for member in state["members"]: guard_workspace(connection, member["requirement_id"])
            connection.execute(insert(decisions).values(id=identifier("dd"), group_id=group_id, revision_id=state["revision"]["id"],
                kind="INTEGRATION", input_digest=state["baseline_digest"], payload_json=encoded(supplied), created_at=time.time()))
            return {"saved": True}

    def enqueue(self, group_id, supplied, key):
        self.store.expire()
        with self.store.engine.begin() as connection:
            rev = current_revision(connection, group_id)
            member_list = member_rows(connection, rev["id"])
            ids = sorted(item["requirement_id"] for item in member_list)
            lock_requirements(connection, ids)
            if current_revision(connection, group_id)["id"] != rev["id"]: raise Conflict("交付范围已变化，请重新载入")
            if key:
                previous = connection.execute(select(jobs).join(attempts, attempts.c.job_id == jobs.c.id).where(and_(attempts.c.group_id == group_id, jobs.c.idempotency_key == key))).mappings().first()
                if previous:
                    if json.loads(previous["input_json"])["request"] != supplied: raise Conflict("相同请求标识对应不同交付操作")
                    return public(previous)
            state = describe(connection, group_id, full=True)
            from .panorama import KINDS, basis
            panorama = supplied["kind"] in KINDS
            expected_context = state["panorama"]["context_digest"] if panorama else state["context_digest"]
            if supplied["context_digest"] != expected_context: raise Conflict("项目版本或交付范围已变化，请重新载入")
            reconcile = supplied["kind"] == "reconcile"
            if panorama and (state["state"] != "ACTIVE" or any(not item["baseline"]["code_digest"] for item in state["members"])):
                raise Conflict("请先准备全部候选项目工作区，并使用活动交付")
            if supplied["kind"] == "archive" and state["state"] == "ARCHIVED": raise Conflict("交付已经归档")
            for identity in ids:
                reserved = connection.execute(select(reservations).where(reservations.c.requirement_id == identity)).mappings().first()
                if reserved and (not reconcile or reserved["group_id"] != group_id): raise Conflict("成员已被其他操作预留")
                statuses = ["QUEUED", "RUNNING"] if reconcile else ACTIVE
                if connection.execute(select(jobs.c.id).where(and_(jobs.c.requirement_id == identity, jobs.c.status.in_(statuses)))).first(): raise Conflict("成员尚有执行或待核实任务")
            if not reconcile and not panorama and not state["ready"]: raise Conflict("范围、必要项目、材料测试或反馈尚未完成，不能交付")
            payload_members = []
            for item in state["members"]:
                history = []
                if item["required"] and not reconcile and not panorama:
                    round_ids = select(material_rounds.c.id).where(material_rounds.c.requirement_id == item["requirement_id"])
                    from .store import revisions as requirement_revisions
                    history = {"requirement_revisions": [public(row) for row in connection.execute(select(requirement_revisions).where(requirement_revisions.c.requirement_id == item["requirement_id"])).mappings()],
                        "workflow_history": [describe_flow(connection, row) for row in connection.execute(select(flows).where(flows.c.requirement_id == item["requirement_id"])).mappings()],
                        "material_versions": [public(row) for row in connection.execute(select(documents).where(documents.c.round_id.in_(round_ids))).mappings()],
                        "material_records": [public(row) for row in connection.execute(select(material_records).where(material_records.c.round_id.in_(round_ids))).mappings()]}
                payload_members.append({**{name: item[name] for name in ("requirement_id", "project_id", "position", "required", "skip_reason", "workspace_path", "baseline", "requirement", "project")}, "history": history})
                if panorama:
                    from .profiles import binding
                    payload_members[-1]["profile"] = binding(connection, item["requirement_id"])
            payload = {"request": supplied, "group_id": group_id, "revision": state["revision"], "members": payload_members, "context_digest": state["context_digest"], "scope_decision": state["scope_decision"], "integration": state["integration"]}
            if panorama: payload["panorama_basis"] = basis(state["members"])
            elif not reconcile: payload["panorama_history"] = state["panorama"]["history"]
            if len(encoded(payload).encode()) > 16 * 1024 * 1024: raise Conflict("本轮历史元数据超过 16 MiB，请缩小范围或先整理历史")
            job = self.store.enqueue(ids[0], "delivery", payload, key, connection=connection, allow_reserved=reconcile)
            if reconcile: connection.execute(delete(reservations).where(reservations.c.group_id == group_id))
            for identity in ids: connection.execute(insert(reservations).values(requirement_id=identity, job_id=job["id"], group_id=group_id))
            connection.execute(insert(attempts).values(job_id=job["id"], group_id=group_id, revision_id=rev["id"], kind=supplied["kind"], input_digest=state["context_digest"], created_at=time.time()))
            return job


def complete(connection, job, status, result, error, data_dir):
    payload = json.loads(job["input_json"])
    ids = [item["requirement_id"] for item in payload["members"]]
    observations = result.get("delivery_observations", [])
    if not result.get("delivery_stopped") or {item["requirement_id"] for item in observations} != set(ids):
        return "UNKNOWN", error or "成员运行资源或代码状态尚未全部核实"
    if status == "SUCCEEDED" and job["cancel_requested"] and payload["request"]["kind"] in ("panorama", "panorama_check"):
        status, error = "CANCELED", "全景操作已取消，运行资源已停止，不发布分析或环境结论"
    from .workflow import invalidate
    for item in observations:
        invalidate(connection, item["requirement_id"], item["tree_digest"])
        connection.execute(update(workspaces).where(workspaces.c.requirement_id == item["requirement_id"]).values(environment=item.get("environment", "STOPPED")))
    if status == "SUCCEEDED":
        if payload["request"]["kind"] == "reconcile":
            related = select(attempts.c.job_id).where(attempts.c.group_id == payload["group_id"])
            connection.execute(update(jobs).where(and_(jobs.c.id.in_(related), jobs.c.status == "UNKNOWN")).values(status="CANCELED", error="跨项目运行资源已停止并核实", updated_at=time.time()))
        elif payload["request"]["kind"] in ("panorama", "panorama_check"):
            state = describe(connection, payload["group_id"], ignore_job=job["id"])
            output = result.get("panorama_output", {})
            from .panorama import basis
            if (state["state"] != "ACTIVE" or state["context_digest"] != payload["context_digest"] or basis(state["members"]) != payload["panorama_basis"] or output.get("basis") != payload["panorama_basis"] or
                any(item["tree_digest"] != next(member["code_digest"] for member in payload["panorama_basis"] if member["requirement_id"] == item["requirement_id"]) for item in observations)):
                status, error = "FAILED", "全景分析的需求、范围或代码基线已变化，结果不能生效"
            else:
                expected_model = payload["request"]["model"] if payload["request"]["kind"] == "panorama" else None
                from .panorama import Report
                valid = output.get("model") == expected_model and output.get("mode") == payload["request"]["kind"]
                coverage = output.get("coverage", [])
                valid = valid and len(coverage) == len(payload["members"]) and {item.get("project_id") for item in coverage} == {item["project_id"] for item in payload["members"]} and all(item.get("readonly") and item.get("readable") and item.get("samples_match") for item in coverage)
                if expected_model:
                    try:
                        report = Report.model_validate(output.get("report"))
                        valid = valid and all(item.file_sha256 for project in report.projects for item in project.evidence)
                        valid = valid and len(report.projects) == len(payload["members"]) and {item.project_id for item in report.projects} == {item["project_id"] for item in payload["members"]}
                    except ValueError: valid = False
                    valid = valid and output.get("model_execution") == "COMPLETED"
                else: valid = valid and output.get("model_execution") == "NOT_ATTEMPTED" and not output.get("report")
                if not valid: status, error = "FAILED", "全景结果与固定执行类型不一致"
                else:
                    connection.execute(insert(decisions).values(id=identifier("dd"), group_id=payload["group_id"], revision_id=payload["revision"]["id"],
                        kind="PANORAMA" if expected_model else "PANORAMA_CHECK", input_digest=digest(payload["panorama_basis"]),
                        payload_json=encoded({**output, "job_id": job["id"], "request": payload["request"], "actor": payload["request"]["actor"], "reason": payload["request"]["reason"]}), created_at=time.time()))
        else:
            state = describe(connection, payload["group_id"], ignore_job=job["id"])
            expected = {item["requirement_id"]: item["baseline"]["code_digest"] for item in payload["members"] if item["required"]}
            if not state["ready"] or state["context_digest"] != payload["context_digest"] or any(item["tree_digest"] != expected[item["requirement_id"]] for item in observations if item["requirement_id"] in expected):
                status, error = "FAILED", "必要项目的实际代码或验收结论已变化，不能交付"
            else:
                from .delivery_export import verify
                output = result["delivery_export"]
                from .workspace import WorkbenchError
                try: verify(data_dir, job["id"], output, payload["context_digest"])
                except WorkbenchError as failure:
                    connection.execute(delete(reservations).where(reservations.c.job_id == job["id"]))
                    return "FAILED", str(failure)
                connection.execute(insert(exports).values(id=identifier("pkg"), group_id=payload["group_id"], revision_id=payload["revision"]["id"],
                    job_id=job["id"], input_digest=payload["context_digest"], sha256=output["sha256"], size=output["size"], manifest_json=encoded(output["manifest"]), created_at=time.time()))
                if payload["request"]["kind"] == "archive":
                    connection.execute(update(groups).where(groups.c.id == payload["group_id"]).values(state="ARCHIVED"))
                    for identity in ids: connection.execute(insert(lifecycles).values(requirement_id=identity, group_id=payload["group_id"]))
    if status != "UNKNOWN": connection.execute(delete(reservations).where(reservations.c.job_id == job["id"]))
    return status, error
