"""Controlled events and transport failures; never a substitute for online acceptance."""
import io
import json
import os
from pathlib import Path
import ssl
import threading
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

import httpx

from agent_workbench.acceptance import Session, plan, VERIFY
from agent_workbench.acceptance_audit import model_evidence
from agent_workbench.model_diagnostics import diagnose, probe_url
from agent_workbench.opencode import skill_usage
from agent_workbench.workspace import WorkbenchError, write_json
from test_control_plane import ControlFixture
from test_delivery import DeliveryFixture


class EvidenceTests(unittest.TestCase):
    def row(self, value, kind="agent"): return {"kind": kind, "payload": {"text": json.dumps(value)} if kind == "agent" else value}
    def job(self, status="SUCCEEDED", model="opencode/big-pickle"): return {"id": "controlled", "status": status, "result": {"model": model}}
    def completed(self): return [self.row({"type": "text", "part": {"text": "controlled report"}}), self.row({"type": "step_finish", "part": {"reason": "stop"}})]

    def test_success_flag_without_events_never_proves_inference(self):
        self.assertFalse(model_evidence(self.job(), [], "opencode/big-pickle")["recorded"])

    def test_ended_text_and_matching_model_are_all_required(self):
        self.assertTrue(model_evidence(self.job(), self.completed(), "opencode/big-pickle")["recorded"])
        for job, rows, expected in [(self.job("FAILED"), self.completed(), "opencode/big-pickle"), (self.job(), self.completed(), "opencode/other-free"), (self.job(), self.completed()[:1], "opencode/big-pickle")]:
            self.assertFalse(model_evidence(job, rows, expected)["recorded"])

    def test_error_event_overrides_a_stop_event(self):
        self.assertFalse(model_evidence(self.job(), self.completed() + [self.row({"type": "error"})], "opencode/big-pickle")["recorded"])

    def test_skill_announcement_is_not_tool_evidence(self):
        rows = self.completed() + [self.row({"type": "text", "part": {"text": "Using Superpowers writing-plans"}})]
        self.assertFalse(model_evidence(self.job(), rows, "opencode/big-pickle")["skills"])

    def test_only_completed_known_skill_reads_count(self):
        value = {"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed", "input": {"filePath": "/opt/superpowers/skills/writing-plans/SKILL.md"}}}}
        self.assertEqual(skill_usage(value), {"writing-plans"})
        value["part"]["state"]["status"] = "running"; self.assertFalse(skill_usage(value))
        for bad in ({}, {"type": "tool_use", "part": []}, {"type": "tool_use", "part": {"state": "bad"}}): self.assertFalse(skill_usage(bad))

    def test_long_text_completion_metadata_is_bound_to_raw_stop(self):
        metadata = self.row({"schema": "workbench-model-completion/v1", "model": "opencode/big-pickle", "completed": True, "text_seen": True, "skills": ["writing-plans"]}, "model_completion")
        stop = self.completed()[1]
        result = model_evidence(self.job(), [metadata, stop], "opencode/big-pickle")
        self.assertTrue(result["recorded"]); self.assertEqual(result["skills"], ["writing-plans"])
        self.assertFalse(model_evidence(self.job(), [metadata], "opencode/big-pickle")["recorded"])

    def test_malformed_agent_parts_do_not_crash_audit(self):
        rows = [self.row({"type": "text", "part": "bad"}), self.row({"type": "text", "part": {"text": []}}), self.row({"type": "tool_use", "part": {"tool": "skill", "state": {"status": "completed", "input": {"name": []}}}})]
        self.assertFalse(model_evidence(self.job(), rows, "opencode/big-pickle")["recorded"])


class DiagnosticTests(unittest.TestCase):
    def test_connect_denial_is_not_reported_as_an_unknown_tls_failure(self):
        def blocked(*_, **__): raise URLError(OSError("Tunnel connection failed: 403 Forbidden private-proxy-value"))
        result = probe_url("https://models.dev/api.json", blocked)
        self.assertEqual((result["status"], result["classification"]), (403, "PROXY_DENIED"))
        self.assertNotIn("private-proxy-value", json.dumps(result))

    def test_tls_and_http_auth_errors_are_distinct_and_do_not_leak_bodies(self):
        def tls(*_, **__): raise URLError(ssl.SSLError("private diagnostic"))
        self.assertEqual(probe_url("https://models.dev/api.json", tls)["classification"], "TLS_ERROR")
        def auth(*_, **__): raise HTTPError("public", 401, "private diagnostic", {}, io.BytesIO(b"private body"))
        result = probe_url("https://opencode.ai/zen/v1/models", auth)
        self.assertEqual(result["classification"], "REACHABLE_HTTP_ERROR"); self.assertNotIn("private", json.dumps(result))

    def test_network_reachability_never_claims_model_inference(self):
        report = diagnose(["opencode/big-pickle"], lambda url: {"url": url, "classification": "REACHABLE", "status": 200})
        self.assertTrue(report["can_attempt"]); self.assertFalse(report["model_inference"])

    def test_missing_openrouter_key_is_reported_without_a_value(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}): report = diagnose(["openrouter/vendor/name:free"], lambda url: {"url": url, "classification": "REACHABLE", "status": 200})
        self.assertEqual(report["status"], "MISSING_CREDENTIAL"); self.assertFalse(report["can_attempt"])


