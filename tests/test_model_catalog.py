"""CLI discovery stays pinned and never copies raw CLI diagnostics into records."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_workbench import agent_clis, model_catalog
from agent_workbench.workspace import WorkbenchError, write_json
from test_control_plane import ControlFixture


class ModelParserTests(unittest.TestCase):
    def test_lines_strip_terminal_colors_and_deduplicate_without_turning_banners_into_models(self):
        rows = model_catalog.parse("Models available:\n\x1b[32mteam/alpha-v2\x1b[0m\nteam/alpha-v2\nopenrouter/team/beta:free\n")
        self.assertEqual(rows, [{"id": "team/alpha-v2", "label": "team/alpha-v2"},
            {"id": "openrouter/team/beta:free", "label": "openrouter/team/beta:free"}])

    def test_json_arrays_and_wrappers_preserve_names_and_empty_is_explicit(self):
        for value in (["team/alpha", {"id": "team/beta", "name": "Beta"}],
                {"models": ["team/alpha", {"id": "team/beta", "name": "Beta"}]},
                {"data": ["team/alpha", {"id": "team/beta", "label": "Beta"}]}):
            with self.subTest(value=value):
                self.assertEqual(model_catalog.parse(json.dumps(value)),
                    [{"id": "team/alpha", "label": "team/alpha"}, {"id": "team/beta", "label": "Beta"}])
        self.assertEqual(model_catalog.parse("[]"), [])
        self.assertEqual(model_catalog.parse("\n"), [])

    def test_malformed_oversized_or_banner_only_output_is_rejected_without_echoing_it(self):
        for value in ('[{"id":"private token"}]', '{broken-private-output', 'Login private-output required', 'x' * (model_catalog.MAX_OUTPUT + 1)):
            with self.subTest(value=value[:30]), self.assertRaises(WorkbenchError) as caught:
                model_catalog.parse(value)
            self.assertNotIn("private-output", str(caught.exception))

    def test_old_cli_documents_and_builtin_digest_remain_unchanged(self):
        old = {"protocol": "opencode", "serve_command": "team-code serve --hostname {host} --port {port}",
            "run_command": "team-code run --attach {endpoint} --model {model} --format json",
            "version_command": "/opt/team-code --version", "expected_version": "", "timeout_seconds": 300, "credential_env": []}
        chosen = {"content": old, "content_digest": agent_clis.digest(old)}
        self.assertIsNone(agent_clis.checked(chosen)["list_models_command"])
        self.assertEqual(agent_clis.command(chosen, "list_models"), ["/opt/team-code", "models"])
        baseline = {**old, "serve_command": "opencode serve --hostname {host} --port {port}",
            "run_command": "opencode run --attach {endpoint} --model {model} --format json", "version_command": "opencode --version", "expected_version": "1.19.4"}
        self.assertEqual(agent_clis.builtin()["content_digest"], agent_clis.digest(baseline))
        self.assertNotIn("list_models_command", agent_clis.builtin()["content"])

    @unittest.skipUnless(shutil.which("node"), "Requires Node for actual discovery supervisor")
    def test_discovery_child_omits_only_managed_filter_and_preserves_home_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            opencode = home / ".config/opencode"
            credentials = home / ".config/team"
            opencode.mkdir(parents=True)
            credentials.mkdir()
            (credentials / "auth.json").write_text('{"controlled":true}')
            managed = Path(temporary) / "managed.json"
            original = '{"enabled_providers":["legacy"],"provider":{"legacy":{"whitelist":["default"]}}}'
            managed.write_text(original)
            os.link(managed, opencode / "opencode.json")
            (opencode / "opencode.jsonc").write_text('// user config retained\n{}')
            script = model_catalog.LIST_SCRIPT.replace("'/home/node/.config/opencode/opencode.json'", json.dumps(str(managed)))
            child = """const fs=require('node:fs'),path=require('node:path');
