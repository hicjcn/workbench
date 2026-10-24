import fcntl
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import update

from agent_workbench.api import create_app
from agent_workbench.execution import Executor, Unknown
from agent_workbench.runner import Runner
from agent_workbench.settings import Settings
from agent_workbench.store import Store, jobs, metadata
from agent_workbench.workspace import WorkbenchError, git, write_json


class ControlFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.settings = Settings(self.data, f"sqlite:///{self.data / 'test.db'}", "user-fixture-token" * 2, "runner-fixture-token" * 2,
                                 model="opencode/glm-5.3-flash-free")
        self.store = Store(self.settings.database_url)
        self.addCleanup(self.store.engine.dispose)
        metadata.create_all(self.store.engine)
        self.client = TestClient(create_app(self.settings, self.store))
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer " + self.settings.access_token
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.txt").write_text("original\n")
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "Fixture")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "add", "--all")
        git(self.source, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        def dispatch(request):
            response = self.client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        self.runner = Runner(self.settings, "http://testserver", transport=httpx.MockTransport(dispatch))
        self.addCleanup(self.runner.client.close)

    def project(self):
        response = self.client.post("/api/v1/projects", json={"name": "后端项目", "source_path": str(self.source)})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def requirement(self, project=None):
        response = self.client.post("/api/v1/requirements", json={"project_id": (project or self.project())["id"], "title": "处理需求", "acceptance": "预期行为"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def action(self, requirement, action, key=None, **payload):
        return self.client.post(f"/api/v1/requirements/{requirement['id']}/actions", json={"action": action, **payload}, headers={"Idempotency-Key": key} if key else {})

    def detail(self, requirement):
        return self.client.get(f"/api/v1/requirements/{requirement['id']}").json()

    def prepared(self):
        requirement = self.requirement()
        self.assertEqual(self.action(requirement, "prepare").status_code, 202)
        self.assertTrue(self.runner.run_once())
        detail = self.detail(requirement)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED", detail)
        return detail


class ControlPlaneTests(ControlFixture):
    def test_auth_origin_and_internal_boundary(self):
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/api/v1/projects").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/session", json={"token": "incorrect"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/session", json={"token": self.settings.access_token}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/projects").status_code, 200)
        self.assertEqual(self.client.post("/internal/claim", json={"owner": "fake"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/projects", json={}, headers={"Origin": "https://foreign.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/projects", headers={"Host": "foreign.example"}).status_code, 400)

    def test_validation_rejects_non_repository_and_blank_title(self):
        self.assertEqual(self.client.post("/api/v1/projects", json={"name": "", "source_path": str(self.source)}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/projects", json={"name": "invalid", "source_path": str(self.root)}).status_code, 422)
        project = self.project()
        self.assertEqual(self.client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "  "}).status_code, 422)

    def test_repeated_intent_and_active_workspace_are_not_replayed(self):
        requirement = self.requirement()
        first = self.action(requirement, "prepare", "same-intent")
        repeated = self.action(requirement, "prepare", "same-intent")
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        self.assertEqual(self.action(requirement, "start", "same-intent").status_code, 409)
        self.assertEqual(self.action(requirement, "prepare", "new-intent").status_code, 409)
        self.client.post(f"/api/v1/jobs/{first.json()['id']}/cancel", json={})
        self.assertFalse(self.runner.run_once())
        self.assertEqual(self.action(requirement, "prepare", "after-cancel").status_code, 202)

    def test_two_requirements_copy_and_persist_independently(self):
        project = self.project()
        first, second = self.requirement(project), self.requirement(project)
        self.action(first, "prepare"); self.action(second, "prepare")
        self.runner.run_once(); self.runner.run_once()
        first_path = Path(self.detail(first)["workspace"]["path"])
        second_path = Path(self.detail(second)["workspace"]["path"])
        (first_path / "repo/main.txt").write_text("first only\n")
        self.action(first, "refresh"); self.runner.run_once()
        self.assertEqual((second_path / "repo/main.txt").read_text(), "original\n")
        self.assertEqual((self.source / "main.txt").read_text(), "original\n")
        self.assertIn("first only", self.detail(first)["workspace"]["diff"])
        second_store = Store(self.settings.database_url)
        try:
            self.assertEqual(second_store.get(jobs, self.detail(first)["jobs"][0]["id"])["status"], "SUCCEEDED")
        finally:
            second_store.engine.dispose()

    def test_review_binds_digest_and_idle_changes_make_it_stale(self):
        detail = self.prepared()
        digest = detail["workspace"]["snapshot"]["tree_digest"]
        accepted = self.client.post(f"/api/v1/requirements/{detail['id']}/reviews", json={"digest": digest, "decision": "APPROVED"})
        self.assertEqual(accepted.status_code, 202)
        self.runner.run_once()
        self.assertEqual(self.detail(detail)["workspace"]["approval"], "APPROVED")
        (Path(detail["workspace"]["path"]) / "repo/main.txt").write_text("after approval\n")
        self.runner.watch_once()
        current = self.detail(detail)
        self.assertEqual(current["workspace"]["approval"], "STALE")
        self.assertEqual(len(current["reviews"]), 1)
        self.client.post(f"/api/v1/requirements/{detail['id']}/reviews", json={"digest": digest, "decision": "APPROVED"})
        self.runner.run_once()
        self.assertEqual(self.detail(detail)["jobs"][0]["status"], "FAILED")
        self.assertEqual(len(self.detail(detail)["reviews"]), 1)

    def test_expired_execution_is_unknown_and_requires_reconciliation(self):
        requirement = self.requirement()
        queued = self.action(requirement, "prepare").json()
        claim = self.store.claim("old_runner")
        with self.store.engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == queued["id"]).values(lease_until=0))
        self.store.expire()
        self.assertEqual(self.store.get(jobs, queued["id"])["status"], "UNKNOWN")
        self.assertEqual(self.action(requirement, "prepare").status_code, 409)
        with self.assertRaises(Exception):
            self.store.finish(queued["id"], "old_runner", claim["epoch"], "SUCCEEDED", {}, None)
        self.assertEqual(self.action(requirement, "reconcile").status_code, 202)
        self.runner.run_once()
        self.assertEqual(self.store.get(jobs, queued["id"])["status"], "CANCELED")
        self.assertEqual(self.action(requirement, "prepare").status_code, 202)

    def test_event_replay_is_durable_and_duplicate_sequence_is_checked(self):
        requirement = self.requirement()
        self.action(requirement, "prepare")
        job = self.store.claim("fixture")
        first = self.store.append_event(job["id"], "fixture", job["epoch"], 0, "output", {"text": "first"})
        duplicate = self.store.append_event(job["id"], "fixture", job["epoch"], 0, "output", {"text": "first"})
        self.assertEqual(first["id"], duplicate["id"])
        with self.assertRaises(Exception):
            self.store.append_event(job["id"], "fixture", job["epoch"], 0, "output", {"text": "different"})
        self.store.append_event(job["id"], "fixture", job["epoch"], 1, "output", {"text": "second"})
        self.store.finish(job["id"], "fixture", job["epoch"], "SUCCEEDED", {}, None)
        stream = self.client.get(f"/api/v1/jobs/{job['id']}/stream", headers={"Last-Event-ID": str(first["id"])})
        self.assertNotIn('"first"', stream.text)
        self.assertIn('"second"', stream.text)
        self.assertIn("event: done", stream.text)

    def test_git_metadata_and_external_helpers_cannot_escape_observation(self):
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        repo = workspace / "repo"
        marker = self.root / "executed"
        helper = self.root / "helper.sh"
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        helper.chmod(0o755)
        git(repo, "config", "diff.external", str(helper))
        git(repo, "config", "core.fsmonitor", str(helper))
        (repo / "main.txt").write_text("changed\n")
        observed = self.runner.executor.observe(workspace)
        self.assertIn("changed", observed["diff"])
        self.assertFalse(marker.exists())
        (repo / ".git/objects/info/alternates").write_text(str(self.source / ".git/objects"))
        with self.assertRaises(WorkbenchError):
            self.runner.executor.observe(workspace)

    def test_old_writer_lock_blocks_second_executor(self):
        detail = self.prepared()
        queued = self.action(detail, "refresh").json()
        job = self.store.claim("fixture")
        lock_path = self.data / "locks" / (detail["id"] + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Unknown):
                self.runner.executor.execute(job, lambda *_: None, threading.Event())
        self.assertEqual(job["id"], queued["id"])

    def test_incomplete_diff_is_visible_but_cannot_be_approved(self):
        detail = self.prepared()
        (Path(detail["workspace"]["path"]) / "repo/main.txt").write_text("x" * 510000)
        self.action(detail, "refresh")
        self.runner.run_once()
        current = self.detail(detail)
        self.assertTrue(current["workspace"]["diff_truncated"])
        self.client.post(f"/api/v1/requirements/{detail['id']}/reviews", json={
            "digest": current["workspace"]["snapshot"]["tree_digest"], "decision": "APPROVED"})
        self.runner.run_once()
        self.assertEqual(self.detail(detail)["jobs"][0]["status"], "FAILED")
        self.assertEqual(self.detail(detail)["reviews"], [])

    def test_list_and_history_do_not_repeat_large_code_payloads(self):
        detail = self.prepared()
        summary = self.client.get("/api/v1/requirements").json()[0]
        self.assertNotIn("snapshot", summary["workspace"])
        self.assertNotIn("diff", summary["workspace"])
        self.assertNotIn("result", detail["jobs"][0])

    def test_large_untracked_file_also_requires_complete_review(self):
        detail = self.prepared()
        (Path(detail["workspace"]["path"]) / "repo/new.txt").write_text("x" * 100001)
        self.action(detail, "refresh")
        self.runner.run_once()
        current = self.detail(detail)
        self.assertLess(len(current["workspace"]["diff"]), 500000)
        self.assertTrue(current["workspace"]["diff_truncated"])
        self.client.post(f"/api/v1/requirements/{detail['id']}/reviews", json={
            "digest": current["workspace"]["snapshot"]["tree_digest"], "decision": "APPROVED"})
        self.runner.run_once()
        self.assertEqual(self.detail(detail)["jobs"][0]["status"], "FAILED")
        self.assertEqual(self.detail(detail)["reviews"], [])

    def test_observed_agent_disconnect_stops_server_before_releasing_workspace(self):
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        write_json(workspace / "runtime.json", {"container": "fixture", "password": "fixture-control-password"})
        config = workspace / "agent-home/.config/opencode/opencode.json"
        from agent_workbench.runtime import opencode_free_config
        write_json(config, opencode_free_config(self.settings.model))
        job = {**detail, "requirement": detail, "input": {"prompt": "fixture"}}
        process = Mock(stdout=io.StringIO('{"type":"text","part":{"text":"started"}}\n'), stderr=io.StringIO(), returncode=0)
        process.poll.return_value = 0
        def emit(kind, _):
            if kind == "agent":
                raise ConnectionError("controller disconnected")
        with patch("agent_workbench.execution.subprocess.Popen", return_value=process), patch("agent_workbench.execution.runtime.stop") as stop:
            with self.assertRaises(ConnectionError):
                self.runner.executor.task(job, workspace, emit, threading.Event())
            stop.assert_called_once_with(workspace)


if __name__ == "__main__":
    unittest.main()
