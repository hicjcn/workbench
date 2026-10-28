from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench import runtime
from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot, write_json


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "main.txt").write_text("original\n")
        (self.source / ".env").write_text("NOT_A_REAL_SECRET=fixture\n")
        self.commit()

    def commit(self):
        git(self.source, "add", "--all")
        git(self.source, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")

    def create(self, requirement="req-a", **kwargs):
        result = create_workspace(self.source, self.root / "workspaces", requirement, "backend", **kwargs)
        return Path(result["workspace"]) / "repo"

    def test_two_copies_and_source_are_independent(self):
        first, second = self.create(), self.create("req-b")
        (first / "main.txt").write_text("first requirement\n")
        self.assertEqual((second / "main.txt").read_text(), "original\n")
        self.assertEqual((self.source / "main.txt").read_text(), "original\n")
        self.assertTrue((first / ".git").is_dir())
        self.assertEqual(git(first, "remote").strip(), b"")

    def test_committed_credentials_are_not_exported_in_history(self):
        repo = self.create()
        self.assertFalse((repo / ".env").exists())
        history = git(repo, "log", "--all", "--format=", "--name-only").decode()
        self.assertNotIn(".env", history)

    def test_default_uses_selected_commit_not_dirty_source(self):
        (self.source / "main.txt").write_text("dirty\n")
        repo = self.create()
        self.assertEqual((repo / "main.txt").read_text(), "original\n")

    def test_dirty_and_selected_untracked_changes_remain_uncommitted(self):
        (self.source / "main.txt").write_text("dirty\n")
        (self.source / "new.txt").write_text("new\n")
        (self.source / ".env.local").write_text("NOT_A_REAL_SECRET=fixture\n")
        repo = self.create(include_dirty=True, include_untracked=True)
        self.assertEqual((repo / "main.txt").read_text(), "dirty\n")
        self.assertTrue((repo / "new.txt").exists())
        self.assertFalse((repo / ".env.local").exists())
        self.assertIn(b"main.txt", git(repo, "diff", "--name-only"))
        self.assertEqual(git(repo, "show", "HEAD:main.txt"), b"original\n")

    def test_digest_changes_without_a_new_commit(self):
        repo = self.create()
        before = snapshot(repo)
        (repo / "main.txt").write_text("modified\n")
        changed = snapshot(repo)
        self.assertEqual(before["commit_sha"], changed["commit_sha"])
        self.assertNotEqual(before["tree_digest"], changed["tree_digest"])
        (repo / "new.txt").write_text("untracked\n")
        self.assertNotEqual(changed["tree_digest"], snapshot(repo)["tree_digest"])

    def test_sensitive_runtime_files_do_not_change_code_digest(self):
        repo = self.create()
        before = snapshot(repo)
        (repo / ".env").write_text("NOT_A_REAL_SECRET=runtime\n")
        self.assertEqual(before, snapshot(repo))

    def test_duplicate_or_traversing_workspace_is_rejected(self):
        self.create()
        with self.assertRaises(WorkbenchError):
            self.create()
        with self.assertRaises(WorkbenchError):
            self.create("../escape")

    def test_symlink_in_destination_is_rejected(self):
        base = self.root / "workspaces"
        base.mkdir()
        (base / "req-a").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(WorkbenchError):
            self.create()

    def test_external_source_symlink_is_rejected(self):
        (self.source / "outside").symlink_to(self.root / "private.txt")
        self.commit()
        with self.assertRaises(WorkbenchError):
            self.create()

    def test_lfs_pointer_is_not_claimed_as_materialized_source(self):
        for newline in ("\n", "\r\n"):
            (self.source / "large.bin").write_bytes(newline.join(("version https://git-lfs.github.com/spec/v1", "oid sha256:fixture", "size 123", "")).encode())
            self.commit()
            with self.subTest(newline=newline), self.assertRaises(WorkbenchError):
                self.create()

    def test_workspace_under_source_is_rejected(self):
        with self.assertRaises(WorkbenchError):
            create_workspace(self.source, self.source / "copies", "req-a", "backend")

    def test_persisted_agent_config_symlink_is_rejected_before_docker(self):
        repo = self.create()
        home = repo.parent / "agent-home"
        home.mkdir()
        external = self.root / "outside-home"
        external.mkdir()
        (home / ".config").symlink_to(external, target_is_directory=True)
        with patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaises(WorkbenchError):
                runtime.start(repo.parent, "fixture-image")
            docker.assert_not_called()
        self.assertEqual(list(external.iterdir()), [])

    def test_openrouter_key_is_forwarded_by_name_without_persisting_it(self):
        repo = self.create()
        workspace = repo.parent
        model = "openrouter/fixture/model:free"
        credential = "fixture-openrouter-not-a-real-key"
        config = workspace / "agent-home/.config/opencode/opencode.json"
        write_json(config, {"model": "paid/model", "small_model": "paid/model"})

        def docker(*args, **kwargs):
            if args[:2] == ("network", "inspect"):
                return subprocess.CompletedProcess([], 1, "", "No such network")
            output = "127.0.0.1:4096\n" if args[0] == "port" else ""
            return subprocess.CompletedProcess([], 0, output, "")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": credential}), \
                patch.object(runtime, "docker", side_effect=docker) as calls:
            result = runtime.start(workspace, "fixture-image", openrouter_model=model)
        run = next(call for call in calls.call_args_list if call.args[0] == "run")
        self.assertIn("OPENROUTER_API_KEY", run.args)
        self.assertNotIn(credential, " ".join(run.args))
        self.assertEqual(run.kwargs["environment"]["OPENROUTER_API_KEY"], credential)
        configured = json.loads(config.read_text())
        self.assertEqual(configured["model"], model)
        self.assertEqual(configured["small_model"], model)
        self.assertEqual(configured["enabled_providers"], ["openrouter"])
        self.assertEqual(configured["provider"]["openrouter"]["whitelist"], ["fixture/model:free"])
        self.assertNotIn(credential, config.read_text())
        self.assertNotIn(credential, (workspace / "runtime.json").read_text())
        self.assertNotIn(credential, repr(result))

    def test_openrouter_missing_key_or_paid_model_stops_before_docker(self):
        repo = self.create()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), patch.object(runtime, "docker") as docker:
            for model in ("openrouter/fixture/model", "openrouter/fixture/model:free"):
                with self.subTest(model=model), self.assertRaises(WorkbenchError):
                    runtime.start(repo.parent, "fixture-image", openrouter_model=model)
            docker.assert_not_called()

    def test_runtime_forwards_profile_environment_ports_and_mount_permissions(self):
        workspace = self.create().parent
        external = self.root / "external"; external.mkdir()
        mapping = {"container_port": 3000, "host_port": 21000, "protocol": "tcp", "service": "agent"}
        def docker(*args, **kwargs):
            if args[:2] == ("network", "inspect"):
                return subprocess.CompletedProcess([], 1, "", "missing")
            return subprocess.CompletedProcess([], 0, "127.0.0.1:4096\n" if args[0] == "port" else "", "")
        with patch.object(runtime, "docker", side_effect=docker) as calls:
            result = runtime.start(workspace, "fixture-image", profile_environment={"APP_TOKEN": "private-value"},
                volume_mounts=[{"source": str(external), "target": "/mnt/data", "read_only": False}], port_mappings=[mapping])
        run = next(call for call in calls.call_args_list if call.args[0] == "run")
        self.assertIn("127.0.0.1:21000:3000/tcp", run.args)
        self.assertIn("APP_TOKEN", run.args)
        self.assertNotIn("private-value", " ".join(run.args))
        self.assertEqual(run.kwargs["environment"]["APP_TOKEN"], "private-value")
        volume = next(value for value in run.args if "dst=/mnt/data" in value)
        self.assertNotIn("readonly", volume)
        self.assertEqual(result["port_mappings"], [mapping])
        self.assertNotIn("private-value", (workspace / "runtime.json").read_text())

    def test_readonly_runtime_forces_external_mounts_readonly(self):
        workspace = self.create().parent
        def docker(*args, **kwargs):
            if args[:2] == ("network", "inspect"):
                return subprocess.CompletedProcess([], 1, "", "missing")
            return subprocess.CompletedProcess([], 0, "127.0.0.1:4096\n" if args[0] == "port" else "", "")
        with patch.object(runtime, "docker", side_effect=docker) as calls:
            runtime.start(workspace, "fixture-image", read_only_source=True, opencode_model="opencode/glm-5.3-flash-free",
                volume_mounts=[{"source": str(self.source), "target": "/mnt/data", "read_only": False}])
        run = next(call for call in calls.call_args_list if call.args[0] == "run")
        self.assertIn("readonly", next(value for value in run.args if "dst=/mnt/data" in value))

    def test_root_runtime_initializes_bash_without_relaxing_readonly_mounts(self):
        from agent_workbench.agent_service import launch_argv
        from agent_workbench.platform_support import container_ids
        workspace = self.create().parent
        def docker(*args, **kwargs):
            if args[:2] == ("network", "inspect"):
                return subprocess.CompletedProcess([], 1, "", "missing")
            return subprocess.CompletedProcess([], 0, "127.0.0.1:4096\n" if args[0] == "port" else "", "")
        with patch.object(runtime, "docker", side_effect=docker) as calls:
            runtime.start(workspace, "fixture-image", run_as_root=True, read_only_source=True,
                opencode_model="opencode/glm-5.3-flash-free")
        args = next(call.args for call in calls.call_args_list if call.args[0] == "run")
        self.assertEqual(args[args.index("--user") + 1], f"0:{container_ids()[1]}")
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        self.assertEqual(args[args.index("--cap-add") + 1], "DAC_OVERRIDE")
        self.assertIn("GIT_CONFIG_VALUE_0=", args)
        self.assertIn("GIT_CONFIG_VALUE_1=/workspace/project", args)
        self.assertIn("--read-only", args)
        self.assertIn("readonly", next(value for value in args if "dst=/workspace/project" in value))
        self.assertEqual(args[-1], "fixture-image")
        state = json.loads((workspace / "runtime.json").read_text())
        launched = next(call.args for call in calls.call_args_list if call.args[:2] == ("exec", "--detach"))
        self.assertEqual(list(launched[3:]), launch_argv(
            ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"],
            directory=state["agent_service"]["directory"], root=True))
        self.assertTrue(state["run_as_root"])

    def test_opencode_free_model_overwrites_persisted_config_without_forwarding_credentials(self):
        workspace = self.create().parent
        model = "opencode/glm-5.3-flash-free"
        config = workspace / "agent-home/.config/opencode/opencode.json"
        write_json(config, {"model": "paid/model", "small_model": "paid/model"})

        def docker(*args, **kwargs):
            if args[:2] == ("network", "inspect"):
                return subprocess.CompletedProcess([], 1, "", "No such network")
            return subprocess.CompletedProcess([], 0, "127.0.0.1:4096\n" if args[0] == "port" else "", "")

        with patch.object(runtime, "docker", side_effect=docker) as calls:
            runtime.start(workspace, "fixture-image", opencode_model=model)
        run = next(call for call in calls.call_args_list if call.args[0] == "run")
        self.assertEqual(set(run.kwargs["environment"]), {"OPENCODE_SERVER_PASSWORD"})
        configured = json.loads(config.read_text())
        self.assertEqual(configured["enabled_providers"], ["opencode"])
        self.assertEqual(configured["model"], model)
        self.assertEqual(configured["small_model"], model)
        self.assertEqual(configured["provider"]["opencode"]["whitelist"], ["glm-5.3-flash-free"])

    def test_paid_opencode_or_ambiguous_provider_selection_stops_before_docker(self):
        workspace = self.create().parent
        with patch.object(runtime, "docker") as docker:
            with self.assertRaises(WorkbenchError):
                runtime.start(workspace, "fixture-image", opencode_model="opencode/paid")
            with self.assertRaises(WorkbenchError):
                runtime.start(workspace, "fixture-image", opencode_model="opencode/glm-5.3-flash-free",
                              openrouter_model="openrouter/fixture/model:free")
            docker.assert_not_called()

    def test_state_writer_does_not_follow_a_predictable_temporary_symlink(self):
        victim = self.root / "victim.txt"
        victim.write_text("preserve")
        (self.root / "record.json.tmp").symlink_to(victim)
        destination = self.root / "record.json"
        write_json(destination, {"value": "saved"})
        self.assertEqual(victim.read_text(), "preserve")
        if os.name != "nt":
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

