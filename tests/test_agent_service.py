"""Exercise the actual Bash supervisor without Docker or model calls."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from agent_workbench import agent_service
from agent_workbench.workspace import WorkbenchError


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='awb-service-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = '/tmp/awb-agent-' + secrets.token_hex(8)
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.environment = {**os.environ, 'HOME': str(self.root)}

    def status(self, *, logs=False):
        result = subprocess.run(agent_service.status_argv(self.directory, logs=logs),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return agent_service.parse_status(result.stdout, logs=logs)

    @unittest.skipUnless(os.name != "nt" and Path("/bin/bash").exists(), "Requires Linux Bash")
    def test_supervisor_preserves_literal_arguments_and_records_exit_and_logs(self):
        arguments = ['space value', '$(touch injected)', '`touch injected2`', '$HOME', 'a;b']
        code = "import json,sys; print(json.dumps(sys.argv[1:])); print('configured CLI failed', file=sys.stderr); sys.exit(7)"
        result = subprocess.run(agent_service.launch_argv([sys.executable, '-c', code, *arguments], directory=self.directory),
                                cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        observed = self.status(logs=True)
        self.assertEqual(observed['agent_status'], 'exited')
        self.assertEqual(observed['agent_exit_code'], 7)
        self.assertIn(json.dumps(arguments), observed['log_tail'])
        self.assertIn('configured CLI failed', observed['log_tail'])
        self.assertFalse((self.root/'injected').exists())
        self.assertFalse((self.root/'injected2').exists())

    @unittest.skipUnless(os.name != "nt" and Path("/bin/bash").exists(), "Requires Linux Bash")
    def test_missing_cli_records_exit_127_instead_of_appearing_running(self):
        result = subprocess.run(agent_service.launch_argv(['/definitely-missing-awb-cli'], directory=self.directory),
                                cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 127, result.stderr)
        observed = self.status(logs=True)
        self.assertEqual(observed['agent_status'], 'exited')
        self.assertEqual(observed['agent_exit_code'], 127)
        self.assertIn('/definitely-missing-awb-cli', observed['log_tail'])

    @unittest.skipUnless(os.name != "nt" and Path("/bin/bash").exists(), "Requires Linux Bash")
    def test_status_tracks_child_process_until_it_exits(self):
        released = self.root/'release'
        code = "import pathlib,sys,time; ready=pathlib.Path(sys.argv[1]);\nwhile not ready.exists(): time.sleep(.01)\n"
        process = subprocess.Popen(agent_service.launch_argv([sys.executable, '-c', code, str(released)], directory=self.directory),
                                   cwd=self.root, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while True:
                observed = self.status()
                if observed['agent_status'] == 'running':
                    break
                self.assertEqual(observed['agent_status'], 'starting')
                if time.monotonic() >= deadline:
                    self.fail('configured CLI child did not start')
                time.sleep(.01)
            self.assertIsNone(process.poll())
        finally:
            released.touch()
            _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(self.status(), {'agent_status': 'exited', 'agent_exit_code': 0})

    def test_status_parser_rejects_malformed_exit_records_and_bounds_logs(self):
        for output in ('', 'healthy:\n', 'exited:\n', 'exited:256\n', 'exited:private\n', 'running:; touch injected\n'):
            with self.subTest(output=output), self.assertRaises(WorkbenchError):
                agent_service.parse_status(output, logs=True)
        observed = agent_service.parse_status('exited:7\n' + 'x'*9000, logs=True)
        self.assertEqual(len(observed['log_tail']), 8000)
        self.assertEqual(observed['agent_exit_code'], 7)

    def test_service_directory_cannot_escape_its_private_temporary_path(self):
        for directory in ('/tmp', '/tmp/awb-agent-../../outside', '/tmp/awb-agent-0123456789abcdef/child', '/workspace/repo'):
            with self.subTest(directory=directory), self.assertRaises(WorkbenchError):
                agent_service.launch_argv(['fixture-cli'], directory=directory)
