import json
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import text

from agent_workbench import application, dependencies, resources
from agent_workbench.checked_archive import verify
from agent_workbench.execution import Canceled, Unknown
from agent_workbench.maintenance import backup, restore
from agent_workbench.profiles import Content
from agent_workbench.workspace import WorkbenchError, snapshot, write_json
from test_control_plane import ControlFixture


class ApplicationTests(ControlFixture):
    def prepare_app(self, preflight=True):
        project = self.project()
        self.url = f"/api/v1/projects/{project['id']}/profiles"
        content = Content(application={"start_command": "node server.cjs"}).model_dump()
        saved = self.client.post(self.url, json={"expected_profile": None, "content": content, "actor": "fixture", "reason": "controlled application"})
        self.assertEqual(saved.status_code, 201)
        self.profile = saved.json(); self.req = self.requirement(project)
        self.action(self.req, "prepare")
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": "sha256:fixed"}]))): self.runner.run_once()
        self.workspace = Path(self.detail(self.req)["workspace"]["path"])
        if preflight: write_json(self.workspace / "preflight-report.json", {"status": "PASSED", "profile_digest": self.profile["content_digest"]})
        self.baseline = snapshot(self.workspace / "repo")["tree_digest"]
        return self.workspace

    def journal(self):
        write_json(self.workspace / application.JOURNAL, {"phase": "READY", "code_digest": self.baseline, "checked_at": 1})

    def test_rejects_shell_commands_remote_urls_ports_and_unknown_options(self):
        project = self.project(); url = f"/api/v1/projects/{project['id']}/profiles"
        for app in [{"start_command": "node x | cat"}, {"start_command": " "}, {"start_command": "node x", "port": 80},
            {"start_command": "node x", "health_path": "http://example.com"}, {"start_command": "node x", "health_path": "/x?token=secret"},
            {"start_command": "node x", "expected_status": 302}, {"start_command": "node x", "ready_timeout_seconds": 1000}, {"start_command": "node x", "privileged": True}]:
            value = {**Content().model_dump(), "application": app}
            self.assertEqual(self.client.post(url, json={"expected_profile": None, "content": value, "actor": "fixture", "reason": "invalid"}).status_code, 422)

    def test_profile_revision_cannot_change_existing_service_command(self):
        self.prepare_app()
        content = Content(application={"start_command": "node different.cjs"}).model_dump()
        self.assertEqual(self.client.post(self.url, json={"expected_profile": self.profile["id"], "content": content, "actor": "fixture", "reason": "revision"}).status_code, 201)
        self.assertEqual(application.spec(self.workspace)["start_command"], "node server.cjs")

    def test_preflight_gate_runs_before_application_docker_mutations(self):
        self.prepare_app(False); self.action(self.req, "app_start")
        with patch("agent_workbench.runtime.docker") as docker: self.runner.run_once(); docker.assert_not_called()
        self.assertEqual(self.detail(self.req)["jobs"][0]["status"], "FAILED")

    def test_start_is_idempotent_at_job_boundary(self):
        self.prepare_app()
        one = self.action(self.req, "app_start", "same").json()
        self.assertEqual(self.action(self.req, "app_start", "same").json()["id"], one["id"])
        self.assertEqual(self.action(self.req, "app_start", "different").status_code, 409)

    def test_capacity_rejection_never_provisions_database_or_application(self):
        self.prepare_app(); write_json(self.workspace / "runtime.json", {"phase": "STARTED", "container": "dev"})
        development = {"State": {"Running": True}, "Config": {"Labels": {**application.labels(self.data, self.workspace), "awb.kind": "agent"}}}
        with patch("agent_workbench.dependencies.inspect", return_value=development), patch("agent_workbench.application.checked_container", return_value=None), patch("agent_workbench.resources.inventory", return_value=[{}] * 4), patch("agent_workbench.dependencies.ensure") as ensure, patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaisesRegex(WorkbenchError, "上限"): application.start(self.runner.executor, self.workspace, lambda *_: None, threading.Event())
            ensure.assert_not_called(); docker.assert_not_called()
        self.assertFalse((self.workspace / application.JOURNAL).exists())

    def test_code_change_invalidates_http_report_without_probing(self):
        self.prepare_app(); self.journal(); (self.workspace / "repo/main.txt").write_text("changed")
        with patch("agent_workbench.application.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.runtime.docker") as docker:
            result = application.check(self.runner.executor, self.workspace); docker.assert_not_called()
        self.assertFalse(result["passed"]); self.assertEqual(result["phase"], "STALE")

    def test_untrusted_git_metadata_is_rejected_before_http_probe(self):
        self.prepare_app(); self.journal()
        (self.workspace / "repo/.git/objects/info/alternates").write_text("/private/external-objects\n")
        with patch("agent_workbench.application.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaisesRegex(WorkbenchError, "外部 Git"): application.check(self.runner.executor, self.workspace)
            docker.assert_not_called()

    def test_missing_application_is_unknown_and_blocks_another_start(self):
        self.prepare_app(); self.journal(); self.action(self.req, "app_check")
        with patch("agent_workbench.application.checked_container", return_value=None): self.runner.run_once()
        self.assertEqual(self.detail(self.req)["jobs"][0]["status"], "UNKNOWN")
        self.assertEqual(self.detail(self.req)["application"]["phase"], "UNKNOWN")
        self.assertEqual(self.action(self.req, "app_start").status_code, 409)

    def test_exited_application_is_failed_with_retained_record(self):
        self.prepare_app(); self.journal()
        with patch("agent_workbench.application.checked_container", return_value={"State": {"Running": False, "ExitCode": 23}}):
            report = application.check(self.runner.executor, self.workspace)
        self.assertEqual(report["phase"], "EXITED"); self.assertEqual(report["exit_code"], 23)
        self.assertTrue((self.workspace / application.JOURNAL).exists())

    def test_docker_probe_timeout_is_unknown_without_retaining_ready_state(self):
        self.prepare_app(); self.journal()
        with patch("agent_workbench.application.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.runtime.docker", side_effect=subprocess.TimeoutExpired("controlled docker", 5)):
            with self.assertRaises(Unknown): application.check(self.runner.executor, self.workspace)
        self.assertEqual(application.status(self.workspace)["phase"], "UNKNOWN")

    def test_http_success_does_not_complete_code_review_or_material_tests(self):
        self.prepare_app(); self.journal(); self.action(self.req, "app_check")
        with patch("agent_workbench.application.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(returncode=0, stdout='{"status":200,"body_bytes":2,"body_sha256":"fixture","body_truncated":false,"latency_ms":1}')):
            self.runner.run_once()
        detail = self.detail(self.req)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED")
        self.assertEqual(detail["workspace"]["approval"], "PENDING")
        self.assertEqual(detail["application"]["last_check"]["business_tests"], "NOT_RUN")

    def test_stop_refuses_unowned_container_and_preserves_journal(self):
        self.prepare_app(); self.journal()
        with patch("agent_workbench.application.checked_container", side_effect=WorkbenchError("wrong owner")), patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaises(Unknown): application.stop(self.data, self.workspace)
            docker.assert_not_called()
        self.assertEqual(application.read(self.workspace, application.JOURNAL)["phase"], "UNKNOWN")

    def test_container_proof_rejects_writable_mounts_ports_and_wrong_image(self):
        self.prepare_app()
        detail = {"Id": "owned", "Image": "sha256:fixed", "Config": {"Labels": application.labels(self.data, self.workspace)},
            "HostConfig": {"PortBindings": {}}, "Mounts": [{"Type": "bind", "Source": str(self.workspace / "repo"), "Destination": "/workspace/project", "RW": False}]}
        with patch("agent_workbench.dependencies.inspect", return_value=detail):
            self.assertEqual(application.checked_container(self.data, self.workspace)["Id"], "owned")
            for key, value in [("mount", True), ("image", "different"), ("port", {"3000/tcp": [{"HostPort": "3000"}]})]:
                changed = json.loads(json.dumps(detail))
                if key == "mount": changed["Mounts"][0]["RW"] = value
                elif key == "image": changed["Image"] = value
                else: changed["HostConfig"]["PortBindings"] = value
                with patch("agent_workbench.dependencies.inspect", return_value=changed):
                    with self.assertRaises(WorkbenchError): application.checked_container(self.data, self.workspace)

    def test_unknown_start_response_retains_journal_and_never_replays(self):
        self.prepare_app(); write_json(self.workspace / "runtime.json", {"phase": "STARTED", "container": "dev"})
        development = {"State": {"Running": True}, "Config": {"Labels": {**application.labels(self.data, self.workspace), "awb.kind": "agent"}}}
        with patch("agent_workbench.dependencies.inspect", return_value=development), patch("agent_workbench.application.checked_container", return_value=None), patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.runtime.docker", side_effect=WorkbenchError("controlled lost response")) as docker:
            with self.assertRaises(Unknown): application.start(self.runner.executor, self.workspace, lambda *_: None, threading.Event())
            self.assertEqual(application.read(self.workspace, application.JOURNAL)["phase"], "UNKNOWN")
            self.assertEqual(docker.call_count, 1)
            with self.assertRaisesRegex(WorkbenchError, "运行记录"): application.start(self.runner.executor, self.workspace, lambda *_: None, threading.Event())
            self.assertEqual(docker.call_count, 1)

    def test_all_writers_stop_before_database_checkpoint(self):
        self.prepare_app(); sequence = []; write_json(self.workspace / "runtime.json", {})
        with patch("agent_workbench.application.stop", side_effect=lambda *_: sequence.append("application")), patch("agent_workbench.runtime.stop", side_effect=lambda *_: sequence.append("development")), patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.dependencies.stop", side_effect=lambda *_: sequence.append("checkpoint")):
            resources.stop_workspace(self.data, self.workspace)
        self.assertEqual(sequence, ["application", "development", "checkpoint"])

    def test_cancel_stops_application_and_other_writers(self):
        self.prepare_app()
        job = {"action": "app_start", "requirement_id": self.req["id"], "workspace": {"path": str(self.workspace)}}
        with patch.object(self.runner.executor, "_execute", side_effect=Canceled("controlled cancel")), patch("agent_workbench.resources.stop_workspace") as stop:
            with self.assertRaises(Canceled): self.runner.executor.execute(job, lambda *_: None, threading.Event())
        stop.assert_called_once_with(self.data, self.workspace)

    def test_backup_blocks_unreconciled_service_and_restores_only_history(self):
        self.prepare_app(); self.journal()
        with self.store.engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
        with patch("agent_workbench.resources.inventory", return_value=[]):
            with self.assertRaisesRegex(WorkbenchError, "运行记录"): backup(self.settings)
        (self.workspace / application.JOURNAL).unlink()
        write_json(self.workspace / application.REPORT, {"phase": "READY", "passed": True, "checked_at": 1})
        with patch("agent_workbench.resources.inventory", return_value=[]): result = backup(self.settings)
        manifest = verify(Path(result["file"]))
        self.assertTrue(any(item["path"].endswith(application.REPORT) for item in manifest["entries"]))
        self.assertFalse(any(item["path"].endswith(application.JOURNAL) for item in manifest["entries"]))
        restored = self.root / "restored"; restore(Path(result["file"]), restored)
        workspace = restored / self.workspace.relative_to(self.data)
        self.assertEqual(application.status(workspace)["phase"], "STOPPED")
        self.assertTrue(application.status(workspace)["last_check"]["passed"])

    def test_logs_redact_database_password_and_bound_returned_text(self):
        self.prepare_app(); password = "controlled-password"
        write_json(self.workspace / dependencies.PRIVATE, {"password": password}, private=True)
        with patch("agent_workbench.application.checked_container", return_value={"Id": "fixture"}), patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(returncode=0, stdout="x" * 17000 + password, stderr="")):
            result = application.execute(self.runner.executor, {"action": "app_logs"}, self.workspace, lambda *_: None, threading.Event())
        self.assertNotIn(password, result["application_logs"]); self.assertLessEqual(len(result["application_logs"]), 16000)

    def test_real_http_probe_limits_bodies_and_never_follows_redirects(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302 if self.path == "/redirect" else 200)
                if self.path == "/redirect": self.send_header("Location", "/private")
                self.end_headers()
                self.wfile.write(b"private-response" if self.path != "/large" else b"x" * 70000)
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for path in ("/health", "/redirect", "/large"):
                result = subprocess.run(["node", "-e", application.HTTP_PROBE, json.dumps({"port": server.server_port, "health_path": path})], capture_output=True, text=True, timeout=5, check=True)
                self.assertNotIn("private-response", result.stdout)
                response = json.loads(result.stdout)
                if path == "/redirect": self.assertEqual(response["status"], 302)
                if path == "/large": self.assertTrue(response["body_truncated"])
            self.assertEqual(hits, ["/health", "/redirect", "/large"])
        finally: server.shutdown(); server.server_close(); thread.join()
