"""Credential-debug startup keeps the real mount/lifecycle gates without CLI health."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
from agent_workbench import runtime
from agent_workbench.agent_clis import digest
from agent_workbench.api import create_app
from agent_workbench.profiles import Content
from agent_workbench.workspace import WorkbenchError, write_json
from test_control_plane import ControlFixture


class ForceStartTests(ControlFixture):
    def set_model(self, model):
        self.settings = replace(self.settings, model=model)
        self.runner.settings = self.runner.executor.settings = self.settings
        self.client = TestClient(create_app(self.settings, self.store))
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer " + self.settings.access_token

    def prepare_profile(self):
        project = self.project()
        configuration = self.root / "credentials.conf"
        configuration.write_text("fixture-private-config")
        directory = self.root / "credential-directory"
        directory.mkdir()
        profile = Content(image="fixture:local", run_as_root=True,
            mounts=[{"source": str(configuration), "target": "/run/workbench-config/credentials.conf"}],
            volume_mounts=[{"source": str(directory), "target": "/mnt/credentials", "read_only": False}],
            environment={"TEAM_TOKEN": "fixture-private-token"},
            port_mappings=[{"container_port": 8080, "host_port": 21000}])
        saved = self.client.post(f"/api/v1/projects/{project['id']}/profiles", json={"content": profile.model_dump()})
        self.assertEqual(saved.status_code, 201, saved.text)
        requirement = self.requirement(project)
        self.action(requirement, "prepare")
        with patch("agent_workbench.runtime.docker", return_value=subprocess.CompletedProcess([], 0, json.dumps([{"Id": "sha256:fixed"}]), "")):
            self.runner.run_once()
        detail = self.detail(requirement)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED", detail)
        return detail

    def docker(self, *args, **options):
        if args[:2] == ("network", "inspect"):
            return subprocess.CompletedProcess([], 1, "", "No such network")
        if args[0] == "port":
            return subprocess.CompletedProcess([], 0, "127.0.0.1:4096\n", "")
        if args[0] == "inspect":
            labels = {"awb.workspace": hashlib.sha256(str(self.workspace).encode()).hexdigest()[:10]}
            return subprocess.CompletedProcess([], 0, json.dumps([{"State": {"Running": True, "Status": "running"}, "Config": {"Labels": labels}}]), "")
        return subprocess.CompletedProcess([], 0, "", "")

    def force_start(self, requirement):
        self.workspace = Path(requirement["workspace"]["path"])
        response = self.action(requirement, "force_start")
        self.assertEqual(response.status_code, 202, response.text)
        with patch("agent_workbench.runtime.docker", side_effect=self.docker) as docker, \
                patch("agent_workbench.resources.inventory", return_value=[]), \
                patch("agent_workbench.runtime_ports.occupied_ports", return_value=set()), \
                patch("agent_workbench.runtime_ports.available", return_value=True), \
                patch("agent_workbench.runtime.probe") as probe:
            self.runner.run_once()
        probe.assert_not_called()
        detail = self.detail(requirement)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED", detail["jobs"][0])
        self.assertEqual(detail["workspace"]["environment"], "DEBUG_RUNNING")
        return detail, docker

    def test_force_start_preserves_failed_preflight_mounts_environment_and_image_without_cli_or_model_credentials(self):
        requirement = self.prepare_profile()
        workspace = Path(requirement["workspace"]["path"])
        report = {"status": "FAILED", "profile_digest": requirement["profile"]["content_digest"], "checks": [{"name": "agent_cli", "passed": False}]}
        write_json(workspace / "preflight-report.json", report)
        self.set_model("openrouter/fixture/requires-login:free")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            detail, docker = self.force_start(requirement)
        self.assertEqual(detail["preflight"], report)
        run = next(call for call in docker.call_args_list if call.args[0] == "run")
        self.assertEqual(run.args[-1], "sha256:fixed")
        self.assertNotIn("--entrypoint", run.args)
        self.assertIn("127.0.0.1:21000:8080/tcp", run.args)
        self.assertEqual(run.args[run.args.index("--user") + 1].split(":")[0], "0")
        config_mount = next(value for value in run.args if "dst=/run/workbench-config/credentials.conf" in value)
        self.assertIn("readonly", config_mount)
        credential_mount = next(value for value in run.args if "dst=/mnt/credentials" in value)
        self.assertNotIn("readonly", credential_mount)
        self.assertEqual(run.kwargs["environment"]["TEAM_TOKEN"], "fixture-private-token")
        self.assertNotIn("fixture-private-token", " ".join(run.args))
        self.assertFalse(any(call.args[0] == "exec" for call in docker.call_args_list))
        journal = json.loads((workspace / "runtime.json").read_text())
        self.assertEqual(journal["phase"], "DEBUG_RUNNING")
        self.assertNotIn("agent_service", journal)
        hints = detail["debug_runtime"]
        self.assertEqual(set(hints), {"container", "home", "shell_command", "serve_command"})
        self.assertIn(journal["container"], hints["shell_command"])
        self.assertEqual(hints["serve_command"], "opencode serve --hostname 0.0.0.0 --port 4096")
        self.assertNotIn(journal["password"], json.dumps(detail))
        self.assertEqual(json.loads((workspace / "agent-home/.config/opencode/opencode.json").read_text())["model"], self.settings.model)
        with patch("agent_workbench.runtime.OpenCodeClient") as client:
            with self.assertRaisesRegex(WorkbenchError, "not ready"):
                runtime.probe(workspace)
        client.assert_not_called()

    def test_debug_mode_blocks_business_actions_even_without_profile_and_preserves_stop_control(self):
        requirement, _ = self.force_start(self.prepared())
        for action in ("start", "task", "build", "verify", "app_start", "app_check"):
            with self.subTest(action=action):
                self.assertEqual(self.action(requirement, action, prompt="fixture task").status_code, 202)
                with patch("agent_workbench.runtime.docker") as docker:
                    self.runner.run_once()
                docker.assert_not_called()
                detail = self.detail(requirement)
                self.assertEqual(detail["jobs"][0]["status"], "FAILED")
                self.assertIn("排查环境", detail["jobs"][0]["error"])
                self.assertEqual(detail["workspace"]["environment"], "DEBUG_RUNNING")
        self.action(requirement, "stop")
        with patch("agent_workbench.runtime.docker", side_effect=self.docker), patch("agent_workbench.resources.inventory", return_value=[]):
            self.runner.run_once()
        stopped = self.detail(requirement)
        self.assertEqual(stopped["jobs"][0]["status"], "SUCCEEDED", stopped["jobs"][0])
        self.assertEqual(stopped["workspace"]["environment"], "STOPPED")
        self.assertIsNone(stopped["debug_runtime"])
        self.assertFalse((self.workspace / "runtime.json").exists())

    def test_force_start_needs_prepared_workspace_and_refuses_existing_runtime_without_replay(self):
        requirement = self.requirement()
        self.action(requirement, "force_start")
        with patch("agent_workbench.runtime.docker") as docker:
            self.runner.run_once()
        docker.assert_not_called()
        self.assertIn("先创建", self.detail(requirement)["jobs"][0]["error"])
        requirement, _ = self.force_start(self.prepared())
        previous = (self.workspace / "runtime.json").read_bytes()
        self.action(requirement, "force_start")
        with patch("agent_workbench.runtime.docker") as docker:
            self.runner.run_once()
        docker.assert_not_called()
        self.assertIn("已有容器运行记录", self.detail(requirement)["jobs"][0]["error"])
        self.assertEqual((self.workspace / "runtime.json").read_bytes(), previous)

    def test_force_start_still_checks_capacity_and_pinned_cli_identity(self):
        requirement = self.prepared()
        self.action(requirement, "force_start")
        with patch("agent_workbench.resources.inventory", return_value=[{}] * self.settings.max_containers), \
                patch("agent_workbench.runtime.docker") as docker:
            self.runner.run_once()
        docker.assert_not_called()
        self.assertIn("上限", self.detail(requirement)["jobs"][0]["error"])
        workspace = Path(requirement["workspace"]["path"])
        manifest = json.loads((workspace / "workspace.json").read_text())
        manifest["agent_cli"]["id"] = "tampered-cli"
        write_json(workspace / "workspace.json", manifest)
        self.action(requirement, "force_start")
        with patch("agent_workbench.runtime.docker") as docker:
            self.runner.run_once()
        docker.assert_not_called()
        self.assertEqual(self.detail(requirement)["jobs"][0]["status"], "FAILED")

    def test_image_entrypoint_exit_keeps_reconciliation_journal_instead_of_debug_success(self):
        requirement = self.prepared()
        workspace = self.workspace = Path(requirement["workspace"]["path"])
        self.action(requirement, "force_start")
        def exited(*args, **options):
            if args[0] == "inspect":
                return subprocess.CompletedProcess([], 0, json.dumps([{"State": {"Running": False, "Status": "exited", "ExitCode": 7}}]), "")
            return self.docker(*args, **options)
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.runtime.docker", side_effect=exited):
            self.runner.run_once()
        detail = self.detail(requirement)
        self.assertEqual(detail["jobs"][0]["status"], "FAILED")
        self.assertIn("未保持运行", detail["jobs"][0]["error"])
        self.assertEqual(json.loads((workspace / "runtime.json").read_text())["phase"], "NEEDS_RECONCILIATION")
        self.assertIsNone(detail["debug_runtime"])

    def test_public_debug_hints_ignore_private_journal_values_and_require_owned_container(self):
        requirement, _ = self.force_start(self.prepared())
        journal_path = self.workspace / "runtime.json"
        state = json.loads(journal_path.read_text())
        state["serve_argv"] = ["private-journal-credential"]
        write_json(journal_path, state, private=True)
        with patch("agent_workbench.runtime.docker") as docker:
            detail = self.detail(requirement)
        docker.assert_not_called()
        self.assertNotIn("private-journal-credential", json.dumps(detail))
        state["container"] = "unmanaged; echo private-value"
        write_json(journal_path, state, private=True)
        self.assertIsNone(self.detail(requirement)["debug_runtime"])

    def test_available_model_credential_is_forwarded_by_name_in_debug_mode(self):
        self.set_model("openrouter/fixture/model:free")
        secret = "fixture-router-secret"
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": secret}):
            detail, calls = self.force_start(self.prepared())
        run = next(call for call in calls.call_args_list if call.args[0] == "run")
        self.assertIn("OPENROUTER_API_KEY", run.args)
        self.assertEqual(run.kwargs["environment"]["OPENROUTER_API_KEY"], secret)
        self.assertNotIn(secret, " ".join(run.args))
        self.assertNotIn(secret, json.dumps(detail))
        configuration = json.loads((self.workspace / "agent-home/.config/opencode/opencode.json").read_text())
        self.assertEqual(configuration["model"], self.settings.model)
        self.assertEqual(configuration["provider"]["openrouter"]["options"]["apiKey"], "{env:OPENROUTER_API_KEY}")

    def test_public_serve_hint_redacts_credential_before_shell_quoting(self):
        requirement, _ = self.force_start(self.prepared())
        secret = "credential'with quote"
        manifest_path = self.workspace / "workspace.json"
        manifest = json.loads(manifest_path.read_text())
        selected = manifest["agent_cli"]
        selected["content"]["serve_command"] = "opencode serve --token " + shlex.quote(secret)
        selected["content"]["credential_env"] = ["TEAM_TOKEN"]
        selected["content_digest"] = digest(selected["content"])
        write_json(manifest_path, manifest)
        with patch.dict(os.environ, {"TEAM_TOKEN": secret}):
            hints = runtime.describe_debug_runtime(self.workspace)
        self.assertEqual(shlex.split(hints["serve_command"]), ["opencode", "serve", "--token", "[REDACTED]"])
