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

    def test_registration_metadata_may_be_omitted_or_blank(self):
        expected = None
        cases = [({}, "", ""), ({"actor": "", "reason": ""}, "", ""),
            ({"actor": "  ", "reason": " \n "}, "", ""),
            ({"actor": " 登记人 ", "reason": " 修订配置 "}, "登记人", "修订配置")]
        for metadata, actor, reason in cases:
            with self.subTest(metadata=metadata):
                response = self.client.post(self.url, json={"expected_profile": expected, "content": Content().model_dump(), **metadata})
                self.assertEqual(response.status_code, 201, response.text)
                saved = self.client.get(self.url).json()["current"]
                self.assertEqual((saved["actor"], saved["reason"]), (actor, reason))
                expected = saved["id"]

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
        for patch_value in [{"cpus": 0}, {"memory_mib": 1}, {"pids_limit": 0}, {"verification_command": "npm test | cat"}]:
            self.assertEqual(self.save({**Content().model_dump(), **patch_value}).status_code, 422)

    def test_verification_command_may_be_omitted_or_blank(self):
        expected = None
        for command_value in (None, "", " "):
            with self.subTest(command=command_value):
                content = Content().model_dump()
                if command_value is None: content.pop("verification_command")
                else: content["verification_command"] = command_value
                response = self.client.post(self.url, json={"expected_profile": expected, "content": content})
                self.assertEqual(response.status_code, 201, response.text)
                saved = self.client.get(self.url).json()["current"]
                self.assertFalse(saved["content"]["verification_command"].strip())
                expected = saved["id"]

    def test_missing_verification_command_does_not_report_success(self):
        profile = self.save().json()
        req = self.prepare(self.requirement(self.project_record))
        workspace = Path(req["workspace"]["path"])
        write_json(workspace / "preflight-report.json", {"status": "PASSED", "profile_digest": profile["content_digest"]})
        self.assertEqual(self.action(req, "verify").status_code, 202)
        with patch("agent_workbench.workflow_execution.verify_command") as verify:
            self.assertTrue(self.runner.run_once())
            verify.assert_not_called()
        job = self.detail(req)["jobs"][0]
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("未登记此命令", job["error"])

    def test_environment_ports_and_writable_directory_are_pinned_and_redacted(self):
        directory = self.root / "external-data"; directory.mkdir()
        content = Content(environment={"APP_TOKEN": "private-app-token", "DEBUG": "1", "FEATURE": "false"},
            volume_mounts=[{"source": str(directory), "target": "/mnt/data", "read_only": False}],
            port_mappings=[{"container_port": 3000}]).model_dump()
        saved = self.save(content)
        self.assertEqual(saved.status_code, 201, saved.text)
        detail = self.prepare(self.requirement(self.project_record))
        workspace = Path(detail["workspace"]["path"])
        options = runtime_options(workspace, self.data)
        self.assertEqual(options["volume_mounts"][0], {"source": str(directory), "target": "/mnt/data", "read_only": False})
        self.assertEqual(options["profile_environment"], {"APP_TOKEN": "private-app-token", "DEBUG": "1", "FEATURE": "false"})
        self.assertIsNone(options["port_mappings"][0]["host_port"])
        self.assertEqual(self.runner.executor.redact("token=private-app-token", workspace), "token=[REDACTED]")
        self.assertEqual(self.runner.executor.redact_payload({"ok": True, "passed": False, "number": 1, "version": "v1.0", "output": "private-app-token"}, workspace),
            {"ok": True, "passed": False, "number": 1, "version": "v1.0", "output": "[REDACTED]"})
        self.assertEqual(self.client.get(self.url).json()["port_range"], {"start": 20000, "end": 29999})

    def test_development_auto_ports_reserve_application_ports_and_readonly_roles_do_not_publish(self):
        content = Content(application={"start_command": "node server.js"}, environment={"DEBUG": "1"},
            port_mappings=[{"container_port": 3000}, {"container_port": 3000, "host_port": 20000, "service": "application"}]).model_dump()
        self.assertEqual(self.save(content).status_code, 201)
        detail = self.prepare(self.requirement(self.project_record))
        workspace = Path(detail["workspace"]["path"])
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.runtime_ports.occupied_ports", return_value=set()), \
            patch("agent_workbench.runtime_ports.available", return_value=True), patch("agent_workbench.runtime.start") as start:
            self.runner.executor.start_runtime(workspace, "fixture")
        self.assertEqual(start.call_args.kwargs["port_mappings"][0]["host_port"], 20001)
        self.assertEqual(start.call_args.kwargs["profile_environment"], {"DEBUG": "1"})
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.runtime.start") as start, \
            patch("agent_workbench.runtime_ports.occupied_ports") as occupied:
            self.runner.executor.start_runtime(workspace, "fixture", read_only_source=True, opencode_model="opencode/glm-5.3-flash-free")
            occupied.assert_not_called()
        self.assertEqual(start.call_args.kwargs["port_mappings"], [])
        self.assertEqual(start.call_args.kwargs["profile_environment"], {})

    def test_invalid_environment_ports_and_volume_boundaries_are_rejected(self):
        for value in [
            {"environment": {"DOCKER_HOST": "bad"}}, {"environment": {"LD_PRELOAD": "bad"}},
            {"environment": {"bad-key": "bad"}}, {"environment": {"APP": "bad\u0000value"}},
            {"port_mappings": [{"container_port": 4096}]},
            {"port_mappings": [{"container_port": 3000, "host_port": 19999}]},
            {"port_mappings": [{"container_port": 3000}, {"container_port": 3000}]},
            {"port_mappings": [{"container_port": 3000, "service": "application"}]},
            {"volume_mounts": [{"source": str(self.config_file), "target": "/workspace/project", "read_only": False}]},
            {"volume_mounts": [{"source": str(self.data), "target": "/mnt/data"}]},
            {"volume_mounts": [{"source": str(self.root), "target": "/mnt/data"}]},
            {"volume_mounts": [{"source": str(self.config_file), "target": "/var/run/docker.sock"}]},
        ]:
            with self.subTest(value=value):
                self.assertEqual(self.save({**Content().model_dump(), **value}).status_code, 422)

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

    def test_requirement_detail_exposes_only_public_runtime_port_fields(self):
        req = self.requirement(self.project_record)
        workspace = Path(self.detail(req)["workspace"]["path"])
        workspace.mkdir(parents=True)
        mapping = {"container_port": 8080, "host_port": 21000, "protocol": "tcp", "service": "agent", "private": "not-public"}
        write_json(workspace / "runtime.json", {"password": "private-runtime-password", "port_mappings": [mapping]})
        detail = self.detail(req)
        self.assertEqual(detail["port_mappings"], [{key: mapping[key] for key in ("container_port", "host_port", "protocol", "service")}])
        self.assertNotIn("private-runtime-password", json.dumps(detail))
        self.assertNotIn("not-public", json.dumps(detail))

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
            connection.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
        with patch("agent_workbench.resources.inventory", return_value=[]): result = backup(self.settings)
        manifest = verify(Path(result["file"]))
        self.assertEqual(manifest["schema_revision"], "0010")
        self.assertFalse(any("runtime-config/" in item["path"] for item in manifest["entries"]))
        target = self.root / "restored"; restore(Path(result["file"]), target)
        restored = Store(f"sqlite:///{target / 'workbench.db'}")
        try: self.assertEqual(restored.list(workspace_profiles), self.store.list(workspace_profiles))
        finally: restored.engine.dispose()
        workspace = target / "workspaces" / req["id"] / req["project_id"]
        self.assertFalse((workspace / "runtime-config").exists())
        staged = runtime_options(workspace, target)
        self.assertEqual(Path(staged["config_mounts"][0]["source"]).read_text(), "PRIVATE_FIXTURE=one\n")
