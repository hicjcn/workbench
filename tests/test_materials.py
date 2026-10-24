"""Controlled reports exercise M4 business rules, never claim online inference."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import zipfile

from agent_workbench.execution import Executor
from agent_workbench.workspace import WorkbenchError, write_json
from agent_workbench import runtime
from unittest.mock import patch
import threading
from sqlalchemy import event
from agent_workbench.material_schemas import DOCUMENT_SCHEMAS
from agent_workbench.materials import Materials
from agent_workbench.store import documents, jobs, material_records
from agent_workbench.workflow import Workflow
from test_control_plane import ControlFixture


def sample_document(kind):
    if kind == "summary_design":
        return {"title": "受控概要设计", "markdown": "## 范围\n受控样例，未调用模型。\n## 接口与数据\n保持兼容。\n## 验证及回滚\n独立验收。"}
    value = {"title": "受控性能测试单" if kind == "performance_plan" else "受控功能测试单", "environment": "隔离样例环境", "cases": [
        {"id": "P1" if kind == "performance_plan" else "F1", "title": "必需用例", "preconditions": "服务就绪", "steps": ["发送样例请求"], "expected": "符合预期", "required": True}]}
    if kind == "performance_plan":
        value.update(workload="10 并发持续 1 分钟", metrics=[{"name": "P95", "target": "小于 200ms"}])
    return value


def evidence(**extra):
    return {"actor": "验收登记人", "occurred_at": datetime.now(timezone.utc).isoformat(),
        "reason": "受控验收记录，非外部实际审批", "evidence": "fixture://controlled-evidence", **extra}


class MaterialFixture(ControlFixture):
    def setUp(self):
        super().setUp()
        self.req = self.prepared()
        self.workspace = Path(self.req["workspace"]["path"])
        self.base = f"/api/v1/requirements/{self.req['id']}/materials"
        self.workflow = Workflow(self.store, self.settings.model)
        self.workflow.start(self.req["id"], ["opencode/glm-5.3-flash-free", "opencode/big-pickle"], "node --test", "受控准备", self.req["workspace"]["snapshot"]["tree_digest"])
        self.complete_code_flow()

    def complete_code_flow(self):
        for _ in range(12):
            flow = self.workflow.read(self.req["id"])["current"]
            key = flow["next_step"]
            if key is None:
                self.assertEqual(flow["state"], "COMPLETE"); return
            self.workflow.enqueue(self.req["id"], flow["id"], key, {"prompt": "", "decision": "APPROVED", "reason": "受控人工确认", "dispositions": {}}, None)
            job = self.store.claim("fixture")
            output = {"summary": "受控分析", "impacted_paths": [], "plan": "限定范围", "risks": []} if key == "analysis" else {"summary": "受控开发", "verification_notes": "另行验证"} if key == "development" else {"command": ["node", "--test"], "exit_code": 0} if key == "verification" else {"summary": "受控评审", "findings": []} if key.startswith("model_review_") else job["input"]["request"]
            self.store.finish(job["id"], "fixture", job["epoch"], "SUCCEEDED", {**self.runner.executor.observe(self.workspace), "workflow_output": output}, None)
        self.fail("Code flow did not complete")

    def state(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["current"]

    def start(self, key=None):
        flow = self.workflow.read(self.req["id"])["current"]
        response = self.client.post(self.base, json={"flow_id": flow["id"], "digest": flow["basis_digest"], "reason": "受控材料轮次"}, headers={"Idempotency-Key": key} if key else {})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def submit(self, step, data=None, request_key=None, context=None):
        state = self.state()
        return self.client.post(self.base + "/steps", json={"round_id": state["id"], "step_key": step,
            "context_digest": context or state["context_digest"], "data": data or {}}, headers={"Idempotency-Key": request_key} if request_key else {})

    def finish(self, output=None):
        job = self.store.claim("material_fixture")
        self.assertIsNotNone(job)
        step = job["input"]["step_key"]
        if step in DOCUMENT_SCHEMAS:
            result = {**self.runner.executor.observe(self.workspace), "material_output": output if output is not None else sample_document(step)}
        else:
            result = self.runner.executor.execute(job, lambda *_: None, __import__("threading").Event())
        self.store.finish(job["id"], "material_fixture", job["epoch"], "SUCCEEDED", result, None)
        return self.store.get(jobs, job["id"])

    def act(self, step, data=None):
        response = self.submit(step, data)
        self.assertEqual(response.status_code, 202, response.text)
        result = self.finish()
        self.assertEqual(result["status"], "SUCCEEDED", result["error"])
        return result

    def ready(self, performance="NOT_REQUIRED"):
        self.start()
        self.act("summary_design")
        self.act("supervisor_review", evidence(decision="APPROVED"))
        self.act("performance_decision", evidence(decision=performance))
        if performance != "UNDECIDED":
            self.act("test_plan")
        if performance == "REQUIRED":
            self.act("performance_plan")

    def execute_cases(self, status="PASS"):
        cases = self.state()["cases"]
        return self.act("test_execution", evidence(environment="受控环境", results=[
            {"version_id": case["version_id"], "case_id": case["id"], "status": status, "actual": "受控登记实际结果", "evidence": "fixture://case-evidence"} for case in cases]))


class MaterialTests(MaterialFixture):
    def test_documents_do_not_imply_supervisor_or_tests_passed(self):
        self.start(); self.act("summary_design")
        self.assertEqual(self.state()["stage"], "SUPERVISOR_REVIEW")
        self.assertEqual(self.submit("test_plan").status_code, 409)
        self.assertEqual(self.submit("acceptance", evidence()).status_code, 409)
        self.assertEqual(self.state()["test_status"], "NOT_RUN")

    def test_performance_undecided_cannot_be_silently_skipped(self):
        self.ready("UNDECIDED")
        self.assertEqual(self.state()["stage"], "PERFORMANCE_DECISION")
        self.assertEqual(self.submit("test_plan").status_code, 409)
        self.act("performance_decision", evidence(decision="REQUIRED"))
        self.act("test_plan")
        self.assertEqual(self.state()["stage"], "PERFORMANCE_PLAN")
        self.assertEqual(self.submit("test_execution", evidence(environment="x", results=[])).status_code, 409)
        self.submit("performance_plan")
        malformed = sample_document("performance_plan"); malformed["metrics"] = []
        self.assertEqual(self.finish(malformed)["status"], "FAILED")

    def test_required_performance_cases_block_acceptance_until_their_execution(self):
        self.ready("REQUIRED")
        first = self.state()["cases"][0]
        self.act("test_execution", evidence(environment="env", results=[{"version_id": first["version_id"], "case_id": first["id"], "status": "PASS", "actual": "受控通过", "evidence": "fixture://pass"}]))
        self.assertEqual(self.state()["test_status"], "NOT_RUN")
        self.assertEqual(self.submit("acceptance", evidence()).status_code, 409)
        self.execute_cases(); self.act("acceptance", evidence())
        self.assertEqual(self.state()["stage"], "ACCEPTED")

    def test_manual_revision_invalidates_dependent_records_preserving_original(self):
        self.ready(); self.execute_cases(); self.act("acceptance", evidence())
        old = self.state()["documents"]["summary_design"]
        self.act("edit_document", {"kind": "summary_design", "expected_version": old["id"], "content": {**old["content"], "markdown": "人工补充回滚方案"}, "reason": "完善主管材料"})
        state = self.state()
        self.assertEqual(state["stage"], "SUPERVISOR_REVIEW")
        self.assertFalse(state["accepted"])
        self.assertEqual(state["documents"]["test_plan"]["validity"], "STALE")
        self.assertEqual(state["documents"]["summary_design"]["number"], 2)
        original = self.client.get(self.base + "/versions/" + old["id"]).json()
        self.assertEqual(original["content"], old["content"])
        self.assertTrue(any(item["kind"] == "acceptance" and item["validity"] == "HISTORICAL" for item in state["records"]))

    def test_test_plan_edit_requires_results_for_new_version(self):
        self.ready(); self.execute_cases()
        old = self.state()["documents"]["test_plan"]
        changed = {**old["content"], "environment": "新测试环境"}
        self.act("edit_document", {"kind": "test_plan", "expected_version": old["id"], "content": changed, "reason": "更新前置"})
        self.assertEqual(self.state()["test_status"], "NOT_RUN")
        stale = evidence(environment="env", results=[{"version_id": old["id"], "case_id": "F1", "status": "PASS", "actual": "old", "evidence": "fixture://old"}])
        self.assertEqual(self.submit("test_execution", stale).status_code, 409)

    def test_failed_tests_and_missing_evidence_cannot_be_accepted(self):
        self.ready(); self.execute_cases("FAIL")
        self.assertEqual(self.state()["test_status"], "FAIL")
        self.assertEqual(self.submit("acceptance", evidence()).status_code, 409)
        bad = evidence(decision="APPROVED"); bad["evidence"] = " "
        self.assertEqual(self.submit("supervisor_review", bad).status_code, 409)

    def test_stale_forms_and_mismatched_idempotency_are_rejected(self):
        self.start(); old_context = self.state()["context_digest"]
        first = self.submit("summary_design", request_key="same")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.submit("summary_design", request_key="same").json()["id"], first.json()["id"])
        self.finish()
        self.assertEqual(self.submit("supervisor_review", evidence(decision="APPROVED"), context=old_context).status_code, 409)
        self.assertEqual(self.submit("test_plan", request_key="same").status_code, 409)

    def test_material_start_requires_self_review_and_is_idempotent(self):
        first = self.start("start-key")
        self.assertEqual(self.start("start-key")["id"], first["id"])
        self.workflow.start(self.req["id"], ["opencode/glm-5.3-flash-free", "opencode/big-pickle"], "node --test", "新开发", first["basis_digest"])
        current = self.workflow.read(self.req["id"])["current"]
        response = self.client.post(self.base, json={"flow_id": current["id"], "digest": current["basis_digest"], "reason": "不能跳过自审"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state()["stage"], "STALE")

    def test_document_comment_requires_new_document_without_resetting_code_review(self):
        self.ready()
        self.act("feedback", evidence(kind="DOCUMENT_COMMENT", scope_unchanged=False, new_requirement=None))
        self.assertEqual(self.state()["stage"], "SUMMARY_DESIGN")
        self.assertEqual(self.workflow.read(self.req["id"])["current"]["state"], "COMPLETE")
        self.act("summary_design")
        self.assertEqual(self.state()["stage"], "SUPERVISOR_REVIEW")

    def test_bug_opens_explicit_fix_round_and_blocks_acceptance_until_resolved(self):
        self.ready(); original = self.workflow.read(self.req["id"])["current"]
        self.act("feedback", evidence(kind="BUG", scope_unchanged=True, new_requirement=None))
        flow = self.workflow.read(self.req["id"])["current"]
        self.assertNotEqual(flow["id"], original["id"])
        self.assertEqual(flow["next_step"], "development")
        self.assertEqual(flow["config"]["carried_scope"]["flow_id"], original["id"])
        self.assertEqual(self.state()["stage"], "STALE")
        self.assertFalse(self.runner.run_once())
        self.complete_code_flow(); self.ready(); self.execute_cases()
        self.assertEqual(self.state()["stage"], "FEEDBACK_RESOLUTION")
        feedback_id = self.state()["outstanding_feedback"][0]["id"]
        self.assertEqual(self.submit("acceptance", evidence()).status_code, 409)
        self.act("resolve_feedback", evidence(feedback_id=feedback_id, decision="RESOLVED"))
        self.act("acceptance", evidence())
        self.assertTrue(self.state()["accepted"])

    def test_requirement_and_architecture_feedback_return_to_analysis_with_new_revision(self):
        self.ready()
        self.act("feedback", evidence(kind="REQUIREMENT_CHANGE", scope_unchanged=False,
            new_requirement={"title": "新业务需求", "description": "新背景", "acceptance": "新验收"}))
        state = self.workflow.read(self.req["id"])
        self.assertEqual(state["revision"]["number"], 2)
        self.assertEqual(state["current"]["next_step"], "analysis")
        self.complete_code_flow(); self.ready()
        self.act("feedback", evidence(kind="ARCHITECTURE_CHANGE", scope_unchanged=False, new_requirement=None))
        state = self.workflow.read(self.req["id"])
        self.assertEqual(state["revision"]["number"], 3)
        self.assertIn("ARCHITECTURE_CHANGE", state["revision"]["reason"])
        self.assertEqual(state["current"]["next_step"], "analysis")

    def test_live_code_change_cannot_be_approved_by_queued_material_job(self):
        self.ready()
        response = self.submit("supervisor_review", evidence(decision="APPROVED"))
        self.assertEqual(response.status_code, 202)
        (self.workspace / "repo/main.txt").write_text("changed after request\n")
        self.runner.run_once()
        self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "FAILED")
        self.assertEqual(self.state()["stage"], "STALE")

    def test_export_manifest_distinguishes_skip_and_preserves_old_package(self):
        self.ready(); self.execute_cases(); self.act("acceptance", evidence()); self.act("export")
        item = self.state()["exports"][0]
        response = self.client.get(self.base + "/exports/" + item["id"])
        self.assertEqual(response.status_code, 200)
        original = response.content
        self.assertEqual(hashlib.sha256(original).hexdigest(), item["sha256"])
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["status_at_export"], "ACCEPTED")
            self.assertTrue(manifest["performance_skip_reason"])
            self.assertEqual(manifest["performance_decision"], "NOT_REQUIRED")
            self.assertFalse(any(name.startswith("performance_plan/") for name in archive.namelist()))
        self.act("summary_design")
        self.assertEqual(self.client.get(self.base + "/exports/" + item["id"]).content, original)
        (self.data / "exports" / (item["job_id"] + ".zip")).write_bytes(b"tampered")
        self.assertEqual(self.client.get(self.base + "/exports/" + item["id"]).status_code, 422)

    def test_material_routes_require_user_authentication(self):
        self.start()
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get(self.base).status_code, 401)
        self.assertEqual(self.client.post(self.base, json={}).status_code, 401)

    def test_failed_feedback_rolls_back_record_and_revision_together(self):
        self.ready()
        before = self.workflow.read(self.req["id"])["revision"]
        count = len(self.state()["records"])
        response = self.submit("feedback", evidence(kind="REQUIREMENT_CHANGE", scope_unchanged=False, new_requirement=before["content"]))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.finish()["status"], "FAILED")
        self.assertEqual(len(self.state()["records"]), count)
        self.assertEqual(self.workflow.read(self.req["id"])["revision"]["id"], before["id"])

    def test_invalid_timeout_cannot_leave_a_submitted_agent_process(self):
        write_json(self.workspace / "runtime.json", {"container": "fixture"})
        write_json(self.workspace / "agent-home/.config/opencode/opencode.json", runtime.model_config(self.settings.model))
        with patch.dict("os.environ", {"AWB_TASK_TIMEOUT": "nan"}), patch("agent_workbench.execution.subprocess.Popen") as launch:
            with self.assertRaises(WorkbenchError):
                self.runner.executor.task({"requirement": self.req, "input": {"prompt": "fixture"}}, self.workspace, lambda *_: None, threading.Event())
            launch.assert_not_called()

    def test_artifact_and_job_result_commit_together_after_nested_savepoint(self):
        self.start(); self.submit("summary_design")
        job = self.store.claim("atomic-fixture")
        def abort_job_update(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE jobs"):
                raise RuntimeError("controlled failure after artifact savepoint")
        event.listen(self.store.engine, "before_cursor_execute", abort_job_update)
        try:
            with self.assertRaises(RuntimeError):
                self.store.finish(job["id"], "atomic-fixture", job["epoch"], "SUCCEEDED",
                    {**self.runner.executor.observe(self.workspace), "material_output": sample_document("summary_design")}, None)
        finally:
            event.remove(self.store.engine, "before_cursor_execute", abort_job_update)
        self.assertIsNone(self.state()["documents"]["summary_design"])
        self.assertEqual(self.store.get(jobs, job["id"])["status"], "RUNNING")
