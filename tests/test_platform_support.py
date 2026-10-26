"""Host compatibility checks: real process locks, no Docker or model requests."""
import csv
import io
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from agent_workbench import locking, platform_support as host, runtime
from agent_workbench import docker_execution
from agent_workbench import docker_worker
from agent_workbench.resources import controller_lock
from agent_workbench.settings import Settings
from agent_workbench.workspace import WorkbenchError, create_workspace, excluded, git, inventory, safe_path, windows_names, write_json


class PlatformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_shutdown_interrupts_a_slow_read_only_docker_query_and_reaps_it(self):
        import psutil
        pid_file = self.root / "query.pid"
        program = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        canceled = threading.Event()
        def request_stop():
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline: time.sleep(0.02)
            canceled.set()
        watcher = threading.Thread(target=request_stop, daemon=True); watcher.start()
        with patch("agent_workbench.runtime.docker_argv", return_value=[sys.executable, "-c", program, str(pid_file)]), runtime.interruptible_inspection(canceled):
            with self.assertRaises(WorkbenchError): runtime.docker("ps", timeout=15)
        watcher.join(timeout=2)
        self.assertTrue(pid_file.exists())
        # A Windows PID can remain queryable while a terminated process handle
        # is open. Waiting verifies termination rather than handle lifetime.
        try: psutil.Process(int(pid_file.read_text())).wait(timeout=3)
        except psutil.NoSuchProcess: pass

    @unittest.skipUnless(host.IS_WINDOWS, "NTFS reader sharing semantics require Windows")
    def test_atomic_state_update_survives_a_concurrent_windows_reader(self):
        path = self.root / "state.json"
        write_json(path, {"phase": "RUNNING"})
        entered, finished = threading.Event(), threading.Event()
        failures = []
        def publish():
            entered.set()
            try: write_json(path, {"phase": "STOPPING"})
            except Exception as error: failures.append(error)
            finally: finished.set()
        with path.open("rb") as reader:
            writer = threading.Thread(target=publish); writer.start()
            self.assertTrue(entered.wait(3))
            self.assertFalse(finished.wait(0.15))
            self.assertEqual(json.loads(reader.read()), {"phase": "RUNNING"})
        writer.join(timeout=4)
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"phase": "STOPPING"})
        self.assertEqual(list(self.root.glob(".state.json.*")), [])

    def test_inspection_cancellation_context_never_starts_mutating_commands(self):
        with patch("agent_workbench.runtime.subprocess.Popen") as spawn, runtime.interruptible_inspection(threading.Event()):
            for args in (("run", "fixture"), ("rm", "fixture"), ("build", ".")):
                with self.assertRaises(WorkbenchError): runtime.docker(*args)
            spawn.assert_not_called()

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

    def test_guardian_launcher_transfers_bounded_context_through_real_pipes(self):
        marker = self.root / "context.bin"
        lock = self.root / "docker-command.lock"
        real_popen = subprocess.Popen
        script = ("import sys; from agent_workbench.docker_worker import serve; "
            "sys.exit(serve(sys.argv[1], [sys.executable, '-c', "
            "\"from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())\", sys.argv[2]]))")
        def launch(argv, **kwargs):
            self.assertEqual(argv[1:3], ["-m", "agent_workbench.docker_worker"])
            return real_popen([sys.executable, "-c", script, str(lock), str(marker)], **kwargs)
        payload = b"FROM scratch\n" * 5000
        with patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), create=True), patch.object(subprocess, "CREATE_NO_WINDOW", getattr(subprocess, "CREATE_NO_WINDOW", 0), create=True), patch("agent_workbench.docker_worker.subprocess.Popen", side_effect=launch):
            result = docker_worker.run(["build", "-"], lock, io.BytesIO(payload), timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(marker.read_bytes(), payload)
        self.assertEqual(self.child_lock(lock).returncode, 0)

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
            for name in ("C:/outside", "C:outside", r"a\outside", "a/file:stream", ":(glob)*", "AUX.txt", "a/NUL", "a/trailing.", "a/trailing "):
                with self.subTest(name=name), self.assertRaises(WorkbenchError):
                    safe_path(self.root, name)

    def test_windows_case_collisions_and_case_variant_private_paths(self):
        for names in (("A.txt", "a.txt"), ("Source/one.py", "source/two.py")):
            with self.assertRaises(WorkbenchError):
                windows_names(names)
        windows_names(("Source/one.py", "Source/two.py"))
        with patch("agent_workbench.workspace.IS_WINDOWS", True):
            for name in (".GIT/config", ".ENV", "Credentials/token"):
                self.assertTrue(excluded(name))

    def test_windows_snapshot_keeps_git_execute_mode_and_lf_bytes(self):
        source = self.root / "source"; source.mkdir()
        for args in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            git(source, *args)
        git(source, "config", "core.autocrlf", "true")
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

    def test_real_api_and_runner_processes_prepare_workspace_without_docker(self):
        data = self.root / "data"
        settings = Settings.load(data, sqlite=True)
        source = self.root / "source"; source.mkdir()
        (source / "main.txt").write_bytes("中文源码\n".encode())
        for args in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"), ("config", "core.autocrlf", "true"), ("add", "--all"), ("-c", "commit.gpgsign=false", "commit", "-m", "fixture")):
            git(source, *args)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        environment = {**os.environ, "PYTHONUTF8": "1", "AWB_MODEL": "opencode/glm-5.3-flash-free"}
        with (self.root / "server.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-m", "agent_workbench.server", "--sqlite", "--data-dir", str(data), "--port", str(port)], env=environment, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=15) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            if client.get("/api/v1/session").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail("real control API failed to start")
                        time.sleep(0.1)
                    def post(path, value):
                        response = client.post("/api/v1" + path, json=value)
                        response.raise_for_status(); return response.json()
                    project = post("/projects", {"name": "Windows 启动样例", "source_path": str(source)})
                    req = post("/requirements", {"project_id": project["id"], "title": "原生进程准备工作区"})
                    job = post(f"/requirements/{req['id']}/actions", {"action": "prepare"})
                    result = subprocess.run([sys.executable, "-m", "agent_workbench.runner", "--data-dir", str(data), "--api", base, "--once"], env=environment, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                    current = client.get("/api/v1/jobs/" + job["id"]).json()
                    self.assertEqual(current["status"], "SUCCEEDED", current.get("error"))
                    detail = client.get("/api/v1/requirements/" + req["id"]).json()
                    self.assertEqual((Path(detail["workspace"]["path"]) / "repo/main.txt").read_bytes(), "中文源码\n".encode())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()

    @unittest.skipUnless(os.name == "nt", "NTFS junctions only exist on Windows")
    def test_windows_junction_is_rejected_as_a_source_component(self):
        target = self.root / "external"; target.mkdir()
        (target / "private.txt").write_bytes(b"private")
        link = self.root / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        self.assertTrue(host.is_link(link))
        with self.assertRaises(WorkbenchError):
            safe_path(self.root, "junction/private.txt")

    @unittest.skipUnless(os.name == "nt", "8.3 aliases only exist on Windows")
    def test_windows_short_alias_is_canonicalized_without_allowing_junctions(self):
        from win32api import GetShortPathName
        from agent_workbench.profiles import checked_source
        path = self.root / "long configuration name.conf"
        path.write_bytes(b"fixture")
        short = GetShortPathName(str(path))
        self.assertEqual(checked_source(short, self.root / "data"), path.resolve())


if __name__ == "__main__":
    unittest.main()