class SessionTests(ControlFixture):
    def session(self, handler=None):
        token = self.root / "sample-token"; token.write_text("sample-local-token" * 3)
        session = Session(self.root / "sample-run", "http://127.0.0.1:8340", token, transport=httpx.MockTransport(handler or (lambda request: httpx.Response(200, json={"id": "controlled"}))))
        self.addCleanup(session.client.close); return session

    def test_remote_api_and_token_symlinks_are_rejected(self):
        token = self.root / "token"; token.write_text("value" * 10)
        with self.assertRaises(WorkbenchError): Session(self.root / "sample-run", "https://remote.example", token)
        link = self.root / "link"; link.symlink_to(token)
        with self.assertRaises(WorkbenchError): Session(self.root / "sample-run", "http://127.0.0.1:8340", link)

    def test_lost_post_response_retains_intent_and_never_blindly_reposts(self):
        calls = []
        def lost(request): calls.append(request); raise httpx.ReadTimeout("controlled lost response")
        session = self.session(lost)
        with self.assertRaises(WorkbenchError): session.post("/requirements/controlled/actions", {"action": "prepare"})
        self.assertTrue(session.state["pending"])
        with self.assertRaises(WorkbenchError): session.post("/requirements/controlled/actions", {"action": "prepare"})
        self.assertEqual(len(calls), 1)

    def test_server_error_retains_uncertainty_but_validation_rejection_is_terminal(self):
        session = self.session(lambda request: httpx.Response(503, json={}))
        with self.assertRaises(WorkbenchError): session.post("/projects", {"name": "fixture"})
        self.assertTrue(session.state["pending"])
        session.state["pending"] = None
        session.client.close(); session.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(422, json={})), base_url="http://127.0.0.1")
        with self.assertRaises(WorkbenchError): session.post("/projects", {"name": "fixture"})
        self.assertIsNone(session.state["pending"])

    def test_resolve_requires_exact_job_intent_and_requirement(self):
        session = self.session(); session.state["resources"]["requirement_id"] = "req_fixture"
        identity = "job_" + "a" * 32
        session.state["pending"] = {"key": "original", "assign": "negative_job_id", "path": "/actions", "body": {}}
        with patch.object(session, "get", return_value={"id": identity, "idempotency_key": "other", "requirement_id": "req_fixture"}):
            with self.assertRaises(WorkbenchError): session.resolve(identity)
        with patch.object(session, "get", return_value={"id": identity, "idempotency_key": "original", "requirement_id": "req_fixture"}): session.resolve(identity)
        self.assertIsNone(session.state["pending"]); self.assertEqual(session.state["resources"]["negative_job_id"], identity)

    def test_resolve_registration_checks_actual_attributes_without_reposting(self):
        session = self.session(); identity = "prj_" + "b" * 32
        session.state["pending"] = {"key": "original", "assign": "project_id", "path": "/projects", "body": {"name": "expected", "source_path": "/expected", "image": "expected:local"}}
        with patch.object(session, "get", return_value=[{"id": identity, "name": "wrong"}]), patch.object(session.client, "post") as post:
            with self.assertRaises(WorkbenchError): session.resolve(identity)
            post.assert_not_called()
        value = {"id": identity, **session.state["pending"]["body"]}
        with patch.object(session, "get", return_value=[value]): session.resolve(identity)
        self.assertEqual(session.state["resources"]["project_id"], identity)

    def test_existing_unrelated_directory_is_never_overwritten(self):
        directory = self.root / "occupied"; directory.mkdir(); (directory / "keep").write_text("keep")
        token = self.root / "token"; token.write_text("value" * 10)
        with self.assertRaises(WorkbenchError): Session(directory, "http://127.0.0.1:8340", token)
        self.assertEqual((directory / "keep").read_text(), "keep")


