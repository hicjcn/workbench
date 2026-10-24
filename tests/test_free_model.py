from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "poc"), *sys.path]):
    verification = importlib.import_module("verify_free_model")


class FreeModelTests(unittest.TestCase):
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

    def test_timeout_preserves_partial_output_stops_runtime_and_never_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            workspace = root / "workspaces/req-free/backend"
            partial = b'{"type":"text","sessionID":"ses_fixture"}\n'

            def start(*args):
                (workspace / "runtime.json").write_text("{}")
                return {"container": "fixture-container"}

            def stop(*args):
                (workspace / "runtime.json").unlink()

            timeout = subprocess.TimeoutExpired("fixture", 1, output=partial, stderr=b"partial stderr")
            with patch.object(sys, "argv", ["verify_free_model.py", "--root", str(root)]), \
                    patch.object(verification.runtime, "start", side_effect=start), \
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
            self.assertEqual((root / "agent-events.jsonl").stat().st_mode & 0o777, 0o600)
            self.assertEqual([call.args[3] for call in docker.call_args_list], ["models", "run"])
            stopped.assert_called_once_with(workspace)


if __name__ == "__main__":
    unittest.main()
