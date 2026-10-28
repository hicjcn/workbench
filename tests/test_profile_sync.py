"""Explicit profile synchronization preserves code, data and fixed job intent."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import update

from agent_workbench import dependencies, profile_sync
from agent_workbench.agent_clis import Content as AgentContent
from agent_workbench.checked_archive import file_hash
from agent_workbench.profiles import Content, runtime_options
from agent_workbench.store import jobs, workspaces
from agent_workbench.workspace import write_json
from test_control_plane import ControlFixture


class ProfileSyncTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.project_record = self.project()
        self.profile_url = f"/api/v1/projects/{self.project_record['id']}/profiles"
        self.host_config = self.root / "settings.conf"
        self.host_config.write_text("old-config\n")

    def save_profile(self, content=None, expected=None):
        response = self.client.post(self.profile_url, json={"expected_profile": expected, "content": content or Content().model_dump()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def mounted_content(self, **values):
        return Content(mounts=[{"source": str(self.host_config), "target": "/run/workbench-config/settings.conf"}], **values).model_dump()

    def prepare_profile(self, profile=None):
        profile = profile or self.save_profile()
        req = self.requirement(self.project_record)
        self.assertEqual(self.action(req, "prepare").status_code, 202)
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": "sha256:original-image"}]))):
            self.assertTrue(self.runner.run_once())
        detail = self.detail(req)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED", detail["jobs"][0].get("error"))
        return detail, Path(detail["workspace"]["path"]), profile

    def run_sync(self, req, *, image="sha256:updated-image", success=True, enqueue=True):
        if enqueue:
            response = self.action(req, "sync_profile")
            self.assertEqual(response.status_code, 202, response.text)
        with patch("agent_workbench.resources.inventory", return_value=[]), \
                patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": image}]))):
            self.assertTrue(self.runner.run_once())
        detail = self.detail(req)
        job = detail["jobs"][0]
        self.assertEqual(job["status"], "SUCCEEDED" if success else "FAILED", job.get("error"))
        return detail, job

    def write_passed_report(self, workspace, profile):
        report = {"status": "PASSED", "profile_digest": profile["content_digest"], "checks": []}
        write_json(workspace / "preflight-report.json", report)
        return report

    def stage_sync(self, req, image="sha256:pending-image"):
        self.assertEqual(self.action(req, "sync_profile").status_code, 202)
        job = self.store.claim("pending-sync-fixture")
        with patch("agent_workbench.resources.inventory", return_value=[]), \
                patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": image}]))):
            result = self.runner.executor.execute(job, lambda *_: None, threading.Event())
        return job, result

    def test_sync_uses_queued_target_and_preserves_source_and_session(self):
        req, workspace, first = self.prepare_profile(self.save_profile(self.mounted_content(environment={"MODE": "old"})))
        session = workspace / "agent-home/session.json"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text("private-agent-session")
        source = (workspace / "repo/main.txt").read_bytes()
        self.write_passed_report(workspace, first)
        write_json(workspace / "capability-report.json", {"old": "capability"})
        write_json(workspace / "application-report.json", {"phase": "READY", "passed": True})

        self.host_config.write_text("new-config\n")
        second = self.save_profile(self.mounted_content(cpus=2, memory_mib=2048, environment={"MODE": "new"}), first["id"])
        self.assertTrue(self.detail(req)["profile_sync"]["has_update"])
        queued = self.action(req, "sync_profile", "fixed-target")
        self.assertEqual(queued.status_code, 202, queued.text)
        repeated = self.action(req, "sync_profile", "fixed-target")
        self.assertEqual(repeated.json()["id"], queued.json()["id"])
        third = self.save_profile(self.mounted_content(cpus=3, environment={"MODE": "third"}), second["id"])
        detail, job = self.run_sync(req, enqueue=False)

        self.assertEqual(detail["profile"]["id"], second["id"])
        self.assertEqual(detail["profile_sync"]["latest_profile_id"], third["id"])
        self.assertTrue(detail["profile_sync"]["has_update"])
        self.assertEqual(detail["workspace"]["environment"], "STOPPED")
        self.assertEqual(detail["workspace"]["approval"], "STALE")
        self.assertEqual((workspace / "repo/main.txt").read_bytes(), source)
        self.assertEqual(session.read_text(), "private-agent-session")
        self.assertIsNone(detail["preflight"])
        self.assertFalse((workspace / "application-report.json").exists())
        profile_sync.resolve(workspace, job["id"])
        options = runtime_options(workspace, self.data)
        self.assertEqual(options["image"], "sha256:updated-image")
        self.assertEqual(options["limits"]["cpus"], 2)
        self.assertEqual(options["limits"]["memory_mib"], 2048)
        self.assertEqual(options["profile_environment"], {"MODE": "new"})
        self.assertEqual(Path(options["config_mounts"][0]["source"]).read_text(), "new-config\n")
        self.assertEqual(self.store.get(jobs, job["id"])["result"]["profile_sync"]["profile_id"], second["id"])

    def test_validation_failure_keeps_original_binding_files_and_report(self):
        req, workspace, first = self.prepare_profile(self.save_profile(self.mounted_content()))
        before_manifest = (workspace / "workspace.json").read_bytes()
        before_config = (workspace / "runtime-config/0.config").read_bytes()
        report = self.write_passed_report(workspace, first)
        self.host_config.write_text("fixed-new-config\n")
        self.save_profile(self.mounted_content(memory_mib=2048), first["id"])
        self.host_config.write_text("changed-after-registration\n")
        detail, _ = self.run_sync(req, success=False)
        self.assertEqual(detail["profile"]["id"], first["id"])
        self.assertEqual((workspace / "workspace.json").read_bytes(), before_manifest)
        self.assertEqual((workspace / "runtime-config/0.config").read_bytes(), before_config)
        self.assertEqual(detail["preflight"], report)
        self.assertFalse((workspace / profile_sync.JOURNAL).exists())

    def test_filesystem_commit_failure_rolls_back_staged_mounts_and_manifest(self):
        req, workspace, first = self.prepare_profile(self.save_profile(self.mounted_content()))
        before = json.loads((workspace / "workspace.json").read_text())
        before_config = (workspace / "runtime-config/0.config").read_bytes()
        report = self.write_passed_report(workspace, first)
        self.host_config.write_text("second-version\n")
        second = self.save_profile(self.mounted_content(memory_mib=2048), first["id"])
        original_write = profile_sync.write_json
        failed = False

        def fail_candidate_once(path, value, **options):
            nonlocal failed
            if Path(path) == workspace / "workspace.json" and value.get("runtime_profile", {}).get("id") == second["id"] and not failed:
                failed = True
                raise OSError("controlled manifest replacement failure")
            return original_write(path, value, **options)

        with patch("agent_workbench.profile_sync.write_json", side_effect=fail_candidate_once):
            detail, _ = self.run_sync(req, success=False)
        self.assertTrue(failed)
        self.assertEqual(detail["profile"]["id"], first["id"])
        self.assertEqual(json.loads((workspace / "workspace.json").read_text()), before)
        self.assertEqual((workspace / "runtime-config/0.config").read_bytes(), before_config)
        self.assertEqual(detail["preflight"], report)
        self.assertFalse((workspace / profile_sync.JOURNAL).exists())

    def test_default_cli_only_sync_rebinds_and_requires_new_preflight(self):
        req, workspace, first = self.prepare_profile()
        report = self.write_passed_report(workspace, first)
        response = self.client.post("/api/v1/agent-clis", json={"name": "新的兼容 CLI", "content": AgentContent(
            serve_command="team-code serve --hostname {host} --port {port}",
            run_command="team-code run --attach {endpoint} --model {model} --format json",
            version_command="team-code --version").model_dump(), "actor": "fixture", "reason": "sync fixture", "set_default": True})
        self.assertEqual(response.status_code, 201, response.text)
        selected = response.json()
        info = self.detail(req)["profile_sync"]
        self.assertTrue(info["has_update"])
        self.assertEqual(info["latest_profile_id"], first["id"])
        self.assertEqual(info["latest_agent_cli_id"], selected["id"])
        detail, job = self.run_sync(req)
        self.assertEqual(detail["agent_cli"]["id"], selected["id"])
        self.assertTrue(detail["agent_cli"]["fixed"])
        self.assertIsNone(detail["preflight"])
        self.assertEqual(detail["profile"]["content_digest"], report["profile_digest"])
        self.assertFalse(detail["profile_sync"]["has_update"])
        profile_sync.resolve(workspace, job["id"])
        self.assertEqual(runtime_options(workspace, self.data)["agent_cli"]["id"], selected["id"])

    def test_same_profile_sync_refreshes_tag_and_invalidates_old_preflight(self):
        req, workspace, first = self.prepare_profile()
        self.write_passed_report(workspace, first)
        self.assertFalse(self.detail(req)["profile_sync"]["has_update"])
        detail, job = self.run_sync(req, image="sha256:retagged-image")
        self.assertEqual(detail["profile"]["id"], first["id"])
        self.assertIsNone(detail["preflight"])
        profile_sync.resolve(workspace, job["id"])
        self.assertEqual(runtime_options(workspace, self.data)["image"], "sha256:retagged-image")
        self.assertFalse((workspace / profile_sync.JOURNAL).exists())

    def test_deleted_container_can_be_stopped_before_syncing_rebuilt_image(self):
        req, workspace, first = self.prepare_profile()
        source = (workspace / "repo/main.txt").read_bytes()
        self.write_passed_report(workspace, first)
        write_json(workspace / "runtime.json", {"container": "deleted-runtime", "network": "deleted-network"})
        with self.store.engine.begin() as connection:
            connection.execute(update(workspaces).where(workspaces.c.requirement_id == req["id"]).values(environment="ERROR"))

        self.assertEqual(self.action(req, "stop").status_code, 202)
        def docker(*args, **options):
            if args[0] == "inspect":
                self.assertEqual(args, ("inspect", "--type", "container", "deleted-runtime"))
                return SimpleNamespace(returncode=1, stdout="[]", stderr="Error: no such object: deleted-runtime\n")
            self.assertEqual(args, ("network", "rm", "deleted-network"))
            return SimpleNamespace(returncode=1, stdout="", stderr="No such network")

        with patch("agent_workbench.resources.inventory", return_value=[]), \
                patch("agent_workbench.runtime.docker", side_effect=docker):
            self.assertTrue(self.runner.run_once())
        stopped = self.detail(req)
        self.assertEqual(stopped["jobs"][0]["status"], "SUCCEEDED", stopped["jobs"][0].get("error"))
        self.assertEqual(stopped["workspace"]["environment"], "STOPPED")
        self.assertFalse((workspace / "runtime.json").exists())
        self.assertEqual(stopped["preflight"]["status"], "PASSED")

        detail, job = self.run_sync(req, image="sha256:rebuilt-image")
        profile_sync.resolve(workspace, job["id"])
        self.assertEqual(detail["profile"]["id"], first["id"])
        self.assertEqual(runtime_options(workspace, self.data)["image"], "sha256:rebuilt-image")
        self.assertIsNone(detail["preflight"])
        self.assertEqual((workspace / "repo/main.txt").read_bytes(), source)

    def test_unacknowledged_switch_can_restore_old_manifest_mounts_and_report(self):
        req, workspace, first = self.prepare_profile(self.save_profile(self.mounted_content()))
        before = json.loads((workspace / "workspace.json").read_text())
        config = (workspace / "runtime-config/0.config").read_bytes()
        report = self.write_passed_report(workspace, first)
        self.host_config.write_text("candidate-config\n")
        second = self.save_profile(self.mounted_content(memory_mib=2048), first["id"])
        response = self.action(req, "sync_profile")
        self.assertEqual(response.status_code, 202, response.text)
        job = self.store.claim("unacknowledged-fixture")
        with patch("agent_workbench.resources.inventory", return_value=[]), \
                patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": "sha256:candidate-image"}]))):
            self.runner.executor.execute(job, lambda *_: None, threading.Event())
        self.assertEqual(json.loads((workspace / "workspace.json").read_text())["runtime_profile"]["id"], second["id"])
        self.assertEqual(self.detail(req)["profile"]["id"], first["id"])
        self.assertTrue((workspace / profile_sync.JOURNAL).exists())
        profile_sync.resolve(workspace, rollback=True)
        self.assertEqual(json.loads((workspace / "workspace.json").read_text()), before)
        self.assertEqual((workspace / "runtime-config/0.config").read_bytes(), config)
        self.assertEqual(json.loads((workspace / "preflight-report.json").read_text()), report)
        self.assertFalse((workspace / profile_sync.JOURNAL).exists())

    def test_reconcile_keeps_committed_same_version_refresh_after_lost_response(self):
        req, workspace, first = self.prepare_profile()
        self.write_passed_report(workspace, first)
        job, result = self.stage_sync(req, image="sha256:committed-refresh")
        self.store.finish(job["id"], job["owner"], job["epoch"], "SUCCEEDED", result, None)
        self.assertTrue((workspace / profile_sync.JOURNAL).exists())
        self.assertEqual(self.action(req, "reconcile").status_code, 202)
        with patch("agent_workbench.resources.stop_workspace"):
            self.assertTrue(self.runner.run_once())
        self.assertFalse((workspace / profile_sync.JOURNAL).exists())
        self.assertEqual(self.detail(req)["profile"]["id"], first["id"])
        self.assertIsNone(self.detail(req)["preflight"])
        self.assertEqual(runtime_options(workspace, self.data)["image"], "sha256:committed-refresh")

    def test_reconcile_restores_uncommitted_switch_after_lost_lease(self):
        req, workspace, first = self.prepare_profile()
        report = self.write_passed_report(workspace, first)
        self.save_profile(Content(memory_mib=2048).model_dump(), first["id"])
        job, _ = self.stage_sync(req)
        with self.store.engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="UNKNOWN", lease_until=None))
        self.assertEqual(self.action(req, "reconcile").status_code, 202)
        with patch("agent_workbench.resources.stop_workspace"):
            self.assertTrue(self.runner.run_once())
        self.assertFalse((workspace / profile_sync.JOURNAL).exists())
        detail = self.detail(req)
        self.assertEqual(detail["profile"]["id"], first["id"])
        self.assertEqual(detail["preflight"], report)
        self.assertEqual(runtime_options(workspace, self.data)["image"], "sha256:original-image")
        self.assertEqual(self.store.get(jobs, job["id"])["status"], "CANCELED")

    def test_late_cleanup_cannot_roll_back_another_sync(self):
        req, workspace, first = self.prepare_profile()
        self.save_profile(Content(memory_mib=2048).model_dump(), first["id"])
        job, _ = self.stage_sync(req)
        after = (workspace / "workspace.json").read_bytes()
        profile_sync.resolve(workspace, rollback=True, expected_job_id="an-earlier-job")
        self.assertEqual((workspace / "workspace.json").read_bytes(), after)
        self.assertEqual(profile_sync.record(workspace)["job_id"], job["id"])
        profile_sync.resolve(workspace, rollback=True, expected_job_id=job["id"])

    def test_unprepared_running_unknown_and_active_jobs_reject_sync(self):
        profile = self.save_profile()
        unprepared = self.requirement(self.project_record)
        self.assertEqual(self.action(unprepared, "sync_profile").status_code, 409)
        req, _, _ = self.prepare_profile(profile)
        for environment in ("RUNNING", "UNKNOWN"):
            with self.subTest(environment=environment):
                with self.store.engine.begin() as connection:
                    connection.execute(update(workspaces).where(workspaces.c.requirement_id == req["id"]).values(environment=environment))
                self.assertEqual(self.action(req, "sync_profile").status_code, 409)
        with self.store.engine.begin() as connection:
            connection.execute(update(workspaces).where(workspaces.c.requirement_id == req["id"]).values(environment="STOPPED"))
        pending = self.action(req, "refresh")
        self.assertEqual(pending.status_code, 202, pending.text)
        for status in ("QUEUED", "RUNNING", "UNKNOWN"):
            with self.subTest(status=status):
                with self.store.engine.begin() as connection:
                    connection.execute(update(jobs).where(jobs.c.id == pending.json()["id"]).values(status=status, lease_until=time.time() + 3600))
                self.assertEqual(self.action(req, "sync_profile").status_code, 409)

    def test_runtime_application_or_dependency_journal_blocks_sync_without_changes(self):
        req, workspace, first = self.prepare_profile()
        manifest = (workspace / "workspace.json").read_bytes()
        for name in ("runtime.json", "application-runtime.json", dependencies.JOURNAL):
            with self.subTest(journal=name):
                write_json(workspace / name, {"phase": "STARTED"})
                detail, _ = self.run_sync(req, success=False)
                self.assertEqual(detail["profile"]["id"], first["id"])
                self.assertEqual((workspace / "workspace.json").read_bytes(), manifest)
                (workspace / name).unlink()

    def test_legacy_workspace_can_explicitly_adopt_latest_profile(self):
        req = self.requirement(self.project_record)
        self.assertEqual(self.action(req, "prepare").status_code, 202)
        self.assertTrue(self.runner.run_once())
        self.assertIsNone(self.detail(req)["profile"])
        profile = self.save_profile(Content(environment={"MODE": "adopted"}).model_dump())
        detail, job = self.run_sync(req)
        self.assertEqual(detail["profile"]["id"], profile["id"])
        workspace = Path(detail["workspace"]["path"])
        profile_sync.resolve(workspace, job["id"])
        self.assertEqual(runtime_options(workspace, self.data)["profile_environment"], {"MODE": "adopted"})

    def test_removing_database_configuration_preserves_dataset_and_backup_files(self):
        req, workspace, first = self.prepare_profile(self.save_profile(Content(dependencies=[{"kind": "postgres"}]).model_dump()))
        state = {"schema": "workbench-postgres/v1", "data_id": "preserved-data", "initialized": True, "phase": "STOPPED"}
        write_json(workspace / dependencies.STATE, state)
        private = {"password": "preserved-private-password"}
        write_json(workspace / dependencies.PRIVATE, private, private=True)
        dump = workspace / dependencies.DUMP
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_bytes(b"preserved-postgres-snapshot")
        snapshot = {"format": "postgres17/custom", "data_id": state["data_id"], "sha256": file_hash(dump), "size": dump.stat().st_size}
        write_json(workspace / dependencies.SNAPSHOT, snapshot)
        without_database = self.save_profile(Content(environment={"MODE": "without-database"}).model_dump(), first["id"])
        detail, job = self.run_sync(req)
        profile_sync.resolve(workspace, job["id"])
        self.assertFalse(detail["profile"]["content"]["dependencies"])
        self.assertEqual(dependencies.read(workspace, dependencies.STATE), state)
        self.assertEqual(dependencies.read(workspace, dependencies.PRIVATE), private)
        self.assertEqual(dump.read_bytes(), b"preserved-postgres-snapshot")
        self.assertEqual(set(dependencies.backup_files(workspace)), {dependencies.STATE, dependencies.SNAPSHOT, dependencies.DUMP})
        restored = self.save_profile(Content(dependencies=[{"kind": "postgres"}]).model_dump(), without_database["id"])
        detail, _ = self.run_sync(req)
        self.assertEqual(detail["profile"]["id"], restored["id"])
        self.assertIsNotNone(dependencies.spec(workspace))
        self.assertEqual(dependencies.read(workspace, dependencies.STATE), state)
        self.assertEqual(dependencies.read(workspace, dependencies.PRIVATE), private)
        self.assertEqual(dependencies.snapshot_info(workspace, required=True), snapshot)

    def test_sync_requires_user_authentication(self):
        req, _, _ = self.prepare_profile()
        self.client.headers.pop("Authorization")
        self.assertEqual(self.action(req, "sync_profile").status_code, 401)
