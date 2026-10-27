"""Committed-link fixtures use Git modes, so Windows needs no symlink privileges."""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.source_export import export
from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot


class SourceLinkExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        git(self.source, 'init', '-b', 'main')
        git(self.source, 'config', 'user.name', 'Fixture')
        git(self.source, 'config', 'user.email', 'fixture@example.invalid')
        (self.source / 'main.txt').write_text('original\n')
        (self.source / '.env').write_text('PRIVATE_FIXTURE_MUST_NOT_COPY=true\n')

    def file(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def commit(self, links=None, executable=()):
        for name, target in (links or {}).items():
            self.file(name, target)
        git(self.source, 'add', '--all')
        for name in executable:
            git(self.source, 'update-index', '--chmod=+x', '--', name)
        for name in links or {}:
            oid = git(self.source, 'hash-object', '-w', name).decode().strip()
            git(self.source, 'update-index', '--cacheinfo', f'120000,{oid},{name}')
        git(self.source, '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'fixture')

    def create(self, **options):
        result = create_workspace(self.source, self.root / 'workspaces', 'req-a', 'backend', **options)
        return Path(result['workspace']) / 'repo'

    def assert_failed_cleanly(self, expression):
        head = git(self.source, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(WorkbenchError, expression):
            self.create()
        self.assertFalse((self.root / 'workspaces/req-a/backend').exists())
        self.assertEqual(list((self.root / 'workspaces/req-a').iterdir()), [])
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), head)

    def test_harness_file_link_uses_committed_bytes_and_keeps_source_and_other_requirements_independent(self):
        self.file('AGENTS.md', 'committed instructions\n')
        self.commit({'.harness': 'AGENTS.md'})
        self.file('AGENTS.md', 'dirty local instructions\n')
        repo = self.create()
        self.assertEqual((repo / '.harness').read_text(), 'committed instructions\n')
        self.assertFalse((repo / '.harness').is_symlink())
        self.assertIn(b'120000 blob', git(self.source, 'ls-tree', 'HEAD', '--', '.harness'))
        self.assertIn(b'100644', git(repo, 'ls-files', '--stage', '--', '.harness'))
        manifest = json.loads((repo.parent / 'workspace.json').read_text())
        self.assertEqual(manifest['materialized_links'], [{'path': '.harness', 'target': 'AGENTS.md'}])
        other = create_workspace(self.source, self.root / 'workspaces', 'req-b', 'backend')
        (repo / '.harness').write_text('private requirement edit\n')
        self.assertEqual((Path(other['workspace']) / 'repo/.harness').read_text(), 'committed instructions\n')
        self.assertEqual((self.source / 'AGENTS.md').read_text(), 'dirty local instructions\n')

    def test_directory_link_preserves_binary_unicode_and_executable_bits(self):
        self.file('tools/agent config/使用.txt', b'\x00\xff\x01')
        self.file('tools/run.sh', '#!/bin/sh\nexit 0\n')
        self.commit({'.harness': 'tools'}, executable=['tools/run.sh'])
        repo = self.create()
        self.assertEqual((repo / '.harness/agent config/使用.txt').read_bytes(), b'\x00\xff\x01')
        self.assertIn(b'100755', git(repo, 'ls-files', '--stage', '--', '.harness/run.sh'))
        self.assertFalse(any(path.is_symlink() for path in (repo / '.harness').rglob('*')))
        self.assertEqual(snapshot(repo)['tree_digest'], json.loads((repo.parent / 'workspace.json').read_text())['source_baseline']['tree_digest'])

    def test_chained_and_nested_directory_links_are_expanded(self):
        self.file('tools/config.txt', 'instructions')
        self.file('shared/helper.txt', 'helper')
        self.commit({'.harness': 'links/current', 'links/current': '../tools', 'tools/helper': '../shared'})
        repo = self.create()
        self.assertEqual((repo / '.harness/config.txt').read_text(), 'instructions')
        self.assertEqual((repo / '.harness/helper/helper.txt').read_text(), 'helper')

    def test_parent_components_follow_intermediate_links_before_dot_dot(self):
        self.file('a/deep/keep.txt', 'keep')
        self.file('a/config.txt', 'correct')
        self.file('config.txt', 'wrong lexical target')
        self.commit({'prefix': 'a/deep', '.harness': 'prefix/../config.txt'})
        self.assertEqual((self.create() / '.harness').read_text(), 'correct')

    def test_regular_harness_file_is_not_excluded(self):
        self.file('.harness', 'ordinary project file')
        self.commit()
        self.assertEqual((self.create() / '.harness').read_text(), 'ordinary project file')

    def test_directory_alias_does_not_bypass_runtime_and_credential_exclusions(self):
        self.file('tools/keep.txt', 'keep')
        self.file('tools/.env.local', 'PRIVATE_FIXTURE')
        self.file('tools/node_modules/pkg.js', 'ignored')
        self.commit({'.harness': 'tools', 'tools/runtime-secrets/ignored': '../../outside'})
        repo = self.create()
        self.assertTrue((repo / '.harness/keep.txt').exists())
        self.assertFalse((repo / '.harness/.env.local').exists())
        self.assertFalse((repo / '.harness/node_modules').exists())
        self.assertFalse((repo / '.harness/runtime-secrets').exists())
        self.assertFalse((repo / '.env').exists())

    def test_external_absolute_link_is_rejected_without_copying_host_content(self):
        private = self.root / 'private.txt'; private.write_text('PRIVATE_FIXTURE')
        self.commit({'.harness': str(private)})
        self.assert_failed_cleanly('仓库内相对路径')
        self.assertEqual(private.read_text(), 'PRIVATE_FIXTURE')

    def test_relative_escape_and_windows_absolute_targets_are_rejected(self):
        for target in ('../private.txt', 'C:/Users/example/private', r'\\server\share\private'):
            with self.subTest(target=target):
                self.commit({'.harness': target})
                self.assert_failed_cleanly('仓库外|仓库内相对路径')

    def test_missing_or_untracked_target_is_not_read_from_checkout(self):
        self.commit({'.harness': 'not-committed.txt'})
        self.file('not-committed.txt', 'host-only data')
        self.assert_failed_cleanly('未包含在已提交')

    def test_direct_link_to_excluded_credentials_is_rejected(self):
        self.commit({'.harness': '.env'})
        self.assert_failed_cleanly('已排除')

    def test_export_ignored_target_cannot_be_reintroduced_via_alias(self):
        self.file('tools/hidden.txt', 'ignored instructions')
        self.file('.gitattributes', 'tools/hidden.txt export-ignore\n')
        self.commit({'.harness': 'tools/hidden.txt'})
        self.assert_failed_cleanly('未包含在已提交')

    def test_local_export_ignore_can_omit_an_unneeded_external_tool_link_without_changing_source_head(self):
        self.commit({'.harness': '../external-tool'})
        head = git(self.source, 'rev-parse', 'HEAD')
        (self.source / '.git/info/attributes').write_text('.harness export-ignore\n')
        self.assertFalse((self.create() / '.harness').exists())
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), head)

    def test_file_link_cycle_and_directory_ancestor_cycle_are_rejected(self):
        self.commit({'.harness': 'other', 'other': '.harness'})
        self.assert_failed_cleanly('循环')
        (self.source / 'other').unlink()
        self.commit({'.harness': '.'})
        self.assert_failed_cleanly('循环')

    def test_long_link_chain_is_bounded(self):
        links = {f'link-{index}': f'link-{index + 1}' for index in range(42)}
        links['link-42'] = 'main.txt'
        self.commit(links)
        self.assert_failed_cleanly('超过 40 层')

    def test_link_through_a_regular_file_is_rejected(self):
        self.commit({'.harness': 'main.txt/../.env'})
        self.assert_failed_cleanly('非目录')

    def test_failed_copy_can_be_retried_after_committing_a_correct_link(self):
        self.commit({'.harness': 'missing'})
        self.assert_failed_cleanly('未包含在已提交')
        self.commit({'.harness': 'main.txt'})
        self.assertEqual((self.create() / '.harness').read_text(), 'original\n')

    def test_expanded_bytes_and_file_counts_are_bounded(self):
        self.file('tools/payload', b'x' * 100)
        self.commit({'.harness': 'tools'})
        with patch('agent_workbench.source_export.LIMIT', 150):
            self.assert_failed_cleanly('展开链接后的源码超过')
        for index in range(4):
            self.file(f'small/f{index}', b'')
        self.commit({'.harness': 'small'})
        # The archive itself fits; expansion creates additional ordinary files.
        with patch('agent_workbench.source_export.MAX_ENTRIES', 9):
            with patch('agent_workbench.source_export.LIMIT', 1000):
                self.assert_failed_cleanly('展开链接后的源码超过')

    def test_dirty_overlay_is_explicitly_rejected_instead_of_copying_a_windows_link_stub(self):
        self.commit({'.harness': 'main.txt'})
        with self.assertRaisesRegex(WorkbenchError, '暂不支持脏文件叠加'):
            self.create(include_dirty=True)


class ArchiveValidationTests(unittest.TestCase):
    def test_special_entries_duplicate_paths_and_traversal_are_rejected(self):
        for scenario in ('hardlink', 'duplicate', 'escape'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                content = io.BytesIO()
                with tarfile.open(fileobj=content, mode='w') as archive:
                    entry = tarfile.TarInfo('../escape' if scenario == 'escape' else '.harness')
                    if scenario == 'hardlink':
                        entry.type = tarfile.LNKTYPE; entry.linkname = 'outside'
                    archive.addfile(entry, io.BytesIO(b''))
                    if scenario == 'duplicate':
                        archive.addfile(entry, io.BytesIO(b''))
                with self.assertRaises(WorkbenchError):
                    export(content.getvalue(), Path(directory))
