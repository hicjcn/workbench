"""Controlled M5 records; no live model calls or external approvals."""
import io
import json
from pathlib import Path
import threading
import zipfile
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import text, update

from agent_workbench.checked_archive import build, verify
from agent_workbench.delivery import Delivery
from agent_workbench.maintenance import backup, restore
from agent_workbench.resources import controller_lock
from agent_workbench.settings import Settings
from agent_workbench.store import jobs, reservations, Store, workspaces
from agent_workbench.workspace import WorkbenchError, git
from test_materials import MaterialFixture, evidence
from test_control_plane import ControlFixture


class DeliveryFixture(MaterialFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(patch.stopall)
        patch("agent_workbench.resources.inventory", return_value=[]).start()
        patch("agent_workbench.resources.cleanup_networks").start()
        self.ready(); self.execute_cases(); self.act("acceptance", evidence())
        self.first = self.req
        self.req = self.prepared()
        self.workspace = Path(self.req["workspace"]["path"])
        self.base = f"/api/v1/requirements/{self.req['id']}/materials"
        self.workflow.start(self.req["id"], ["opencode/glm-5.3-flash-free", "opencode/big-pickle"], "node --test", "受控第二项目", self.req["workspace"]["snapshot"]["tree_digest"])
        self.complete_code_flow(); self.ready(); self.execute_cases(); self.act("acceptance", evidence())
        self.service = Delivery(self.store)
        self.group_input = {"title": "跨项目受控交付", "description": "样例业务链路", "actor": "登记人", "reason": "组织交付", "expected_revision": None,
            "members": [{"requirement_id": item["id"], "required": True, "skip_reason": ""} for item in (self.first, self.req)]}
        self.group = self.service.save(self.group_input)["id"]
        self.url = "/api/v1/deliveries/" + self.group

    def group_state(self): return self.service.read(self.group)

    def confirm(self):
        state = self.group_state()
        self.service.decide(self.group, evidence(expected_scope=state["scope_digest"], decision="APPROVED"))

    def integrate(self, result="PASS"):
        self.service.integrate(self.group, evidence(expected_baseline=self.group_state()["baseline_digest"], result=result, environment="受控联调环境", actual="样例登记结果"))

    def operation(self, kind="capture", key=None):
        return self.client.post(self.url + "/operations", json=evidence(kind=kind, context_digest=self.group_state()["context_digest"]), headers={"Idempotency-Key": key} if key else {})

    def run_delivery(self, kind="capture"):
        response = self.operation(kind)
        self.assertEqual(response.status_code, 202, response.text)
        self.runner.run_once()
        result = self.store.get(jobs, response.json()["id"])
        self.assertEqual(result["status"], "SUCCEEDED", result.get("error"))
        return result


class DeliveryTests(DeliveryFixture):
    def test_one_unaccepted_required_member_blocks_the_whole_group(self):
        self.confirm()
        self.act("supervisor_review", evidence(decision="REJECTED"))
        self.assertFalse(self.group_state()["members_ready"])
        self.assertEqual(self.group_state()["members"][0]["ready"], True)
        self.assertEqual(self.operation().status_code, 409)
        with self.assertRaisesRegex(Exception, "当前必要项目"): self.integrate()

    def test_global_scope_and_integration_are_explicit_gates(self):
        self.assertEqual(self.operation().status_code, 409)
        self.confirm()
        self.assertFalse(self.group_state()["ready"])
        self.integrate("FAIL")
        self.assertEqual(self.operation().status_code, 409)
        self.integrate()
        self.assertTrue(self.group_state()["ready"])

    def test_all_members_reserved_and_cancel_releases_them(self):
        self.confirm(); self.integrate()
        job = self.operation().json()
        self.assertEqual(len(self.store.list(reservations)), 2)
        for member in (self.first, self.req): self.assertEqual(self.action(member, "refresh").status_code, 409)
        changed = {**self.group_input, "expected_revision": self.group_state()["revision"]["id"]}
        self.assertEqual(self.client.post(self.url + "/revisions", json=changed).status_code, 409)
        self.client.post(f"/api/v1/jobs/{job['id']}/cancel", json={})
        self.assertEqual(self.store.list(reservations), [])
        self.assertEqual(self.action(self.req, "refresh").status_code, 202)

    def test_actual_second_member_changes_fail_capture_and_stale_its_reviews(self):
        self.confirm(); self.integrate()
        response = self.operation()
        (self.workspace / "repo/main.txt").write_text("changed after request\n")
        self.runner.run_once()
        self.assertEqual(self.store.get(jobs, response.json()["id"])["status"], "FAILED")
        self.assertEqual(self.store.list(reservations), [])
        self.assertFalse(self.group_state()["ready"])
        self.assertEqual(self.group_state()["exports"], [])

    def test_unknown_reserves_every_member_until_explicit_group_reconcile(self):
        self.confirm(); self.integrate()
        queued = self.operation().json(); claimed = self.store.claim("gone")
        with self.store.engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == queued["id"]).values(lease_until=0))
        self.store.expire()
        self.assertEqual(self.store.get(jobs, queued["id"])["status"], "UNKNOWN")
        self.assertEqual(len(self.store.list(reservations)), 2)
        self.assertEqual(self.action(self.req, "reconcile").status_code, 409)
        self.run_delivery("reconcile")
        self.assertEqual(self.store.get(jobs, queued["id"])["status"], "CANCELED")
        self.assertEqual(self.store.list(reservations), [])
        self.assertEqual(claimed["id"], queued["id"])

    def test_archive_freezes_both_and_reopen_is_audited(self):
        self.confirm(); self.integrate(); self.run_delivery("archive")
        self.assertEqual(self.group_state()["state"], "ARCHIVED")
        for member in (self.first, self.req): self.assertEqual(self.action(member, "task", prompt="change").status_code, 409)
        self.assertEqual(self.client.post(self.url + "/reopen", json=evidence()).status_code, 200)
        self.assertEqual(self.group_state()["state"], "ACTIVE")
        self.assertEqual(self.action(self.req, "refresh").status_code, 202)

    def test_delivery_contains_both_complete_code_and_material_histories(self):
        self.confirm(); self.integrate(); self.run_delivery()
        item = self.group_state()["exports"][0]
        response = self.client.get(self.url + "/exports/" + item["id"])
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len(manifest["projects"]), 2)
            self.assertEqual(manifest["integration"]["payload"]["result"], "PASS")
            for member in (self.first, self.req):
                prefix = "projects/" + member["project_id"]
                self.assertEqual(archive.read(prefix + "/code/main.txt"), b"original\n")
                history = json.loads(archive.read(prefix + "/history.json"))
                self.assertTrue(history["material_versions"])
                self.assertTrue(history["workflow_history"])
        (self.data / "deliveries" / (item["job_id"] + ".zip")).write_bytes(b"tampered")
        self.assertEqual(self.client.get(self.url + "/exports/" + item["id"]).status_code, 422)

    def test_revision_reconfirms_scope_and_marks_old_export_stale(self):
        self.confirm(); self.integrate(); self.run_delivery()
        previous = self.group_state()
        supplied = {**self.group_input, "title": "修订范围", "expected_revision": previous["revision"]["id"]}
        self.service.save(supplied, self.group)
        state = self.group_state()
        self.assertEqual(state["revision"]["number"], 2)
        self.assertEqual(len(state["history"]), 2)
        self.assertFalse(state["scope_approved"])
        self.assertFalse(state["integration_valid"])
        self.assertEqual(state["exports"][0]["validity"], "STALE")
        self.assertEqual(self.client.post(self.url + "/scope", json=evidence(expected_scope=previous["scope_digest"], decision="APPROVED")).status_code, 409)

    def test_optional_project_requires_skip_reason_and_is_not_counted_as_tested(self):
        supplied = {**self.group_input, "expected_revision": self.group_state()["revision"]["id"], "members": [self.group_input["members"][0], {"requirement_id": self.req["id"], "required": False, "skip_reason": "不涉及本次接口"}]}
        self.service.save(supplied, self.group)
        self.confirm(); self.integrate("NOT_REQUIRED"); self.run_delivery()
        item = self.group_state()["exports"][0]
        manifest = verify(self.data / "deliveries" / (item["job_id"] + ".zip"), item)
        self.assertEqual(manifest["projects"][1]["status"], "SKIPPED")
        self.assertTrue(manifest["projects"][1]["reason"])
        supplied["members"][1]["skip_reason"] = " "
        self.assertEqual(self.client.post(self.url + "/revisions", json=supplied).status_code, 422)

    def test_scope_changes_block_child_development(self):
        self.workflow.start(self.req["id"], ["opencode/glm-5.3-flash-free", "opencode/big-pickle"], "node --test", "新开发", self.req["workspace"]["snapshot"]["tree_digest"])
        flow = self.workflow.read(self.req["id"])["current"]
        self.workflow.enqueue(self.req["id"], flow["id"], "analysis", {}, None)
        job = self.store.claim("fixture")
        self.store.finish(job["id"], "fixture", job["epoch"], "SUCCEEDED", {**self.runner.executor.observe(self.workspace), "workflow_output": {"summary": "new", "impacted_paths": [], "plan": "x", "risks": []}}, None)
        self.workflow.enqueue(self.req["id"], flow["id"], "scope", {"decision": "APPROVED", "reason": "local"}, None)
        job = self.store.claim("fixture")
        self.store.finish(job["id"], "fixture", job["epoch"], "SUCCEEDED", {**self.runner.executor.observe(self.workspace), "workflow_output": job["input"]["request"]}, None)
        with self.assertRaisesRegex(Exception, "跨项目分析"): self.workflow.enqueue(self.req["id"], flow["id"], "development", {}, None)

    def test_runtime_authentication_and_limit_report(self):
        report = self.client.get("/api/v1/runtime").json()
        self.assertEqual(report["limits"], {"active_jobs": 2, "containers": 4})
        self.assertTrue(report["resources_stale"])
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/api/v1/runtime").status_code, 401)
        self.assertEqual(self.client.post("/internal/runtime", json={"owner": "x", "report": {}}).status_code, 401)


