from contextlib import redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError


with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "poc"), *sys.path]):
    verification = importlib.import_module("verify_free_model")


class FreeModelTests(unittest.TestCase):
    model = "openrouter/fixture/model:free"
    credential = "fixture-openrouter-not-a-real-key"

    def catalog_response(self, pricing=None):
        value = {"data": [{"id": "fixture/model:free", "pricing":
                          {"prompt": "0", "completion": "0"} if pricing is None else pricing}]}
        return io.BytesIO(json.dumps(value).encode())

    def passing_report(self):
        return dict(agent_exit_code=0, agent_error_events=0, model_response_observed=True,
                    implementation_file_changed=True, regression_test_file_changed=True,
                    test_count=4, source_unchanged=True, tests_passed=True,
                    acceptance_behavior_passed=True)

    def test_complete_evidence_passes(self):
        self.assertTrue(verification.task_passed(self.passing_report()))

    def test_successful_cli_exit_with_error_event_is_failure(self):
        report = self.passing_report()
        report["agent_error_events"] = 1
        self.assertFalse(verification.task_passed(report))

    def test_missing_model_or_code_evidence_cannot_pass(self):
        for field, value in (("model_response_observed", False),
                             ("implementation_file_changed", False),
                             ("regression_test_file_changed", False),
                             ("test_count", 2), ("source_unchanged", False),
                             ("tests_passed", False), ("acceptance_behavior_passed", False)):
            with self.subTest(field=field):
                report = self.passing_report()
                report[field] = value
                self.assertFalse(verification.task_passed(report))

    def test_only_explicit_free_provider_models_are_accepted(self):
        self.assertEqual(verification.selected_provider(self.model), "openrouter")
        self.assertEqual(verification.selected_provider("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"), "openrouter")
        self.assertEqual(verification.selected_provider("opencode/glm-5.3-flash-free"), "opencode")
        for model in ("openrouter/fixture/model", "openrouter/free", "openrouter/fixture/model:free\n",
                      "other/fixture-free", "openrouter/fixture/model:free#paid",
                      "opencode/paid", "opencode/fixture-free\n", "opencode/vendor/model-free"):
            with self.subTest(model=model), self.assertRaises(verification.WorkbenchError):
                verification.selected_provider(model)

    def test_catalog_requires_complete_finite_zero_pricing(self):
        for pricing in ({"prompt": "0.001", "completion": "0"},
                        {"prompt": "0", "completion": "0", "request": "0.01"},
                        {"prompt": "0"}, {"prompt": "NaN", "completion": "0"},
                        {"prompt": "0", "completion": "unknown"}):
            with self.subTest(pricing=pricing), \
                    patch.object(verification, "urlopen", return_value=self.catalog_response(pricing)), \
                    self.assertRaises(verification.WorkbenchError):
                verification.openrouter_catalog_model(self.model)
        with patch.object(verification, "urlopen", return_value=self.catalog_response()) as requested:
            result = verification.openrouter_catalog_model(self.model)
        self.assertEqual(result["id"], "fixture/model:free")
        self.assertNotIn("Authorization", requested.call_args.args[0].headers)

    def test_openrouter_missing_key_does_not_create_fixture_or_start_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "run"
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), \
                    patch.object(sys, "argv", ["verify_free_model.py", "--root", str(root), "--model", self.model]), \
                    patch.object(verification.runtime, "start") as start, \
                    patch.object(verification, "urlopen") as requested, \
                    self.assertRaises(verification.WorkbenchError):
                verification.main()
            self.assertFalse(root.exists())
            start.assert_not_called()
            requested.assert_not_called()

    def test_charged_catalog_model_is_blocked_before_container_or_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "run"
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": self.credential}), \
                    patch.object(sys, "argv", ["verify_free_model.py", "--root", str(root), "--model", self.model]), \
                    patch.object(verification, "urlopen", return_value=self.catalog_response({"prompt": "1", "completion": "0"})), \
                    patch.object(verification.runtime, "start") as start, \
                    patch.object(verification.runtime, "docker") as docker, redirect_stdout(io.StringIO()):
                self.assertEqual(verification.main(), 1)
            start.assert_not_called()
            docker.assert_not_called()
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["status"], "FAILED_OR_BLOCKED")
            self.assertFalse(report["credentials_injected"])
            self.assertFalse(report.get("submission_attempted", False))

    def test_catalog_network_failure_does_not_expose_transport_details(self):
        with patch.object(verification, "urlopen", side_effect=URLError(self.credential)), \
                self.assertRaises(verification.WorkbenchError) as failure:
            verification.openrouter_catalog_model(self.model)
        self.assertNotIn(self.credential, str(failure.exception))

    def test_openrouter_routes_selected_provider_and_redacts_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "run"
            workspace = root / "workspaces/req-free/backend"

            def start(*args, **kwargs):
                (workspace / "runtime.json").write_text("{}")
                return {"container": "fixture-container"}

            def stop(*args):
                (workspace / "runtime.json").unlink()

            event = json.dumps({"type": "error", "error": {"message": self.credential}}) + "\n"
            captured = io.StringIO()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": self.credential}), \
                    patch.object(sys, "argv", ["verify_free_model.py", "--root", str(root), "--model", self.model]), \
                    patch.object(verification, "urlopen", return_value=self.catalog_response()), \
                    patch.object(verification.runtime, "start", side_effect=start) as started, \
                    patch.object(verification.runtime, "stop", side_effect=stop), \
                    patch.object(verification, "wait_ready", return_value={}), \
                    patch.object(verification.runtime, "docker", side_effect=[
                        subprocess.CompletedProcess([], 0, self.model + "\n", ""),
                        subprocess.CompletedProcess([], 0, event, self.credential),
                        subprocess.CompletedProcess([], 0, "# tests 2\n", self.credential),
                        subprocess.CompletedProcess([], 1, "", self.credential),
                    ]) as docker, redirect_stdout(captured):
                self.assertEqual(verification.main(), 1)
            self.assertEqual(started.call_args.kwargs["openrouter_model"], self.model)
            self.assertEqual(docker.call_args_list[0].args[-1], "openrouter")
            self.assertIn(self.model, docker.call_args_list[1].args)
            report = json.loads((root / "report.json").read_text())
            self.assertTrue(report["credentials_injected"])
            self.assertTrue(report["free_pricing_verified"])
            self.assertEqual(report["status"], "FAILED_OR_BLOCKED_MODEL_TASK")
            for path in root.iterdir():
                if path.is_file():
                    self.assertNotIn(self.credential, path.read_text())
            self.assertNotIn(self.credential, captured.getvalue())
            self.assertIn("[REDACTED]", (root / "agent-events.jsonl").read_text())

    def test_timeout_partial_output_redacts_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            partial = json.dumps({"type": "error", "message": self.credential}).encode()
            verification.save_agent_output(root, partial, self.credential.encode(), (self.credential,))
            for path in root.iterdir():
                self.assertNotIn(self.credential, path.read_text())

    def test_timeout_preserves_partial_output_stops_runtime_and_never_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "run"
            workspace = root / "workspaces/req-free/backend"
            partial = b'{"type":"text","sessionID":"ses_fixture"}\n'

            def start(*args, **kwargs):
                (workspace / "runtime.json").write_text("{}")
                return {"container": "fixture-container"}

            def stop(*args):
                (workspace / "runtime.json").unlink()

            timeout = subprocess.TimeoutExpired("fixture", 1, output=partial, stderr=b"partial stderr")
            with patch.object(sys, "argv", ["verify_free_model.py", "--root", str(root),
                                           "--model", "opencode/glm-5.3-flash-free"]), \
                    patch.object(verification.runtime, "start", side_effect=start) as started, \
                    patch.object(verification.runtime, "stop", side_effect=stop) as stopped, \
                    patch.object(verification, "wait_ready", return_value={}), \
                    patch.object(verification.runtime, "docker", side_effect=[
                        subprocess.CompletedProcess([], 0, "opencode/glm-5.3-flash-free\n", ""),
                        timeout,
                    ]) as docker, redirect_stdout(io.StringIO()):
                self.assertEqual(verification.main(), 1)

            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["status"], "UNKNOWN_TASK_STATE_AFTER_TIMEOUT")
            self.assertTrue(report["runtime_stopped"])
            self.assertEqual((root / "agent-events.jsonl").read_bytes(), partial)
            self.assertEqual((root / "agent-stderr.txt").read_text(), "partial stderr")
            if os.name != "nt":
                self.assertEqual((root / "agent-events.jsonl").stat().st_mode & 0o777, 0o600)
            self.assertEqual([call.args[3] for call in docker.call_args_list], ["models", "run"])
            stopped.assert_called_once_with(workspace)
            self.assertEqual(started.call_args.kwargs["opencode_model"], "opencode/glm-5.3-flash-free")


if __name__ == "__main__":
    unittest.main()
