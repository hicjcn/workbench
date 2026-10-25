"""Controlled report tests; the PoC separately verifies real multi-repository mounts."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import update

from agent_workbench import panorama, runtime
from agent_workbench.delivery import Delivery
from agent_workbench.execution import Unknown
from agent_workbench.store import jobs, reservations, requirements, workspaces
from agent_workbench.workspace import WorkbenchError, create_workspace, snapshot
from test_control_plane import ControlFixture
from test_delivery import DeliveryFixture
from test_materials import evidence


def controlled_output(job):
    payload = job["input"]
    model = payload["request"]["model"] if payload["request"]["kind"] == "panorama" else None
    output = {"mode": payload["request"]["kind"], "basis": payload["panorama_basis"], "image_id": "controlled-image",
        "model": model, "model_execution": "COMPLETED" if model else "NOT_ATTEMPTED",
        "coverage": [{"project_id": member["project_id"], "readonly": True, "readable": True, "samples_match": True} for member in payload["members"]]}
    if model:
        bundle = [{"project_id": m["project_id"], "files": snapshot(Path(m["workspace_path"]) / "repo")["files"]} for m in payload["members"]]
        value = {"summary": "Controlled joint analysis; no model inference", "projects": [{"project_id": m["project_id"], "impact": "CHANGE", "summary": "Controlled candidate",
            "evidence": [{"path": "main.txt", "line": 1, "explanation": "Controlled code reference"}]} for m in payload["members"]], "relations": [], "risks": [], "open_questions": []}
        output["report"] = panorama.validate_report(value, bundle)
    return output


class PanoramaTests(ControlFixture):
    def setUp(self):
        super().setUp(); self.first = self.prepared(); self.second = self.prepared()
        self.service = Delivery(self.store)
        self.group = self.service.save({"title": "Candidate projects", "description": "Controlled requirement", "members": [{"requirement_id": r["id"], "required": True, "skip_reason": ""} for r in (self.first, self.second)],
            "actor": "fixture", "reason": "candidate set", "expected_revision": None})["id"]
        self.url = f"/api/v1/deliveries/{self.group}/panorama"

    def state(self): return self.service.read(self.group)

    def run_request(self, kind="panorama_check", key=None, context=None, **extra):
        payload = {**evidence(kind=kind, context_digest=context or self.state()["panorama"]["context_digest"], model="opencode/glm-5.3-flash-free", prompt=""), **extra}
        return self.client.post(self.url, json=payload, headers={"Idempotency-Key": key} if key else {})

    def complete_controlled(self, kind="panorama_check", transform=None):
        response = self.run_request(kind); self.assertEqual(response.status_code, 202, response.text)
        def fake(executor, job, emit, canceled):
            output = controlled_output(job)
            return transform(output) if transform else output
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.panorama.execute", side_effect=fake): self.runner.run_once()
        return self.store.get(jobs, response.json()["id"])

    def test_auth_and_supported_free_model_only(self):
        self.assertEqual(self.run_request(model="paid/model").status_code, 422)
        self.client.headers.pop("Authorization")
        self.assertEqual(self.run_request().status_code, 401)

    def test_environment_check_is_not_an_analysis_or_scope_confirmation(self):
        before = self.state()["scope_digest"]
        result = self.complete_controlled()
        self.assertEqual(result["status"], "SUCCEEDED", result.get("error"))
        state = self.state(); self.assertIsNone(state["panorama"]["analysis"])
        self.assertEqual(state["panorama"]["environment"]["payload"]["model_execution"], "NOT_ATTEMPTED")
        self.assertEqual(state["scope_digest"], before); self.assertFalse(state["scope_ready"])

    def test_all_candidates_reserved_idempotently_and_queued_cancel_releases(self):
        first = self.run_request(key="same").json()
        self.assertEqual(self.client.post(self.url, json=first["input"]["request"], headers={"Idempotency-Key": "same"}).json()["id"], first["id"])
        self.assertEqual(len(self.store.list(reservations)), 2)
        self.assertEqual(self.action(self.second, "refresh").status_code, 409)
        self.assertEqual(self.run_request(key="another").status_code, 409)
        self.client.post(f"/api/v1/jobs/{first['id']}/cancel", json={})
        self.assertFalse(self.store.list(reservations))

    def test_stale_candidate_code_rejects_old_input_context(self):
        context = self.state()["panorama"]["context_digest"]
        path = Path(self.second["workspace"]["path"]); (path / "repo/main.txt").write_text("changed\n")
        self.action(self.second, "refresh"); self.runner.run_once()
        self.assertEqual(self.run_request(context=context).status_code, 409)

    def test_unprepared_optional_candidate_also_blocks_joint_analysis(self):
        third = self.requirement(); state = self.state()
        self.service.save({**state["revision"]["content"], "expected_revision": state["revision"]["id"],
            "members": state["revision"]["content"]["members"] + [{"requirement_id": third["id"], "required": False, "skip_reason": "candidate only"}]}, self.group)
        self.assertEqual(self.run_request().status_code, 409)

    def test_source_changes_after_enqueue_fail_without_model_execution(self):
        response = self.run_request("panorama")
        path = Path(self.second["workspace"]["path"]); (path / "repo/main.txt").write_text("external edit\n")
        with patch("agent_workbench.resources.inventory", return_value=[]), patch.object(self.runner.executor, "task") as task:
            self.runner.run_once(); task.assert_not_called()
        self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "FAILED")
        self.assertFalse(self.store.list(reservations)); self.assertFalse(self.state()["panorama"]["history"])

    def test_unknown_cleanup_keeps_every_reservation_until_group_reconcile(self):
        response = self.run_request()
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.panorama.execute", side_effect=Unknown("controlled uncertain stop")): self.runner.run_once()
        self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "UNKNOWN")
        self.assertEqual(len(self.store.list(reservations)), 2)
        state = self.state()
        result = self.client.post(f"/api/v1/deliveries/{self.group}/operations", json=evidence(kind="reconcile", context_digest=state["context_digest"]))
        self.assertEqual(result.status_code, 202, result.text)
        with patch("agent_workbench.resources.inventory", return_value=[]): self.runner.run_once()
        self.assertFalse(self.store.list(reservations)); self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "CANCELED")

    def test_environment_result_cannot_smuggle_a_model_conclusion(self):
        result = self.complete_controlled(transform=lambda output: {**output, "report": {"summary": "not allowed"}})
        self.assertEqual(result["status"], "FAILED"); self.assertFalse(self.state()["panorama"]["history"])

    def test_missing_coverage_is_not_accepted(self):
        result = self.complete_controlled(transform=lambda output: {**output, "coverage": output["coverage"][:1]})
        self.assertEqual(result["status"], "FAILED")

    def test_cancel_arriving_after_cleanup_still_prevents_result_publication(self):
        response = self.run_request()
        def canceled_after_work(executor, job, *_):
            output = controlled_output(job)
            self.store.cancel(job["id"])
            return output
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.panorama.execute", side_effect=canceled_after_work): self.runner.run_once()
        self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "CANCELED")
        self.assertFalse(self.store.list(reservations)); self.assertFalse(self.state()["panorama"]["history"])

    def test_joint_report_binds_scope_without_bypassing_project_analyses(self):
        before = self.state()["scope_digest"]
        result = self.complete_controlled("panorama"); self.assertEqual(result["status"], "SUCCEEDED", result.get("error"))
        state = self.state(); self.assertNotEqual(state["scope_digest"], before); self.assertFalse(state["scope_ready"])
        report = state["panorama"]["analysis"]["payload"]["report"]
        self.assertEqual(len(report["projects"]), 2); self.assertEqual(len(report["projects"][0]["evidence"][0]["file_sha256"]), 64)

    def test_requirement_change_during_execution_invalidates_report(self):
        def change(output):
            with self.store.engine.begin() as connection: connection.execute(update(requirements).where(requirements.c.id == self.second["id"]).values(title="new requirement"))
            return output
        result = self.complete_controlled("panorama", change)
        self.assertEqual(result["status"], "FAILED"); self.assertFalse(self.state()["panorama"]["history"])

    def test_reports_reject_missing_projects_unknown_paths_and_bad_relations(self):
        bundle = [{"project_id": "a", "files": [{"path": "main.txt", "sha256": "a" * 64}]}, {"project_id": "b", "files": []}]
        report = {"summary": "Controlled", "projects": [{"project_id": "a", "impact": "CHANGE", "summary": "A", "evidence": [{"path": "main.txt", "explanation": "code"}]},
            {"project_id": "b", "impact": "UNCERTAIN", "summary": "B", "evidence": []}], "relations": [], "risks": [], "open_questions": []}
        self.assertEqual(len(panorama.validate_report(report, bundle)["projects"]), 2)
        for path in ("../main.txt", "/etc/passwd", ".env", "node_modules/pkg.js", "missing.txt"):
            invalid = json.loads(json.dumps(report)); invalid["projects"][0]["evidence"][0]["path"] = path
            with self.assertRaises(WorkbenchError): panorama.validate_report(invalid, bundle)
        with self.assertRaises(WorkbenchError): panorama.validate_report({**report, "projects": report["projects"][:1]}, bundle)
        with self.assertRaises(WorkbenchError): panorama.validate_report({**report, "relations": [{"from_project": "a", "to_project": "outside", "description": "invalid"}]}, bundle)

    def test_context_mounts_reject_original_repository_writable_mode_and_wrong_target(self):
        primary = Path(self.first["workspace"]["path"])
        cases = [({"source": str(self.source), "target": "/workspace/candidates/" + self.first["project_id"]}, True)]
        isolated = Path(create_workspace(primary / "repo", self.data / "attempts/job_context", self.first["id"], self.first["project_id"])["workspace"])
        cases += [({"source": str(isolated / "repo"), "target": "/run/other"}, True), ({"source": str(isolated / "repo"), "target": "/workspace/candidates/" + self.first["project_id"]}, False)]
        with patch("agent_workbench.runtime.start") as start:
            for mount, readonly in cases:
                with self.assertRaises(WorkbenchError): self.runner.executor.start_runtime(isolated, "fixture", context_mounts=[mount], read_only_source=readonly)
            start.assert_not_called()

    def test_external_directory_permission_is_limited_to_candidate_roots(self):
        target = "/workspace/candidates/" + self.first["project_id"]
        config = runtime.model_config("opencode/glm-5.3-flash-free", True, [target])
        self.assertEqual(config["permission"]["external_directory"], {target + "/*": "allow"})
        self.assertEqual(config["permission"]["*"], "deny")
        with self.assertRaises(WorkbenchError): runtime.model_config("opencode/glm-5.3-flash-free", True, ["/workspace/*"])


class PanoramaScopeTests(DeliveryFixture):
    def analyze(self):
        state = self.group_state()
        response = self.client.post(self.url + "/panorama", json=evidence(kind="panorama", context_digest=state["panorama"]["context_digest"], model="opencode/glm-5.3-flash-free", prompt=""))
        self.assertEqual(response.status_code, 202, response.text)
        with patch("agent_workbench.panorama.execute", side_effect=lambda executor, job, *_: controlled_output(job)): self.runner.run_once()
        self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "SUCCEEDED")

    def test_new_report_invalidates_old_scope_and_export(self):
        self.confirm(); self.integrate(); self.run_delivery()
        self.analyze(); state = self.group_state()
        self.assertFalse(state["scope_approved"]); self.assertFalse(state["integration_valid"])
        self.assertEqual(state["exports"][0]["validity"], "STALE")
        self.confirm(); self.assertTrue(self.group_state()["scope_approved"])

    def test_changed_code_before_confirmation_blocks_approval(self):
        self.analyze(); (self.workspace / "repo/main.txt").write_text("changed after joint report\n")
        self.action(self.req, "refresh"); self.runner.run_once()
        with self.assertRaises(Exception): self.confirm()

    def test_joint_history_in_delivery_package(self):
        self.analyze(); self.confirm(); self.integrate(); result = self.run_delivery()
        self.assertEqual(result["result"]["delivery_export"]["manifest"]["panorama_history"][0]["kind"], "PANORAMA")

    def test_scope_revision_cannot_drop_previous_joint_analysis_gate(self):
        self.analyze(); self.confirm()
        state = self.group_state()
        self.service.save({**self.group_input, "expected_revision": state["revision"]["id"], "reason": "controlled scope update"}, self.group)
        state = self.group_state()
        self.assertTrue(state["panorama"]["analysis_required"]); self.assertIsNone(state["panorama"]["analysis"])
        self.assertFalse(state["scope_ready"])
        with self.assertRaises(Exception): self.confirm()
        self.analyze(); self.confirm(); self.assertTrue(self.group_state()["scope_approved"])
