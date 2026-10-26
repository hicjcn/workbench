import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import text

from agent_workbench.checked_archive import verify
from agent_workbench.maintenance import backup, restore
from agent_workbench.profiles import Content, inherit, runtime_options
from agent_workbench.store import Store, jobs, workspace_profiles
from agent_workbench.workspace import WorkbenchError, create_workspace
from agent_workbench.workspace import write_json
from test_control_plane import ControlFixture


class ProfileTests(ControlFixture):
    def setUp(self):
        super().setUp(); self.project_record = self.project()
        self.url = f"/api/v1/projects/{self.project_record['id']}/profiles"
        self.config_file = self.root / "settings.conf"; self.config_file.write_text("PRIVATE_FIXTURE=one\n")

    def save(self, content=None, expected=None):
        return self.client.post(self.url, json={"expected_profile": expected, "content": content or Content().model_dump(), "actor": "登记人", "reason": "受控接入"})

    def prepare(self, requirement):
        self.action(requirement, "prepare")
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": "sha256:fixture-fixed-image"}]))):
            self.assertTrue(self.runner.run_once())
        detail = self.detail(requirement)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED", detail["jobs"][0].get("error"))
        return detail

    def mounted(self):
        return Content(mounts=[{"source": str(self.config_file), "target": "/run/workbench-config/settings.conf"}]).model_dump()

    def test_versions_reject_stale_forms_and_preserve_original(self):
        first = self.save().json()
        second = self.save(Content(memory_mib=2048).model_dump(), first["id"])
        self.assertEqual(second.status_code, 201)
        self.assertEqual(self.save(expected=first["id"]).status_code, 409)
        state = self.client.get(self.url).json()
        self.assertEqual(state["current"]["number"], 2)
        self.assertEqual(state["history"][1]["content"]["memory_mib"], 1536)

    def test_queued_prepare_pins_profile_and_image_for_its_lifetime(self):
        first = self.save().json(); req = self.requirement(self.project_record)
        self.action(req, "prepare")
        self.save(Content(image="different:local").model_dump(), first["id"])
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": "sha256:fixed"}]))): self.runner.run_once()
        detail = self.detail(req)
        self.assertEqual(detail["profile"]["id"], first["id"])
        options = runtime_options(Path(detail["workspace"]["path"]), self.data)
        self.assertEqual(options["image"], "sha256:fixed")
        second = self.prepare(self.requirement(self.project_record))
        self.assertEqual(second["profile"]["number"], 2)

    def test_mounts_are_independent_private_copies_and_inherit_into_readonly_roles(self):
        self.assertEqual(self.save(self.mounted()).status_code, 201)
        req = self.prepare(self.requirement(self.project_record)); workspace = Path(req["workspace"]["path"])
        self.config_file.write_text("changed host content\n")
        options = runtime_options(workspace, self.data)
        copied = Path(options["config_mounts"][0]["source"])
        self.assertEqual(copied.read_text(), "PRIVATE_FIXTURE=one\n")
        if os.name != "nt":
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        role = Path(create_workspace(workspace / "repo", self.data / "attempts/job_fixture", req["id"], req["project_id"])["workspace"])
        inherit(workspace, role, self.data)
        self.assertEqual(Path(runtime_options(role, self.data)["config_mounts"][0]["source"]).read_bytes(), copied.read_bytes())
        self.assertNotIn("PRIVATE_FIXTURE", json.dumps(req))

    def test_missing_or_changed_external_config_blocks_rehydration(self):
        self.save(self.mounted()); req = self.prepare(self.requirement(self.project_record)); workspace = Path(req["workspace"]["path"])
        (workspace / "runtime-config/0.config").unlink(); self.config_file.write_text("changed\n")
        with self.assertRaisesRegex(WorkbenchError, "固定版本不一致"): runtime_options(workspace, self.data)

    def test_symlinks_control_files_and_unsafe_mount_targets_are_rejected(self):
        for source in [self.data, self.data / "access-token"]:
            source.mkdir(exist_ok=True) if source == self.data else source.write_text("sensitive")
            value = self.mounted(); value["mounts"][0]["source"] = str(source)
            self.assertEqual(self.save(value).status_code, 422)
        link = self.root / "linked"; link.symlink_to(self.config_file)
        value = self.mounted(); value["mounts"][0]["source"] = str(link)
        self.assertEqual(self.save(value).status_code, 422)
        for target in ["/var/run/docker.sock", "/workspace/project/.env", "/run/workbench-config/../escape", "/run/workbench-config/x,readonly=false"]:
            value = self.mounted(); value["mounts"][0]["target"] = target
            self.assertEqual(self.save(value).status_code, 422)

    def test_invalid_limits_and_shell_pipelines_are_rejected(self):
        for patch_value in [{"cpus": 0}, {"memory_mib": 1}, {"pids_limit": 0}, {"verification_command": "npm test | cat"}, {"verification_command": " "}]:
            self.assertEqual(self.save({**Content().model_dump(), **patch_value}).status_code, 422)

    def test_new_profile_does_not_change_existing_legacy_workspace(self):
        req = self.requirement(self.project_record); self.action(req, "prepare"); self.runner.run_once()
        self.save()
        self.assertIsNone(self.detail(req)["profile"])
        self.action(req, "prepare"); self.runner.run_once()
        self.assertIsNone(self.detail(req)["profile"])

    def test_profile_start_requires_preflight_before_any_docker_call(self):
        self.save(); req = self.prepare(self.requirement(self.project_record))
        self.action(req, "start")
        with patch("agent_workbench.runtime.start") as start: self.runner.run_once(); start.assert_not_called()
        self.assertEqual(self.detail(req)["jobs"][0]["status"], "FAILED")
        self.assertIn("环境预检", self.detail(req)["jobs"][0]["error"])

    def test_profile_routes_require_local_user_authentication(self):
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.save().status_code, 401)

    def test_preflight_cleanup_failure_marks_report_and_job_unknown(self):
        self.save(); req = self.prepare(self.requirement(self.project_record))
        self.action(req, "preflight")
        def interrupted_start(workspace, *_args, **_kwargs):
            write_json(workspace / "runtime.json", {"container": "controlled", "network": "controlled", "password": "controlled"})
            raise WorkbenchError("controlled startup failure")
        with patch.object(self.runner.executor, "start_runtime", side_effect=interrupted_start), patch("agent_workbench.runtime.stop", side_effect=WorkbenchError("cannot verify stop")):
            self.runner.run_once()
        detail = self.detail(req)
        self.assertEqual(detail["jobs"][0]["status"], "UNKNOWN")
        self.assertEqual(detail["preflight"]["status"], "UNKNOWN")
        self.assertEqual(self.action(req, "refresh").status_code, 409)

    def test_backup_preserves_profile_binding_but_excludes_mounted_file_contents(self):
        self.save(self.mounted()); req = self.prepare(self.requirement(self.project_record))
        with self.store.engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('0008')"))
        with patch("agent_workbench.resources.inventory", return_value=[]): result = backup(self.settings)
        manifest = verify(Path(result["file"]))
        self.assertEqual(manifest["schema_revision"], "0008")
        self.assertFalse(any("runtime-config/" in item["path"] for item in manifest["entries"]))
        target = self.root / "restored"; restore(Path(result["file"]), target)
        restored = Store(f"sqlite:///{target / 'workbench.db'}")
        try: self.assertEqual(restored.list(workspace_profiles), self.store.list(workspace_profiles))
        finally: restored.engine.dispose()
        workspace = target / "workspaces" / req["id"] / req["project_id"]
        self.assertFalse((workspace / "runtime-config").exists())
        staged = runtime_options(workspace, target)
        self.assertEqual(Path(staged["config_mounts"][0]["source"]).read_text(), "PRIVATE_FIXTURE=one\n")