class CompletionEventTests(ControlFixture):
    def execute_stream(self, values):
        detail = self.prepared(); workspace = Path(detail["workspace"]["path"])
        write_json(workspace / "runtime.json", {"container": "controlled", "password": "private-completion-password"})
        from agent_workbench.runtime import model_config
        write_json(workspace / "agent-home/.config/opencode/opencode.json", model_config(self.settings.model))
        process = Mock(stdout=io.StringIO("\n".join(json.dumps(value) for value in values) + "\n"), stderr=io.StringIO(), stdin=io.StringIO(), returncode=0)
        process.poll.return_value = 0; emitted = []
        job = {"requirement": detail, "input": {"prompt": "controlled"}}
        observed = self.runner.executor.observe(workspace)
        with patch("agent_workbench.execution.subprocess.Popen", return_value=process), patch("agent_workbench.runtime.stop"), patch.object(self.runner.executor, "observe", return_value=observed):
            result = self.runner.executor.task(job, workspace, lambda kind, payload: emitted.append({"kind": kind, "payload": payload}), threading.Event(), collect_text=True)
        return result, emitted

    def test_completion_evidence_survives_truncated_text_without_leaking_secrets(self):
        values = [{"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed", "input": {"filePath": "/opt/superpowers/skills/writing-plans/SKILL.md"}}}},
            {"type": "text", "part": {"text": "private-completion-password" + "x" * 20000}}, {"type": "step_finish", "part": {"reason": "stop"}}]
        result, emitted = self.execute_stream(values)
        self.assertEqual(sum(item["kind"] == "model_completion" for item in emitted), 1)
        self.assertNotIn("private-completion-password", json.dumps(emitted))
        proof = model_evidence({"id": "controlled", "status": "SUCCEEDED", "result": result}, emitted, self.settings.model)
        self.assertTrue(proof["recorded"]); self.assertEqual(proof["skills"], ["writing-plans"])


class PlannerTests(unittest.TestCase):
    def inputs(self, step="scope"):
        state = {"pending": None, "resources": {"requirement_id": "req", "negative_job_id": "negative"}, "models": ["opencode/big-pickle", "opencode/one-free", "opencode/two-free"]}
        detail = {"jobs": [], "workspace": {"snapshot": {"tree_digest": "tree"}, "environment": "RUNNING"}, "profile": {"content_digest": "fixed"}, "preflight": {"status": "PASSED", "profile_digest": "fixed"}}
        flow = {"current": {"id": "flow", "state": "ACTIVE", "next_step": step}}
        group = {"id": "group", "panorama": {"analysis": {"id": "report"}}, "scope_approved": True}
        return state, detail, flow, {"current": None}, group, True

    def test_human_gates_never_generate_an_approval_request(self):
        for step in ("scope", "resolution", "self_review"):
            action = plan(*self.inputs(step)); self.assertEqual(action["status"], "WAITING_HUMAN"); self.assertNotIn("body", action)

    def test_unknown_and_failed_jobs_block_without_replay(self):
        for status in ("UNKNOWN", "FAILED", "CANCELED", "RUNNING"):
            values = self.inputs("development"); values[1]["jobs"] = [{"id": "old", "status": status}]
            self.assertNotEqual(plan(*values)["status"], "READY")

    def test_generation_never_implies_supervisor_or_test_execution(self):
        for step in ("supervisor_review", "performance_decision", "test_execution", "acceptance", "feedback_resolution"):
            values = self.inputs(); values[2]["current"]["state"] = "COMPLETE"
            values[3]["current"] = {"context_valid": True, "accepted": False, "next_step": step}
            self.assertEqual(plan(*values)["status"], "WAITING_HUMAN")

    def test_distinguishes_model_generation_and_machine_verification(self):
        self.assertTrue(plan(*self.inputs("development"))["model_call"])
        self.assertFalse(plan(*self.inputs("verification"))["model_call"])

    def test_expected_negative_control_does_not_hide_other_failures(self):
        values = self.inputs(); values[1]["jobs"] = [{"id": "negative", "status": "FAILED"}, {"id": "other", "status": "FAILED"}]
        self.assertEqual(plan(*values)["jobs"], ["other"])


class AcceptanceAuditTests(DeliveryFixture):
    def test_completed_controlled_flow_cannot_be_reported_as_full_online_acceptance(self):
        response = self.client.get(f"/api/v1/requirements/{self.req['id']}/acceptance")
        self.assertEqual(response.status_code, 200)
        result = response.json(); self.assertEqual(result["status"], "INCOMPLETE")
        checks = {item["key"]: item for item in result["checks"]}
        self.assertEqual(checks["self_review"]["status"], "RECORDED")
        self.assertEqual(checks["reviews"]["status"], "PENDING")
        self.assertEqual(checks["documents"]["status"], "PENDING")
        self.assertFalse(result["manual_identity_verified"]); self.assertFalse(result["provider_identity_verified"])

    def test_audit_is_authenticated_and_readonly(self):
        from agent_workbench.maintenance import metadata_digest, rows_snapshot
        before = metadata_digest(rows_snapshot(self.store))
        self.client.get(f"/api/v1/requirements/{self.req['id']}/acceptance")
        self.assertEqual(before, metadata_digest(rows_snapshot(self.store)))
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get(f"/api/v1/requirements/{self.req['id']}/acceptance").status_code, 401)
