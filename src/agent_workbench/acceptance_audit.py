"""Read-only evidence checklist: completed records alone never prove online execution."""
import hashlib
import json

from sqlalchemy import select

from . import materials
from .store import (flows, jobs, events, workspaces, material_rounds, material_records, public, requirements,
    delivery_members, delivery_revisions, delivery_decisions, reservations)
from .workflow import describe as describe_flow
from .opencode import SKILLS, skill_usage



def model_evidence(job, rows, expected_model):
    ended, text_seen, errored = False, False, False
    skills = set(); checksum = hashlib.sha256(); count = 0
    for row in rows:
        payload = row["payload"] if "payload" in row else json.loads(row["payload_json"])
        checksum.update(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()); count += 1
        if row["kind"] == "model_completion" and payload.get("schema") == "workbench-model-completion/v1" and payload.get("model") == expected_model and payload.get("completed") is True:
            text_seen |= payload.get("text_seen") is True
            if isinstance(payload.get("skills"), list): skills.update(skill for skill in payload["skills"] if isinstance(skill, str) and skill in SKILLS)
        if row["kind"] != "agent": continue
        try: value = json.loads(payload.get("text", ""))
        except (ValueError, TypeError): continue
        if not isinstance(value, dict): continue
        part = value.get("part") or {}
        if not isinstance(part, dict): continue
        ended |= value.get("type") == "step_finish" and part.get("reason") == "stop"
        text_seen |= value.get("type") == "text" and isinstance(part.get("text"), str) and bool(part["text"].strip())
        errored |= value.get("type") == "error"
        skills.update(skill_usage(value))
    result = job.get("result") or {}
    actual_model = result.get("model") or (result.get("panorama_output") or {}).get("model")
    return {"job_id": job["id"], "model": actual_model, "ended": bool(ended), "text_seen": bool(text_seen),
        "recorded": job["status"] == "SUCCEEDED" and actual_model == expected_model and ended and text_seen and not errored,
        "event_count": count, "event_digest": checksum.hexdigest(), "skills": sorted(skills)}