console.log(JSON.stringify({home:process.env.HOME,view:process.env.XDG_CONFIG_HOME,
 managedDefault:fs.existsSync(path.join(process.env.XDG_CONFIG_HOME,'opencode/opencode.json')),
 userConfig:fs.existsSync(path.join(process.env.XDG_CONFIG_HOME,'opencode/opencode.jsonc')),
 credential:fs.existsSync(path.join(process.env.XDG_CONFIG_HOME,'team/auth.json')),
 config:JSON.parse(fs.readFileSync(process.env.OPENCODE_CONFIG,'utf8'))}));"""
            result = subprocess.run([shutil.which("node"), "-e", script, json.dumps([shutil.which("node"), "-e", child])],
                env={**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "OPENCODE_CONFIG": str(managed)},
                capture_output=True, text=True, timeout=10)
            envelope = json.loads(result.stdout)
            self.assertTrue(envelope["ok"], envelope)
            observed = json.loads(envelope["stdout"])
            self.assertEqual(observed["home"], str(home))
            self.assertFalse(observed["managedDefault"])
            self.assertTrue(observed["userConfig"])
            self.assertTrue(observed["credential"])
            self.assertEqual(observed["config"], {})
            self.assertFalse(Path(observed["view"]).exists())
            self.assertEqual(managed.read_text(), original)
            self.assertEqual((opencode / "opencode.json").read_text(), original)


class ModelCatalogTests(ControlFixture):
    def prepared_runtime(self, phase="DEBUG_RUNNING"):
        detail = self.prepared()
        self.workspace = Path(detail["workspace"]["path"])
        self.selected = detail["agent_cli"]
        suffix = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:10]
        self.container = f"awb-{detail['id'][:20]}-{detail['project_id'][:20]}-{suffix}"
        self.state = {"container": self.container, "phase": phase, "image": "sha256:fixture",
            "run_as_root": True, "container_home": "/root", "agent_cli": self.selected}
        write_json(self.workspace / "runtime.json", self.state, private=True)
        self.inspection = {"State": {"Running": True}, "Image": "sha256:fixture",
            "Config": {"Labels": {"awb.managed": "true", "awb.workspace": suffix, "awb.kind": "agent"}},
            "Mounts": [{"Type": "bind", "Source": str(self.workspace / "repo"), "Destination": "/workspace/project"}]}
        return detail

    def run_discovery(self, detail, payload=None, *, stderr="", inspection=None):
        def docker(*args, **options):
            if args[0] == "inspect": return subprocess.CompletedProcess([], 0, json.dumps([inspection or self.inspection]), "")
            self.assertEqual(args[:2], ("exec", self.container))
            self.assertEqual(args[-4:-2], ("node", "-e"))
            self.assertEqual(json.loads(args[-1]), ["opencode", "models"])
            self.assertEqual(options["timeout"], 40)
            return subprocess.CompletedProcess([], 0, json.dumps(payload or {"ok": True, "stdout": "team/alpha\nteam/beta\n"}), stderr)
        queued = self.action(detail, "list_models")
        self.assertEqual(queued.status_code, 202, queued.text)
        with patch("agent_workbench.runtime.docker", side_effect=docker) as calls:
            self.runner.run_once()
        return self.detail(detail), calls

    def test_api_read_does_not_run_docker_and_successful_discovery_is_pinned(self):
        detail = self.prepared_runtime()
        endpoint = f"/api/v1/requirements/{detail['id']}/models"
        with patch("agent_workbench.runtime.docker") as docker:
            self.assertEqual(self.client.get(endpoint).json(), {"status": "NOT_LOADED", "models": []})
        docker.assert_not_called()
        detail, calls = self.run_discovery(detail)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED", detail["jobs"][0])
        report = self.client.get(endpoint).json()
        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["agent_cli_id"], self.selected["id"])
        self.assertEqual(report["agent_cli_digest"], self.selected["content_digest"])
        self.assertEqual(report["image"], self.state["image"])
        self.assertEqual(model_catalog.require_models(self.workspace, ["team/alpha"]), report)
        with self.assertRaises(WorkbenchError): model_catalog.require_models(self.workspace, ["team/missing"])
        self.assertEqual(len(calls.call_args_list), 2)
        self.assertNotIn("stderr", json.dumps(report))

    def test_failed_refresh_clears_previous_models_and_does_not_persist_stderr_or_stdout(self):
        detail = self.prepared_runtime()
        self.run_discovery(detail)
        detail, _ = self.run_discovery(detail, {"ok": False, "error": "exit", "exit_code": 7}, stderr="PRIVATE-LOGIN-CREDENTIAL")
        self.assertEqual(detail["jobs"][0]["status"], "FAILED")
        report = model_catalog.read(self.workspace)
        self.assertEqual(report["status"], "FAILED")
        self.assertEqual(report["models"], [])
        self.assertNotIn("PRIVATE-LOGIN-CREDENTIAL", (self.workspace / model_catalog.REPORT).read_text())
        self.assertNotIn("PRIVATE-LOGIN-CREDENTIAL", json.dumps(detail))

    def test_timeout_and_empty_catalog_are_distinct(self):
        detail = self.prepared_runtime()
        failed, _ = self.run_discovery(detail, {"ok": False, "error": "timeout"})
        self.assertIn("30 秒", failed["jobs"][0]["error"])
        success, _ = self.run_discovery(detail, {"ok": True, "stdout": "[]"})
        self.assertEqual(success["jobs"][0]["status"], "SUCCEEDED")
        self.assertEqual(model_catalog.read(self.workspace)["status"], "EMPTY")

    def test_foreign_container_or_missing_runtime_is_not_executed(self):
        detail = self.prepared_runtime()
        wrong = json.loads(json.dumps(self.inspection))
        wrong["Config"]["Labels"]["awb.workspace"] = "someone-else"
        failed, calls = self.run_discovery(detail, inspection=wrong)
        self.assertEqual(failed["jobs"][0]["status"], "FAILED")
        self.assertEqual(len(calls.call_args_list), 1)
        (self.workspace / "runtime.json").unlink()
        self.action(detail, "list_models")
        with patch("agent_workbench.runtime.docker") as docker: self.runner.run_once()
        docker.assert_not_called()
        self.assertEqual(model_catalog.read(self.workspace)["models"], [])

    def test_catalog_is_invalid_after_fixed_profile_or_cli_changes(self):
        detail = self.prepared_runtime()
        self.run_discovery(detail)
        path = self.workspace / "workspace.json"
        manifest = json.loads(path.read_text())
        manifest["runtime_profile"] = {"image_id": "sha256:fixture", "content_digest": "new-credential-profile"}
        write_json(path, manifest)
        self.assertEqual(model_catalog.read(self.workspace), {"status": "NOT_LOADED", "models": []})

    def test_listing_command_is_versioned_and_rejects_shell(self):
        content = agent_clis.Content(list_models_command="team-code available --json")
        chosen = {"content": content.model_dump(), "content_digest": agent_clis.digest(content.model_dump())}
        self.assertEqual(agent_clis.command(chosen, "list_models"), ["team-code", "available", "--json"])
        for value in ("", "team-code models | cat", "team-code models {model}"):
            with self.assertRaises(ValueError): agent_clis.Content(list_models_command=value)
