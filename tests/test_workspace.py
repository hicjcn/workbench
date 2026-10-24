from pathlib import Path
import tempfile
import unittest

from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
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
        (self.source / "large.bin").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:fixture\nsize 123\n")
        self.commit()
        with self.assertRaises(WorkbenchError):
            self.create()

    def test_workspace_under_source_is_rejected(self):
        with self.assertRaises(WorkbenchError):
            create_workspace(self.source, self.source / "copies", "req-a", "backend")


if __name__ == "__main__":
    unittest.main()