class MaintenanceTests(DeliveryFixture):
    def stamp(self):
        with self.store.engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('0008')"))

    def test_private_backup_restores_new_paths_tokens_and_all_code(self):
        self.confirm(); self.integrate(); self.run_delivery("archive"); self.stamp()
        (self.data / "access-token").write_text(self.settings.access_token)
        (self.data / "runner-token").write_text(self.settings.runner_token)
        private = self.workspace / "agent-home/.local/share/opencode/auth.json"
        private.parent.mkdir(parents=True); private.write_text('"private fixture"')
        result = backup(self.settings)
        archive = Path(result["file"])
        with zipfile.ZipFile(archive) as zipped:
            self.assertFalse(any("agent-home" in name or name.endswith("access-token") or name.endswith("runtime.json") for name in zipped.namelist()))
        destination = self.root / "restored"
        info = restore(archive, destination)
        self.assertTrue(info["credentials_regenerated"])
        self.assertNotEqual((destination / "access-token").read_text(), self.settings.access_token)
        self.assertEqual((destination / "access-token").stat().st_mode & 0o777, 0o600)
        restored = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            state = Delivery(restored).read(self.group)
            self.assertEqual(state["state"], "ARCHIVED")
            self.assertTrue(state["ready"])
            self.assertEqual(len(state["exports"]), 1)
            for row in restored.list(workspaces):
                path = Path(row["path"])
                self.assertTrue(path.is_relative_to(destination))
                self.assertEqual((path / "repo/main.txt").read_text(), "original\n")
                original_path = next(item["path"] for item in self.store.list(workspaces) if item["requirement_id"] == row["requirement_id"])
                self.assertEqual(git(path / "repo", "rev-parse", "HEAD"), git(Path(original_path) / "repo", "rev-parse", "HEAD"))
        finally: restored.engine.dispose()
        self.assertEqual((self.source / "main.txt").read_text(), "original\n")
        with self.assertRaises(WorkbenchError): restore(archive, destination)

    def test_backup_requires_no_active_services_or_tasks(self):
        self.stamp()
        with controller_lock(self.data):
            with self.assertRaisesRegex(WorkbenchError, "仍在运行"): backup(self.settings)
        self.action(self.req, "refresh")
        with self.assertRaisesRegex(WorkbenchError, "排队"): backup(self.settings)

    def test_invalid_archive_is_rejected_before_target_is_created(self):
        archive = self.root / "broken.zip"; archive.write_bytes(b"broken")
        target = self.root / "new"
        with self.assertRaises(WorkbenchError): restore(archive, target)
        self.assertFalse(target.exists())
        with self.assertRaises(WorkbenchError): build(self.root / "unsafe.zip", {}, [("../outside", b"bad")])


