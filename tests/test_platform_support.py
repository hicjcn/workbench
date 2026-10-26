"""Host compatibility checks: real process locks, no Docker or model requests."""
import csv
import io
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench import locking, platform_support as host, runtime
from agent_workbench import docker_execution
from agent_workbench.resources import controller_lock
from agent_workbench.workspace import WorkbenchError, create_workspace, git, inventory, safe_path, write_json


class PlatformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def child_lock(self, path, shared=False):
        code = ("import sys; from agent_workbench import locking; "
            "f=open(sys.argv[1], 'a+b'); "
            "locking.flock(f, (locking.LOCK_SH if sys.argv[2]=='shared' else locking.LOCK_EX) | locking.LOCK_NB)")
        return subprocess.run([sys.executable, "-c", code, str(path), "shared" if shared else "exclusive"],
            capture_output=True, timeout=15)

    def test_real_shared_controller_locks_exclude_maintenance_across_processes(self):
        path = self.root / "locks/controller.lock"
        with controller_lock(self.root):
            self.assertEqual(self.child_lock(path, shared=True).returncode, 0)
            self.assertNotEqual(self.child_lock(path).returncode, 0)
            with self.assertRaises(WorkbenchError):
                with controller_lock(self.root, exclusive=True):
                    self.fail("maintenance must not enter")
        with controller_lock(self.root, exclusive=True):
            self.assertNotEqual(self.child_lock(path, shared=True).returncode, 0)
        self.assertEqual(self.child_lock(path).returncode, 0)

    def test_exclusive_lock_release_and_explicit_unlock(self):
        path = self.root / "writer.lock"
        with path.open("a+b") as handle:
            locking.flock(handle, locking.LOCK_EX | locking.LOCK_NB)
            self.assertNotEqual(self.child_lock(path).returncode, 0)
            locking.flock(handle, locking.LOCK_UN)
            self.assertEqual(self.child_lock(path).returncode, 0)

    def guardian(self, marker):
        lock = self.root / "docker-command.lock"
        script = ("import sys; from agent_workbench.docker_worker import serve; "
            "sys.exit(serve(sys.argv[1], [sys.executable, '-c', "
            "\"from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text('started'); time.sleep(2)\", sys.argv[2]]))")
        process = subprocess.Popen([sys.executable, "-c", script, str(lock), str(marker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        self.assertEqual(process.stdout.readline(), b"READY\n")
        return process, lock

    def test_guardian_eof_before_acknowledgement_never_executes(self):
        marker = self.root / "executed"
        process, lock = self.guardian(marker)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=10), 2)
        self.assertFalse(marker.exists())
        self.assertEqual(self.child_lock(lock).returncode, 0)
        process.stdout.close(); process.stderr.close()

    def test_guardian_owns_lock_after_runner_stream_is_closed(self):
        marker = self.root / "executed"
        process, lock = self.guardian(marker)
        process.stdin.write(b"GO\n"); process.stdin.close()
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        self.assertNotEqual(self.child_lock(lock).returncode, 0)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(self.child_lock(lock).returncode, 0)
        process.stdout.close(); process.stderr.close()

    def test_docker_endpoint_is_local_and_environment_cannot_redirect_it(self):
        for windows, endpoint in ((True, "npipe:////./pipe/dockerDesktopLinuxEngine"), (False, "unix:///var/run/docker.sock")):
            with patch.object(host, "IS_WINDOWS", windows), patch.dict(os.environ,
                {"DOCKER_HOST": "tcp://untrusted.invalid:2375", "DOCKER_CONTEXT": "untrusted", "DOCKER_TLS_VERIFY": "1"}):
                self.assertEqual(host.docker_argv("ps"), ["docker", "--host=" + endpoint, "ps"])
                self.assertNotIn("DOCKER_HOST", host.docker_environment())
                with patch("agent_workbench.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "中文", "")) as run:
                    self.assertEqual(runtime.docker("ps").stdout, "中文")
                    self.assertEqual(run.call_args.args[0], host.docker_argv("ps"))
                    self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")

    def test_windows_non_root_container_identity_does_not_need_posix_uid(self):
        with patch.object(host, "IS_WINDOWS", True):
            self.assertEqual(host.container_ids(), (1000, 1000))
            self.assertEqual(host.container_user(), "1000:1000")

    def test_bind_mount_preserves_spaces_unicode_and_csv_boundaries(self):
        with patch.object(host, "IS_WINDOWS", True):
            value = host.bind_mount(PureWindowsPath(r"C:\Work, shared\工程\repo"), "/workspace/project", readonly=True)
        self.assertEqual(next(csv.reader(io.StringIO(value))),
            ["type=bind", "src=C:/Work, shared/工程/repo", "dst=/workspace/project", "readonly"])
        with self.assertRaises(ValueError):
            host.bind_mount("/tmp/a\nreadonly", "/workspace/project")

    def test_docker_desktop_mount_equivalence_rejects_other_drives_and_traversal(self):
        expected = PureWindowsPath(r"C:\Workbench\工程\repo")
        with patch.object(host, "IS_WINDOWS", True):
            for actual in (r"C:\Workbench\工程\repo", "C:/workbench/工程/repo", "/run/desktop/mnt/host/c/Workbench/工程/repo", "/host_mnt/c/Workbench/工程/repo"):
                self.assertTrue(host.mount_matches(actual, expected), actual)
            for actual in ("/run/desktop/mnt/host/d/Workbench/工程/repo", "/tmp/Workbench/工程/repo", "/run/desktop/mnt/host/c/x/../Workbench/工程/repo", r"\\server\Workbench\工程\repo", None):
                self.assertFalse(host.mount_matches(actual, expected), actual)
        with patch.object(host, "IS_WINDOWS", False):
            self.assertFalse(host.mount_matches("/host_mnt/c/Workbench/工程/repo", "/Workbench/工程/repo"))

    def test_windows_source_names_are_rejected_before_joining_paths(self):
        with patch("agent_workbench.workspace.IS_WINDOWS", True):
            for name in ("C:/outside", "C:outside", r"a\outside", "a/file:stream", "AUX.txt", "a/NUL", "a/trailing.", "a/trailing "):
                with self.subTest(name=name), self.assertRaises(WorkbenchError):
                    safe_path(self.root, name)

    def test_windows_snapshot_keeps_git_execute_mode_and_lf_bytes(self):
        source = self.root / "source"; source.mkdir()
        for args in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            git(source, *args)
        (source / "中文 script.sh").write_bytes(b"#!/bin/sh\nprintf 'fixture\\n'\n")
        git(source, "add", "--all"); git(source, "update-index", "--chmod=+x", "--", "中文 script.sh")
        git(source, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        with patch("agent_workbench.workspace.IS_WINDOWS", True):
            created = create_workspace(source, self.root / "copies", "req-a", "backend")
            repo = Path(created["workspace"]) / "repo"
            self.assertTrue(inventory(repo)["files"][0]["executable"])
        self.assertEqual((repo / "中文 script.sh").read_bytes(), (source / "中文 script.sh").read_bytes())
        self.assertEqual(git(repo, "config", "core.autocrlf").strip(), b"false")

    def test_utf8_private_record_round_trip(self):
        path = self.root / "记录.json"
        write_json(path, {"name": "团队 CLI 配置", "path": "C:\\工作台\\项目"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["name"], "团队 CLI 配置")

    def test_windows_image_commands_use_guardian_instead_of_pass_fds(self):
        with (self.root / "docker-images.lock").open("a+b") as lock:
            with patch.object(docker_execution, "IS_WINDOWS", True), patch("agent_workbench.docker_worker.run", return_value=subprocess.CompletedProcess([], 0)) as worker, patch("agent_workbench.docker_execution.subprocess.run") as direct:
                docker_execution.command(["pull", "node:22"], lock)
                direct.assert_not_called()
                worker.assert_called_once_with(["pull", "node:22"], self.root / "docker-command.lock", None)

    @unittest.skipUnless(os.name == "nt", "NTFS junctions only exist on Windows")
    def test_windows_junction_is_rejected_as_a_source_component(self):
        target = self.root / "external"; target.mkdir()
        (target / "private.txt").write_bytes(b"private")
        link = self.root / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        self.assertTrue(host.is_link(link))
        with self.assertRaises(WorkbenchError):
            safe_path(self.root, "junction/private.txt")


if __name__ == "__main__":
    unittest.main()
