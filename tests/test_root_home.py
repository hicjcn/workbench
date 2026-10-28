"""Root HOME locates mounted credentials while OpenCode config stays isolated."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench import runtime
from agent_workbench.container_shell import shell_argv
from agent_workbench.profiles import Content
from agent_workbench.workspace import WorkbenchError
from test_control_plane import ControlFixture


class RootHomeRuntimeTests(ControlFixture):
    def start(self, **options):
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        def docker(*args, **_kwargs):
            if args[:2] == ("network", "inspect"):
                return subprocess.CompletedProcess([], 1, "", "No such network")
            if args[0] == "port":
                return subprocess.CompletedProcess([], 0, "127.0.0.1:4096\n", "")
            if args[0] == "inspect":
                return subprocess.CompletedProcess([], 0, '[{"State":{"Running":true}}]', "")
            return subprocess.CompletedProcess([], 0, "", "")
        with patch("agent_workbench.runtime.docker", side_effect=docker) as calls:
            runtime.start(workspace, "fixture:local", start_agent=False,
                opencode_model="opencode/glm-5.3-flash-free", **options)
        run = next(call.args for call in calls.call_args_list if call.args[0] == "run")
        return workspace, run

    def test_root_home_preserves_image_root_and_isolated_opencode_state(self):
        workspace, run = self.start(run_as_root=True)
        self.assertIn("HOME=/root", run)
        self.assertIn("OPENCODE_CONFIG=/home/node/.config/opencode/opencode.json", run)
        mounts = [run[index + 1] for index, argument in enumerate(run) if argument == "--mount"]
        self.assertFalse(any("dst=/root," in mount or mount.endswith("dst=/root") for mount in mounts))
        for path in (".config/opencode", ".local/share/opencode", ".cache/opencode", ".local/state/opencode"):
            self.assertTrue((workspace / "agent-home" / path).is_dir())
            self.assertTrue(any("dst=/root/" + path in mount for mount in mounts))
        fixed = json.loads((workspace / "agent-home/.config/opencode/opencode.json").read_text())
        self.assertEqual(fixed["model"], "opencode/glm-5.3-flash-free")
        self.assertEqual(runtime.describe_debug_runtime(workspace)["home"], "/root")
        self.assertEqual(json.loads((workspace / "runtime.json").read_text())["container_home"], "/root")

    def test_nonroot_retains_original_home_and_mount_layout(self):
        workspace, run = self.start(run_as_root=False)
        self.assertIn("HOME=/home/node", run)
        self.assertNotIn("OPENCODE_CONFIG=/home/node/.config/opencode/opencode.json", run)
        self.assertFalse(any("dst=/root/" in argument for argument in run))
        self.assertEqual(runtime.describe_debug_runtime(workspace)["home"], "/home/node")

    def test_explicit_credential_directory_owns_its_subtree_and_child_files_can_overlay_state(self):
        directory = self.root / "credentials"
        directory.mkdir()
        mounts = [{"source": str(directory), "target": "/root/.config", "read_only": True},
            {"source": str(directory), "target": "/root/.local/share/opencode", "read_only": True},
            {"source": str(directory), "target": "/root/.cache/opencode/auth", "read_only": True}]
        workspace, run = self.start(run_as_root=True, volume_mounts=mounts)
        bindings = [run[index + 1] for index, argument in enumerate(run) if argument == "--mount"]
        self.assertFalse(any("dst=/root/.config/opencode" in value for value in bindings))
        self.assertEqual(sum("dst=/root/.local/share/opencode" in value for value in bindings), 1)
        self.assertTrue(any("dst=/root/.local/share/opencode,readonly" in value for value in bindings))
        self.assertTrue(any("dst=/root/.cache/opencode," in value or value.endswith("dst=/root/.cache/opencode") for value in bindings))
        self.assertTrue(any("dst=/root/.cache/opencode/auth,readonly" in value for value in bindings))
        self.assertTrue((workspace / "agent-home/.config/opencode/opencode.json").is_file())

    def test_readonly_role_retains_readonly_source_and_credential_mounts(self):
        directory = self.root / "credentials"
        directory.mkdir()
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        with patch("agent_workbench.runtime.docker") as docker:
            docker.side_effect = lambda *args, **_: subprocess.CompletedProcess([], 1 if args[:2] == ("network", "inspect") else 0,
                "127.0.0.1:4096\n" if args[0] == "port" else "", "")
            runtime.start(workspace, "fixture:local", run_as_root=True, read_only_source=True,
                opencode_model="opencode/glm-5.3-flash-free",
                volume_mounts=[{"source": str(directory), "target": "/root/.team-login", "read_only": False}])
        run = next(call.args for call in docker.call_args_list if call.args[0] == "run")
        self.assertTrue(any("dst=/workspace/project,readonly" in argument for argument in run))
        self.assertTrue(any("dst=/root/.team-login,readonly" in argument for argument in run))
        configuration = json.loads((workspace / "agent-home/.config/opencode/opencode.json").read_text())
        self.assertEqual(configuration["permission"]["*"], "deny")

    def test_root_state_symlink_is_rejected_before_any_docker_operation(self):
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        outside = self.root / "outside"
        outside.mkdir()
        (workspace / "agent-home").mkdir(exist_ok=True)
        (workspace / "agent-home/.local").symlink_to(outside, target_is_directory=True)
        with patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaisesRegex(WorkbenchError, "Symlinks"):
                runtime.start(workspace, "fixture:local", run_as_root=True, start_agent=False)
        docker.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])


class RootHomeConfigurationTests(unittest.TestCase):
    def test_profile_cannot_redirect_managed_opencode_configuration(self):
        with self.assertRaises(ValueError):
            Content(environment={"OPENCODE_CONFIG": "/root/unrestricted.json"})

    @unittest.skipUnless(os.name != "nt" and Path("/bin/bash").exists(), "Requires Linux Bash")
    def test_home_relative_credential_lookup_and_pinned_config_survive_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = root / ".team-login"
            credentials.mkdir()
            (credentials / "auth.json").write_text('{"fixture":true}')
            startup = root / "root.bashrc"
            startup.write_text('export OPENCODE_CONFIG=/root/wrong-config.json\n')
            program = 'import json,os,pathlib; print(json.dumps({"readable":(pathlib.Path(os.environ["HOME"])/".team-login/auth.json").is_file(),"config":os.environ["OPENCODE_CONFIG"]}))'
            environment = {**os.environ, "HOME": str(root), "OPENCODE_CONFIG": "/home/node/.config/opencode/opencode.json"}
            with patch("agent_workbench.container_shell.ROOT_BASHRC", str(startup)):
                result = subprocess.run(shell_argv([sys.executable, "-c", program], root=True),
                    env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"readable": True, "config": "/home/node/.config/opencode/opencode.json"})
