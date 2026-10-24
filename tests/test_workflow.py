"""Controlled reports verify gates; these fixtures are not online model evidence."""
import io
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch

from sqlalchemy import update

from agent_workbench import runtime
from agent_workbench.execution import Unknown
from agent_workbench.store import jobs
from agent_workbench.workflow_execution import execute_step, verify_command
from agent_workbench.workspace import WorkbenchError, snapshot, write_json
from test_control_plane import ControlFixture


class WorkflowTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.req = self.prepared()
        self.base = f"/api/v1/requirements/{self.req['id']}"
        self.workspace = Path(self.req["workspace"]["path"])

    def state(self):
        response = self.client.get(self.base + "/workflow")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def start(self, models=None):
        response = self.client.post(self.base + "/workflow", json={
            "review_models": models or ["opencode/glm-5.3-flash-free", "opencode/big-pickle"],
            "verification_command": "node --test", "reason": "受控流程测试",
            "digest": snapshot(self.workspace / "repo")["tree_digest"]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def enqueue(self, key, request_key=None, **supplied):
        return self.client.post(self.base + "/workflow/steps", json={
            "flow_id": self.state()["current"]["id"], "step_key": key, **supplied},
            headers={"Idempotency-Key": request_key} if request_key else {})

    def finish(self, output=None, status="SUCCEEDED"):
        job = self.store.claim("controlled_fixture")
        self.assertIsNotNone(job)
        key = job["input"]["step_key"]
        if output is None:
            output = ({"summary": "受控分析报告", "impacted_paths": ["main.txt"], "plan": "明确范围", "risks": []}
                if key == "analysis" else {"summary": "受控开发报告", "verification_notes": "稍后执行独立验证"}
                if key == "development" else {"command": ["node", "--test"], "exit_code": 0}
                if key == "verification" else {"summary": "受控模型报告", "findings": []}
                if key.startswith("model_review_") else job["input"]["request"])
        result = {**self.runner.executor.observe(self.workspace), "workflow_output": output}
        self.store.finish(job["id"], "controlled_fixture", job["epoch"], status, result, None)
        return self.store.get(jobs, job["id"])

    def advance(self, through):
        for key in ["analysis", "scope", "development", "verification", "model_review_0", "model_review_1", "resolution", "self_review"]:
            self.assertEqual(self.enqueue(key, decision="APPROVED", reason="受控人工确认").status_code, 202)
            self.assertEqual(self.finish()["status"], "SUCCEEDED")
            if key == through:
                break

    def test_ordering_and_old_endpoints_cannot_bypass_human_gates(self):
        self.start()
        self.assertEqual(self.enqueue("development").status_code, 409)
        self.assertEqual(self.action(self.req, "task", prompt="bypass").status_code, 409)
        self.assertEqual(self.client.post(self.base + "/reviews", json={"digest": self.req["workspace"]["snapshot"]["tree_digest"], "decision": "APPROVED"}).status_code, 409)
        self.advance("analysis")
        self.assertEqual(self.enqueue("scope", decision="APPROVED").status_code, 409)
        self.assertEqual(self.enqueue("scope", decision="APPROVED", reason="已检查方案").status_code, 202)
        self.assertEqual(self.state()["current"]["next_step"], "scope")
        self.finish()
        self.assertEqual(self.state()["current"]["next_step"], "development")

    def test_two_distinct_models_share_baseline_and_final_confirmation(self):
        self.start()
        self.advance("model_review_1")
        for key in ["model_review_0", "model_review_1"]:
            job = next(item for item in self.store.list(jobs) if item["input"].get("step_key") == key)
            self.assertEqual(job["input"]["bundle"]["code_digest"], self.state()["current"]["basis_digest"])
            self.assertFalse(any(name.startswith("model_review_") for name in job["input"]["bundle"]["upstream"]))
        self.assertNotEqual(*self.state()["current"]["config"]["review_models"])
        self.assertEqual(self.enqueue("self_review", decision="APPROVED", reason="不能跳过问题处理").status_code, 409)
        for key in ["resolution", "self_review"]:
            self.enqueue(key, decision="APPROVED", reason="已核查报告")
            self.finish()
        self.assertEqual(self.state()["current"]["state"], "COMPLETE")
        self.assertEqual(self.detail(self.req)["workspace"]["approval"], "APPROVED")

    def test_repeated_step_key_is_same_intent_even_after_position_moves(self):
        self.start()
        first = self.enqueue("analysis", "intent")
        self.assertEqual(self.enqueue("analysis", "intent").json()["id"], first.json()["id"])
        self.finish()
        self.assertEqual(self.enqueue("analysis", "intent").json()["id"], first.json()["id"])
        self.assertEqual(self.enqueue("analysis", "intent", prompt="different").status_code, 409)
        self.assertEqual(len(self.state()["current"]["attempts"]), 1)

    def test_requirement_revision_is_optimistic_and_invalidates_old_flow(self):
        self.start(); self.advance("analysis")
        old = self.state()["revision"]
        body = {"expected_revision": old["id"], "title": "新需求", "description": "", "acceptance": "更新行为", "reason": "业务变更"}
        self.assertEqual(self.client.post(self.base + "/revisions", json=body).status_code, 201)
        self.assertEqual(self.client.post(self.base + "/revisions", json=body).status_code, 409)
        current = self.state()
        self.assertEqual(current["revision"]["number"], 2)
        self.assertEqual(current["revisions"][1]["content"]["title"], old["content"]["title"])
        self.assertEqual(current["current"]["state"], "STALE")
        self.assertEqual(len(current["current"]["attempts"]), 1)
        self.assertEqual(self.enqueue("scope", decision="APPROVED", reason="过期").status_code, 409)

    def test_code_changes_invalidate_idle_flow_and_keep_reports(self):
        self.start(); self.advance("analysis")
        (self.workspace / "repo/main.txt").write_text("external edit\n")
        self.runner.watch_once()
        self.assertEqual(self.state()["current"]["state"], "STALE")
        self.assertTrue(self.state()["current"]["nodes"][0]["output"])

    def test_failed_or_malformed_reports_never_advance_or_auto_retry(self):
        self.start(); self.enqueue("analysis")
        self.assertEqual(self.finish({"summary": "缺少方案"})["status"], "FAILED")
        self.assertEqual(self.state()["current"]["next_step"], "analysis")
        self.assertFalse(self.runner.run_once())
        self.enqueue("analysis", "explicit-retry")
        self.finish()
        self.assertEqual(len(self.state()["current"]["attempts"]), 2)

    def test_blocking_findings_require_per_finding_disposition(self):
        self.start(); self.advance("verification")
        self.enqueue("model_review_0")
        self.finish({"summary": "受控问题", "findings": [{"severity": "MAJOR", "path": "main.txt", "line": 1, "description": "受控缺陷", "recommendation": "检查"}]})
        self.enqueue("model_review_1"); self.finish()
        self.assertEqual(self.enqueue("resolution", decision="APPROVED", reason="概括说明不能代替逐项处理").status_code, 409)
        self.assertEqual(self.enqueue("resolution", decision="APPROVED", reason="已核查", dispositions={"model_review_0:0": {"decision": "FALSE_POSITIVE", "reason": "核对调用方证明不触发"}}).status_code, 202)
        self.finish()
        self.assertEqual(self.state()["current"]["next_step"], "self_review")

    def test_lease_loss_blocks_new_round_until_reconcile(self):
        self.start(); self.enqueue("analysis")
        job = self.store.claim("lost")
        with self.store.engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(lease_until=0))
        self.store.expire()
        self.assertEqual(self.state()["current"]["state"], "STALE")
        self.assertEqual(self.enqueue("analysis").status_code, 409)
        self.action(self.req, "reconcile"); self.runner.run_once()
        self.start()
        self.assertEqual(len(self.state()["history"]), 2)

    def test_development_updates_baseline_but_failed_verification_cannot_pass(self):
        self.start(); self.advance("scope")
        self.enqueue("development")
        (self.workspace / "repo/main.txt").write_text("controlled development\n")
        self.finish()
        self.assertEqual(self.state()["current"]["state"], "ACTIVE")
        self.assertEqual(self.state()["current"]["basis_digest"], snapshot(self.workspace / "repo")["tree_digest"])
        self.enqueue("verification")
        self.assertEqual(self.finish({"command": ["node", "--test"], "exit_code": 1})["status"], "FAILED")
        self.assertEqual(self.enqueue("model_review_0").status_code, 409)

    def test_isolated_roles_use_distinct_homes_and_read_only_same_code(self):
        self.start(); self.advance("verification")
        homes, digests = [], []
        def start(workspace, image, ca, **options):
            self.assertTrue(options["read_only_source"])
            homes.append(workspace / "agent-home")
            digests.append(snapshot(workspace / "repo")["tree_digest"])
            write_json(workspace / "runtime.json", {})
        def task(job, isolated, emit, canceled, **options):
            self.assertTrue(options["read_only"])
            return {"agent_text": '{"summary":"受控独立报告","findings":[]}'}
        def stop(workspace):
            (workspace / "runtime.json").unlink()
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.runtime.start", side_effect=start), patch("agent_workbench.runtime.probe", return_value={"health": {"healthy": True}}), patch("agent_workbench.runtime.stop", side_effect=stop), patch.object(self.runner.executor, "task", side_effect=task):
            for key in ("model_review_0", "model_review_1"):
                self.enqueue(key)
                self.runner.run_once()
                self.assertEqual(self.detail(self.req)["jobs"][0]["status"], "SUCCEEDED")
        self.assertNotEqual(homes[0], homes[1])
        self.assertEqual(digests[0], digests[1])

    def test_actual_verification_exit_code_is_checked(self):
        self.start(); self.advance("development"); self.enqueue("verification")
        job = self.store.claim("fixture")
        write_json(self.workspace / "runtime.json", {"container": "fixture"})
        process = Mock(stdout=io.StringIO("fixture test failed\n"), returncode=1)
        process.poll.return_value = 1
        with patch("agent_workbench.workflow_execution.subprocess.Popen", return_value=process):
            with self.assertRaises(WorkbenchError):
                verify_command(self.runner.executor, job, self.workspace, lambda *_: None, threading.Event())

    def test_non_distinct_models_or_charged_profiles_are_rejected(self):
        for models in [["opencode/big-pickle"] * 2, ["opencode/big-pickle", "openrouter/vendor/paid"]]:
            response = self.client.post(self.base + "/workflow", json={"review_models": models,
                "verification_command": "node --test", "reason": "invalid", "digest": self.req["workspace"]["snapshot"]["tree_digest"]})
            self.assertIn(response.status_code, (409, 422))

    def test_history_retains_original_inputs_after_new_round(self):
        first = self.start(); self.advance("analysis")
        self.start()
        archived = self.client.get(self.base + "/workflow/runs/" + first["id"])
        self.assertEqual(archived.status_code, 200)
        value = archived.json()
        self.assertEqual(value["state"], "SUPERSEDED")
        self.assertEqual(value["revision"]["number"], 1)
        self.assertEqual(value["nodes"][0]["output"]["summary"], "受控分析报告")
        self.assertEqual(value["attempts"][0]["input"]["bundle"]["requirement_revision"]["number"], 1)

    def test_failed_read_only_cleanup_is_unknown_until_reconcile(self):
        self.start(); self.enqueue("analysis")
        def start(workspace, *args, **options):
            write_json(workspace / "runtime.json", {})
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.runtime.start", side_effect=start), patch("agent_workbench.runtime.probe", return_value={"health": {"healthy": True}}), patch.object(self.runner.executor, "task", return_value={"agent_text": '{"summary":"report","impacted_paths":[],"plan":"plan","risks":[]}'}), patch("agent_workbench.runtime.stop", side_effect=WorkbenchError("cannot confirm stop")):
            self.runner.run_once()
        self.assertEqual(self.detail(self.req)["jobs"][0]["status"], "UNKNOWN")
        self.assertEqual(self.enqueue("analysis").status_code, 409)
        journals = list((self.data / "attempts").glob("*/*/*/runtime.json"))
        self.assertEqual(len(journals), 1)
        self.action(self.req, "reconcile")
        with patch("agent_workbench.runtime.stop", side_effect=lambda workspace: (workspace / "runtime.json").unlink()) as stop:
            self.runner.run_once()
            stop.assert_called_once_with(journals[0].parent)
        self.assertEqual(self.detail(self.req)["jobs"][0]["status"], "SUCCEEDED")

    def test_progress_text_does_not_become_the_final_structured_report(self):
        write_json(self.workspace / "runtime.json", {"container": "fixture"})
        write_json(self.workspace / "agent-home/.config/opencode/opencode.json", runtime.model_config(self.settings.model))
        final = '{"summary":"final report"}'
        events = [{"type": "text", "part": {"text": "Using Superpowers for this task"}},
            {"type": "text", "part": {"text": final}}, {"type": "step_finish", "part": {"reason": "stop"}}]
        process = Mock(stdout=io.StringIO("".join(json.dumps(event) + "\n" for event in events)), stderr=io.StringIO(), returncode=0)
        process.poll.return_value = 0
        recorded = []
        observed = self.runner.executor.observe(self.workspace)
        with patch("agent_workbench.execution.subprocess.Popen", return_value=process), patch.object(self.runner.executor, "observe", return_value=observed):
            result = self.runner.executor.task({"requirement": self.req, "input": {"prompt": "fixture"}}, self.workspace,
                lambda kind, payload: recorded.append(payload), threading.Event(), collect_text=True)
        self.assertEqual(result["agent_text"], final)
        self.assertTrue(any("Using Superpowers" in item["text"] for item in recorded))
        process.stdin.write.assert_called_once()
        self.assertIn("fixture", process.stdin.write.call_args.args[0])