def inspect(store, requirement_id):
    store.get(requirements, requirement_id)
    with store.engine.connect() as connection:
        workspace = public(connection.execute(select(workspaces).where(workspaces.c.requirement_id == requirement_id)).mappings().one())
        histories = [dict(row) for row in connection.execute(select(flows).where(flows.c.requirement_id == requirement_id).order_by(flows.c.created_at)).mappings()]
        parent = next((row for row in histories if row["is_current"]), None)
        flow = describe_flow(connection, parent) if parent else None
        round_row = connection.execute(select(material_rounds).where((material_rounds.c.requirement_id == requirement_id) & material_rounds.c.is_current.is_(True))).mappings().first()
        material = materials.describe(connection, round_row) if round_row else None
        all_jobs = {row["id"]: public(row) for row in connection.execute(select(jobs).where(jobs.c.requirement_id == requirement_id)).mappings()}
        proofs = {}
        def proof(identity, model):
            if identity not in all_jobs:
                saved = connection.execute(select(jobs).where(jobs.c.id == identity)).mappings().first()
                if not saved: return None
                all_jobs[identity] = public(saved)
            key = (identity, model)
            if key not in proofs:
                rows = connection.execute(select(events).where(events.c.job_id == identity).order_by(events.c.id)).mappings()
                proofs[key] = model_evidence(all_jobs[identity], rows, model)
            return proofs[key]
        nodes = {node["key"]: node for node in flow["nodes"]} if flow else {}
        models = []
        for key, node in nodes.items():
            if node["model"] and node["job_id"]: models.append({"step": key, **(proof(node["job_id"], node["model"]) or {})})
        analysis = next((item for item in models if item["step"] == "analysis"), None)
        if flow and flow["config"].get("carried_scope"):
            original = next((row for row in histories if row["id"] == flow["config"]["carried_scope"]["flow_id"]), None)
            visited = set()
            while original and original["id"] not in visited:
                visited.add(original["id"])
                source = describe_flow(connection, original)
                node = next((node for node in source["nodes"] if node["key"] == "analysis"), None)
                if node and node["job_id"]:
                    analysis = proof(node["job_id"], source["config"]["development_model"]); break
                original = next((row for row in histories if row["id"] == source["config"].get("carried_scope", {}).get("flow_id")), None)
        development = next((item for item in models if item["step"] == "development"), None)
        reviews = [item for item in models if item["step"].startswith("model_review_")]
        document_proofs = []
        if material:
            for kind, document in material["documents"].items():
                if document and document["validity"] == "CURRENT":
                    generated = next((item for item in reversed(material["version_history"]) if item["kind"] == kind and item.get("provenance") == "AGENT"), None)
                    # Human revisions remain valid; require a model-origin version in this round.
                    if generated:
                        document_proofs.append({"kind": kind, **(proof(generated["job_id"], flow["config"]["development_model"]) or {})})
        related = [public(row) for row in connection.execute(select(material_records).join(material_rounds, material_rounds.c.id == material_records.c.round_id)
            .where((material_rounds.c.requirement_id == requirement_id) & (material_records.c.kind == "feedback"))).mappings()]
        feedback = [item for item in related if item["payload"].get("kind") in ("BUG", "REQUIREMENT_CHANGE", "ARCHITECTURE_CHANGE")]
        checks = []
        def add(key, title, passed, evidence=None, category="record"):
            checks.append({"key": key, "title": title, "status": "RECORDED" if passed else "PENDING", "category": category, "evidence": evidence or []})
        add("workspace", "独立工作区已准备", bool(workspace["snapshot"]))
        linked = connection.execute(select(delivery_revisions.c.id).join(delivery_members, delivery_members.c.revision_id == delivery_revisions.c.id)
            .where((delivery_members.c.requirement_id == requirement_id) & delivery_revisions.c.is_current.is_(True))).scalars().all()
        if linked:
            joint = []
            for revision_id in linked:
                saved = public(connection.execute(select(delivery_decisions).where((delivery_decisions.c.revision_id == revision_id) & (delivery_decisions.c.kind == "PANORAMA"))
                    .order_by(delivery_decisions.c.created_at.desc()).limit(1)).mappings().first())
                if saved: joint.append(proof(saved["payload"]["job_id"], saved["payload"]["model"]))
            add("panorama", "联合分析具有模型结束事件", len(joint) == len(linked) and all(item and item["recorded"] for item in joint), [item for item in joint if item], "model")
        add("analysis", "分析任务具有模型结束事件", bool(analysis and analysis.get("recorded")), [analysis] if analysis else [], "model")
        add("development", "开发任务具有模型结束事件", bool(development and development.get("recorded")), [development] if development else [], "model")
        add("superpowers", "开发中有 Superpowers 技能读取或调用记录", bool(development and development.get("skills")), [development] if development else [], "tool")
        verification = nodes.get("verification") or {}
        add("verification", "当前代码验证命令成功", verification.get("status") == "SUCCEEDED" and (verification.get("output") or {}).get("exit_code") == 0,
            [verification["job_id"]] if verification.get("job_id") else [])
        add("reviews", "至少两个不同模型的评审结束事件", len(reviews) >= 2 and all(p.get("recorded") for p in reviews) and len({p.get("model") for p in reviews}) == len(reviews), reviews, "model")
        add("self_review", "当前代码已登记人工自审", bool(flow and flow["state"] == "COMPLETE" and workspace["approval"] == "APPROVED"), category="human_registration")
        needed_documents = {"summary_design", "test_plan"} | ({"performance_plan"} if material and material["performance_decision"] == "REQUIRED" else set())
        add("documents", "当前材料有模型生成的执行证据", needed_documents <= {p["kind"] for p in document_proofs if p.get("recorded")}, document_proofs, "model")
        add("supervisor", "当前概要设计已登记主管通过", bool(material and material["supervisor_valid"] and material["supervisor"]["payload"]["decision"] == "APPROVED"), category="human_registration")
        add("tests", "必需用例已逐项登记实际结果", bool(material and material["test_status"] == "PASS"), category="human_registration")
        add("feedback", "至少完成一次反馈后的复评复测", bool(feedback and len(histories) >= 2 and material and material["accepted"] and not material["outstanding_feedback"]), [item["id"] for item in feedback], "human_registration")
        add("materials", "当前材料阶段已登记验收", bool(material and material["accepted"]), category="human_registration")
        active = [job["id"] for job in all_jobs.values() if job["status"] in ("QUEUED", "RUNNING", "UNKNOWN")]
        active += list(connection.execute(select(reservations.c.job_id).where(reservations.c.requirement_id == requirement_id)).scalars())
        active = sorted(set(active))
        complete = all(item["status"] == "RECORDED" for item in checks) and not active
        return {"schema": "workbench-acceptance-audit/v1", "requirement_id": requirement_id,
            "status": "EVIDENCE_RECORDED_REQUIRES_REVIEW" if complete else "INCOMPLETE", "checks": checks, "active_jobs": active,
            "code_digest": workspace["snapshot"]["tree_digest"] if workspace["snapshot"] else None, "flow_id": flow["id"] if flow else None,
            "manual_identity_verified": False, "provider_identity_verified": False,
            "note": "只核对工作台保存的任务与人工登记证据；不独立认证模型服务或主管身份，不替代业务验收。"}