class AdmissionTests(MaterialFixture):
    def test_claim_respects_global_active_limit(self):
        pending = []
        for _ in range(3):
            req = self.requirement(); pending.append(self.action(req, "prepare").json())
        first, second = self.store.claim("one"), self.store.claim("two")
        self.assertIsNotNone(first); self.assertIsNotNone(second)
        self.assertIsNone(self.store.claim("three"))
        self.store.finish(first["id"], "one", first["epoch"], "FAILED", {}, "controlled failure")
        self.assertEqual(self.store.claim("three")["id"], pending[2]["id"])

    def test_container_admission_counts_readonly_and_orphans(self):
        with patch("agent_workbench.resources.inventory", return_value=[{}] * 4), patch("agent_workbench.runtime.start") as start:
            with self.assertRaisesRegex(WorkbenchError, "达到上限"): self.runner.executor.start_runtime(self.workspace, "fixture")
            start.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def test_restore_rejects_traversal_before_creating_any_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = root / "malicious.zip"; target = root / "new"
            content = b"bad"; name = "files/../../escape"
            manifest = {"schema": "workbench-backup/v1", "schema_revision": "0005", "entries": [{"path": name, "size": 3, "sha256": hashlib.sha256(content).hexdigest(), "mode": 0o644}]}
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(name, content); output.writestr("manifest.json", json.dumps(manifest))
            with self.assertRaisesRegex(WorkbenchError, "不安全路径"): restore(archive, target)
            self.assertFalse(target.exists()); self.assertFalse((root / "escape").exists())

    def test_restore_rejects_changed_entry_hash_even_with_valid_zip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = root / "changed.zip"; target = root / "new"
            manifest = {"schema": "workbench-backup/v1", "schema_revision": "0005", "entries": [{"path": "database/jobs.json", "size": 2, "sha256": "0" * 64, "mode": 0o644}]}
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("database/jobs.json", b"[]"); output.writestr("manifest.json", json.dumps(manifest))
            with self.assertRaisesRegex(WorkbenchError, "条目摘要不符"): restore(archive, target)
            self.assertFalse(target.exists())


class DeliveryDiffTests(ControlFixture):
    def test_literal_filename_cannot_expand_patch_to_excluded_credentials(self):
        from agent_workbench.delivery_export import full_diff
        from agent_workbench.workspace import snapshot
        detail = self.prepared(); workspace = Path(detail["workspace"]["path"]); repo = workspace / "repo"
        (repo / ":(glob)*").write_text("allowed literal file\n")
        (repo / ".env").write_text("PASSWORD=private-arbitrary-fixture\n")
        git(repo, "add", "--all")
        content = full_diff(self.runner.executor, workspace, snapshot(repo)["files"])
        self.assertIn(b"allowed literal file", content)
        self.assertNotIn(b"private-arbitrary-fixture", content)
