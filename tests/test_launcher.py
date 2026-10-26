"""Real local process lifecycle with isolated SQLite and no model/container tasks."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import httpx

from agent_workbench.configuration import Configuration, launcher_directory
from agent_workbench.launcher import owned_process, process_info, read_state
from agent_workbench.workspace import write_json

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "workbench.toml"
        self.web = self.root / "web"; self.web.mkdir(); (self.web / "assets").mkdir(); (self.web / "index.html").write_text("fixture", encoding="utf-8")
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0)); self.port = reserved.getsockname()[1]
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("AWB_")}
        self.environment.update(PYTHONUTF8="1", PYTHONPATH=str(ROOT / "src"))
        self.write_config()
        self.addCleanup(self.cleanup)

    def write_config(self, extra=""):
        self.file.write_text('[workbench]\nport=' + str(self.port) + '\nuse_sqlite=true\nmodel="opencode/glm-5.3-flash-free"\ndata_dir="data"\nweb_dir=' + json.dumps(str(self.web)) +
            '\n[launcher]\nlogs_dir="logs"\nopen_browser=false\nrequire_docker=false\nstop_managed_environments=false\nstartup_timeout_seconds=60\nstop_timeout_seconds=60\n' + extra, encoding="utf-8")

    def command(self, action, *, bootstrap=False, expect=0):
        argv = [sys.executable, str(ROOT / "scripts/control.py")] if bootstrap else [sys.executable, "-m", "agent_workbench.launcher"]
        result = subprocess.run([*argv, action, "--config", str(self.file), "--no-browser"], cwd=ROOT, env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=75)
        self.assertNotIn("fixture-launcher-private-value", result.stdout + result.stderr)
        if expect is not None: self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def cleanup(self):
        try: self.command("stop", bootstrap=True, expect=None)
        except (subprocess.TimeoutExpired, OSError): pass
        state = read_state(launcher_directory(self.file))
        if state:
            deadline = time.monotonic() + 5
            while owned_process(state, state["instance"]) and time.monotonic() < deadline: time.sleep(0.1)

    def test_start_status_duplicate_start_stop_and_restart_preserve_data(self):
        self.write_config('[environment]\nAGENT_LAUNCHER_TEST_TOKEN="fixture-launcher-private-value"\n')
        first = json.loads(self.command("start").stdout)
        self.assertEqual(first["phase"], "RUNNING")
        second = json.loads(self.command("start").stdout)
        self.assertTrue(second["already_running"]); self.assertEqual(first["instance"], second["instance"])
        data = self.root / "data"
        token = (data / "access-token").read_text(encoding="utf-8")
        with httpx.Client(base_url=first["api_url"], trust_env=False, headers={"Authorization": "Bearer " + token}) as client:
            self.assertEqual(client.get("/").text, "fixture")
            self.assertEqual(client.get("/api/v1/config").json()["data_dir"], str(data))
        state = read_state(launcher_directory(self.file))
        self.assertNotIn("fixture-launcher-private-value", json.dumps(state))
        stopped = json.loads(self.command("stop", bootstrap=True).stdout)
        self.assertEqual(stopped["phase"], "STOPPED")
        self.assertFalse(owned_process(state, state["instance"]))
        self.assertEqual(json.loads(self.command("stop", bootstrap=True).stdout)["phase"], "STOPPED")
        restarted = json.loads(self.command("start").stdout)
        self.assertNotEqual(first["instance"], restarted["instance"])
        self.assertEqual((data / "access-token").read_text(encoding="utf-8"), token)
        self.command("stop", bootstrap=True)
        for path in (self.root / "logs").rglob("*.log"):
            self.assertNotIn("fixture-launcher-private-value", path.read_text(encoding="utf-8"))

    def test_stop_still_targets_original_instance_after_invalid_config_edit(self):
        first = json.loads(self.command("start").stdout)
        self.file.write_text('[workbench]\nport="invalid edited configuration"\n', encoding="utf-8")
        observed = json.loads(self.command("status", bootstrap=True).stdout)
        self.assertEqual(observed["instance"], first["instance"])
        self.assertEqual(json.loads(self.command("stop", bootstrap=True).stdout)["phase"], "STOPPED")

    def test_pid_reuse_does_not_signal_or_terminate_an_unrelated_process(self):
        foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=self.environment)
        self.addCleanup(lambda: foreign.poll() is None and (foreign.terminate(), foreign.wait(timeout=10)))
        directory = launcher_directory(self.file); directory.mkdir(parents=True)
        state = {**process_info(foreign.pid), "instance": uuid.uuid4().hex, "phase": "RUNNING", "control_port": self.port,
            "control_token": secrets.token_urlsafe(32), "api_url": f"http://127.0.0.1:{self.port}", "data_dir": str(self.root / "data"), "logs_dir": str(self.root / "logs"),
            "python_executable": sys.executable, "storage": "sqlite", "stop_managed_environments": False, "stop_timeout_seconds": 10, "roles": {}}
        write_json(directory / "instance.json", state)
        self.assertFalse(owned_process(state, state["instance"]))
        self.assertEqual(json.loads(self.command("status", expect=2).stdout)["phase"], "STALE")
        self.command("stop", bootstrap=True)
        self.assertIsNone(foreign.poll())

    def test_port_conflict_fails_without_adopting_the_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port)); listener.listen()
            self.command("start", expect=1)
            self.assertEqual(listener.getsockname()[1], self.port)
            state = read_state(launcher_directory(self.file))
            self.assertEqual(state["phase"], "FAILED")
            self.assertFalse(owned_process(state, state["instance"]))

    def test_setup_rejects_live_instance_before_installing_dependencies(self):
        first = json.loads(self.command("start").stdout)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/control.py"), "setup", "--config", str(self.file), "--skip-image-build"], cwd=ROOT, env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Stop managed instances", result.stderr)
        self.assertEqual(json.loads(self.command("status").stdout)["instance"], first["instance"])

    def test_setup_rejects_manually_started_controller_without_a_launcher_record(self):
        from agent_workbench.resources import controller_lock
        with controller_lock(self.root / "data"):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/control.py"), "setup", "--config", str(self.file), "--skip-image-build"], cwd=ROOT, env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Stop", result.stderr)
        self.assertFalse((self.root / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
