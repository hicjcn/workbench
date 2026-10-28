"""Stop reconciles only the recorded container and preserves uncertain state."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from agent_workbench import runtime
from agent_workbench.workspace import WorkbenchError, create_workspace, git, write_json


class RuntimeStopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        source = self.root / "source"
        source.mkdir()
        (source / "main.txt").write_text("preserved source\n", encoding="utf-8")
        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Runtime Stop Fixture")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "add", "--all")
        git(source, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        created = create_workspace(source, self.root / "workspaces", "req-stop", "backend")
        self.workspace = Path(created["workspace"])
        self.state_path = self.workspace / "runtime.json"
        self.state = {"container": "awb-Recorded-123", "network": "awb-network-fixture", "phase": "STARTED"}
        self.write_state()

    def write_state(self):
        write_json(self.state_path, self.state)
        return self.state_path.read_bytes()

    def inspected_container(self, *, owned=True):
        owner = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:10] if owned else "different-workspace"
        detail = {"Id": "recorded-container-id", "Config": {"Labels": {"awb.workspace": owner}}}
        return subprocess.CompletedProcess([], 0, json.dumps([detail]), "")

    def assert_preserved(self, before):
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual((self.workspace / "repo/main.txt").read_text(encoding="utf-8"), "preserved source\n")

    def test_confirmed_missing_container_clears_stale_record(self):
        errors = (
            f"Error response from daemon: No such container: {self.state['container']}",
            f"error RESPONSE from DAEMON: no SUCH CONTAINER: {self.state['container']}\n",
            f"Error: No such object: {self.state['container']}\n",
            f"no such container: {self.state['container']}",
        )
        for error in errors:
            with self.subTest(error=error):
                self.write_state()
                absent = subprocess.CompletedProcess([], 1, "[]\n", error)
                done = subprocess.CompletedProcess([], 0, "", "")
                with patch.object(runtime, "docker", side_effect=[absent, done]) as docker:
                    result = runtime.stop(self.workspace)
                self.assertTrue(result["stopped"])
                self.assertTrue(result["code_and_agent_state_preserved"])
                self.assertFalse(self.state_path.exists())
                self.assertEqual(docker.call_args_list, [
                    call("inspect", "--type", "container", self.state["container"], check=False),
                    call("network", "rm", self.state["network"], check=False),
                ])
                self.assertEqual((self.workspace / "repo/main.txt").read_text(encoding="utf-8"), "preserved source\n")

    def test_uncertain_inspection_preserves_record_and_reports_docker_error(self):
        errors = (
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
            "error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.",
            "dial unix /var/run/docker.sock: connect: No such file or directory",
            "permission denied while trying to connect to the Docker daemon socket",
            f"Error response from daemon: No such container: {self.state['container']}-other",
            f"Error: No such object: {self.state['container'].lower()}",
            f"Error response from daemon: No such image: {self.state['container']}",
            f"Error: No such container: {self.state['container']}\nCannot connect to the Docker daemon",
            f"Cannot connect to the Docker daemon\nError: No such container: {self.state['container']}",
        )
        for error in errors:
            with self.subTest(error=error):
                before = self.write_state()
                failed = subprocess.CompletedProcess([], 17, "", error)
                with patch.object(runtime, "docker", return_value=failed) as docker:
                    with self.assertRaises(WorkbenchError) as raised:
                        runtime.stop(self.workspace)
                message = str(raised.exception)
                self.assertIn("retaining state", message)
                self.assertIn("exit 17", message)
                self.assertIn(error, message)
                self.assertIn("AWB_DOCKER_ENDPOINT", message)
                docker.assert_called_once_with("inspect", "--type", "container", self.state["container"], check=False)
                self.assert_preserved(before)

    def test_inspection_diagnostic_is_bounded(self):
        before = self.state_path.read_bytes()
        error = "connection failure: " + "x" * 1600 + "UNBOUNDED-TAIL"
        with patch.object(runtime, "docker", return_value=subprocess.CompletedProcess([], 1, "", error)):
            with self.assertRaises(WorkbenchError) as raised:
                runtime.stop(self.workspace)
        self.assertIn(error[:1500], str(raised.exception))
        self.assertNotIn("UNBOUNDED-TAIL", str(raised.exception))
        self.assert_preserved(before)

    def test_owned_container_is_removed_before_record_is_cleared(self):
        def docker_response(*args, **kwargs):
            if args[0] == "inspect":
                return self.inspected_container()
            self.assertTrue(self.state_path.exists(), "retain the journal until Docker removal succeeds")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch.object(runtime, "docker", side_effect=docker_response) as docker:
            result = runtime.stop(self.workspace)
        self.assertTrue(result["stopped"])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(docker.call_args_list, [
            call("inspect", "--type", "container", self.state["container"], check=False),
            call("rm", "--force", self.state["container"]),
            call("network", "rm", self.state["network"], check=False),
        ])
        self.assertEqual((self.workspace / "repo/main.txt").read_text(encoding="utf-8"), "preserved source\n")

    def test_container_with_different_owner_is_not_removed(self):
        before = self.state_path.read_bytes()
        with patch.object(runtime, "docker", return_value=self.inspected_container(owned=False)) as docker:
            with self.assertRaisesRegex(WorkbenchError, "different workspace"):
                runtime.stop(self.workspace)
        docker.assert_called_once_with("inspect", "--type", "container", self.state["container"], check=False)
        self.assert_preserved(before)

    def test_removal_failure_preserves_runtime_record(self):
        before = self.state_path.read_bytes()
        with patch.object(runtime, "docker", side_effect=[self.inspected_container(), WorkbenchError("Docker removal failed")]) as docker:
            with self.assertRaisesRegex(WorkbenchError, "Docker removal failed"):
                runtime.stop(self.workspace)
        self.assertEqual(docker.call_args_list, [
            call("inspect", "--type", "container", self.state["container"], check=False),
            call("rm", "--force", self.state["container"]),
        ])
        self.assert_preserved(before)


if __name__ == "__main__":
    unittest.main()
