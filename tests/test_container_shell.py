"""Bash initialization keeps the requested process and its data streams intact."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench.container_shell import shell_argv


class ShellArgumentsTests(unittest.TestCase):
    def test_command_is_kept_as_separate_arguments(self):
        argv = ["program", "space arg", "$(printf injected)", "; exit 99", "quote'\"arg", ""]
        self.assertEqual(shell_argv(argv, root=True)[-len(argv):], argv)
        self.assertEqual(shell_argv(argv, root=True)[0], "/bin/bash")

    def test_empty_or_invalid_command_is_rejected(self):
        for argv in ([], [""], "program --flag", ["program", "bad\x00arg"], ["program", 1]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                shell_argv(argv)


@unittest.skipUnless(os.name != "nt" and Path("/bin/bash").exists(), "Requires Linux Bash")
class ContainerShellTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="awb-shell-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.rc = self.root / "root.bashrc"

    def run_command(self, code, *, root=True, args=(), stdin="", home=True, environment=None):
        environment = {**os.environ, **(environment or {})}
        if home: environment["HOME"] = str(self.home)
        else: environment.pop("HOME", None)
        with patch("agent_workbench.container_shell.ROOT_BASHRC", str(self.rc)):
            return subprocess.run(shell_argv([sys.executable, "-c", code, *args], root=root),
                cwd=self.root, env=environment, input=stdin, text=True, capture_output=True, timeout=10)

    def test_interactive_guard_environment_stdin_cwd_home_and_literal_arguments(self):
        self.rc.write_text('case $- in *i*) ;; *) return ;; esac\n'
            'export AWB_SHELL_FIXTURE=loaded\nexport NVM_DIR="$HOME/.nvm"\n'
            'printf "startup output\\n"\nprintf "startup stderr\\n" >&2\n'
            'read -r discarded || true\nset -- changed positional parameters\ncd /\nset -eu\n')
        args = ["space arg", "quote'\"arg", "$(touch injected)", "; exit 99", "\\path", ""]
        result = self.run_command('import json,os,sys; print(json.dumps({"argv":sys.argv[1:],'
            '"stdin":sys.stdin.read(),"cwd":os.getcwd(),"home":os.getenv("HOME"),'
            '"nvm":os.getenv("NVM_DIR"),"marker":os.getenv("AWB_SHELL_FIXTURE")}));'
            'print("business stderr",file=sys.stderr); sys.exit(7)', args=args, stdin="task input\n")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stderr, "business stderr\n")
        value = json.loads(result.stdout)
        self.assertEqual(value, {"argv": args, "stdin": "task input\n", "cwd": str(self.root),
            "home": str(self.home), "nvm": "/root/.nvm", "marker": "loaded"})
        self.assertFalse((self.root / "injected").exists())

    def test_root_initialization_adds_an_executable_to_path(self):
        binary = self.root / "nvm-bin"
        binary.mkdir()
        program = binary / "fixture-cli"
        program.write_text('#!/bin/sh\nprintf "fixture-cli-ready\\n"\n')
        program.chmod(0o755)
        self.rc.write_text('case $- in *i*) ;; *) return ;; esac\n'
            f'export PATH="{binary}:$PATH"\n')
        with patch("agent_workbench.container_shell.ROOT_BASHRC", str(self.rc)):
            result = subprocess.run(shell_argv(["fixture-cli"], root=True), cwd=self.root,
                env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fixture-cli-ready\n", ""))

    def test_missing_or_failed_root_initialization_fails_before_command(self):
        missing = self.run_command('print("must not run")')
        self.assertEqual(missing.returncode, 126)
        self.assertEqual(missing.stdout, "")
        self.assertIn("missing or not readable", missing.stderr)
        self.rc.write_text('printf "startup stdout\\n"; printf "startup error\\n" >&2; return 23\n')
        failed = self.run_command('print("must not run")')
        self.assertEqual(failed.returncode, 23)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(failed.stderr, f"Cannot load container startup file {self.rc} (exit 23).\n")

    def test_stderr_version_output_is_not_mixed_with_initialization_output(self):
        self.rc.write_text('echo noisy-init; echo noisy-init >&2\n')
        result = self.run_command('import sys; print("fixture/1.0",file=sys.stderr)')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", "fixture/1.0\n"))

    def test_injected_control_database_and_ca_values_survive_bashrc(self):
        values = {name: "injected-" + name.lower() for name in (
            "OPENCODE_SERVER_USERNAME", "OPENCODE_SERVER_PASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD",
            "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "GIT_SSL_CAINFO", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")}
        self.rc.write_text('case $- in *i*) ;; *) return ;; esac\n'
            + ''.join(f'export {name}=stale-value\n' for name in values)
            + 'export NVM_DIR="$HOME/.nvm"\nexport PATH="/fixture/nvm-bin:$PATH"\n')
        code = 'import json,os;names=' + repr(list(values)) + ';print(json.dumps({"values":{name:os.getenv(name) for name in names},"nvm":os.getenv("NVM_DIR"),"path":os.getenv("PATH")}))'
        result = self.run_command(code, environment=values)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["values"], values)
        self.assertEqual(output["nvm"], "/root/.nvm")
        self.assertTrue(output["path"].startswith("/fixture/nvm-bin:"))

    def test_injected_empty_value_and_new_bashrc_variable_are_preserved(self):
        self.rc.write_text('export OPENCODE_SERVER_PASSWORD=stale-value\nexport AWB_SHELL_FIXTURE=from-bashrc\n')
        result = self.run_command('import json,os;print(json.dumps([os.getenv("OPENCODE_SERVER_PASSWORD"),os.getenv("AWB_SHELL_FIXTURE")]))',
            environment={"OPENCODE_SERVER_PASSWORD": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["", "from-bashrc"])

    def test_readonly_stale_control_value_fails_clearly(self):
        self.rc.write_text('readonly OPENCODE_SERVER_PASSWORD=stale-value\n')
        result = self.run_command('print("must not run")', environment={"OPENCODE_SERVER_PASSWORD": "injected-value"})
        self.assertEqual(result.returncode, 126)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Cannot restore injected container variable OPENCODE_SERVER_PASSWORD.\n")

    def test_root_umask_keeps_new_files_group_writable(self):
        self.rc.write_text('umask 0077\n')
        result = self.run_command('from pathlib import Path; Path("created").write_text("fixture")')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "created").stat().st_mode & 0o777, 0o664)

    def test_nonroot_uses_its_own_optional_bashrc(self):
        self.rc.write_text('export AWB_SHELL_FIXTURE=wrong-root-file\n')
        (self.home / ".bashrc").write_text('case $- in *i*) ;; *) return ;; esac\n'
            'export AWB_SHELL_FIXTURE=user-file\n')
        result = self.run_command('import os; print(os.getenv("AWB_SHELL_FIXTURE"))', root=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "user-file\n", ""))
        (self.home / ".bashrc").unlink()
        result = self.run_command('print("no rc required")', root=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "no rc required\n", ""))

    def test_unset_home_is_restored_after_root_initialization(self):
        self.rc.write_text('export AWB_SHELL_FIXTURE="$HOME"\n')
        result = self.run_command('import os,json;print(json.dumps([os.getenv("HOME"),os.getenv("AWB_SHELL_FIXTURE")]))', home=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [None, "/root"])

    def test_readonly_home_cannot_redirect_the_command_away_from_workspace_home(self):
        self.rc.write_text('readonly HOME=/root\n')
        for home in (True, False):
            with self.subTest(original_home_set=home):
                result = self.run_command('print("must not run")', home=home)
                self.assertEqual(result.returncode, 126)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "Cannot restore container HOME after startup initialization.\n")

    def test_requested_process_receives_sigterm_directly(self):
        self.rc.write_text(':\n')
        marker = self.root / "ready"
        with patch("agent_workbench.container_shell.ROOT_BASHRC", str(self.rc)):
            process = subprocess.Popen(shell_argv([sys.executable, "-c",
                'from pathlib import Path;import time;Path("ready").write_text("ready");time.sleep(30)'], root=True),
                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            process.terminate()
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGTERM)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
