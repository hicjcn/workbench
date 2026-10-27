import json
import errno
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text

from agent_workbench.checked_archive import build as build_archive, verify
from agent_workbench.delivery_export import build as build_delivery, full_diff
from agent_workbench.execution import Executor
from agent_workbench.maintenance import backup, metadata_digest, restore
from agent_workbench.multi_repo import checked_shared_base, diff, discover, layout, relative_names
from agent_workbench.profiles import Content
from agent_workbench.store import Store, jobs, projects, requirements
from agent_workbench.workflow_execution import verify_command
from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot, write_json
from test_control_plane import ControlFixture


class MultiRepositoryTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.parent = self.root / 'business 中文 space'; self.parent.mkdir()
        self.names = ['order service', 'services/payment 中文']
        self.source.rename(self.parent / self.names[0]); self.source = self.parent / self.names[0]
        payment = self.parent / self.names[1]
        payment.parent.mkdir(); shutil.copytree(self.source, payment)
        (payment / 'main.txt').write_text('payment\n')
        git(payment, 'add', '--all'); git(payment, '-c', 'commit.gpgsign=false', 'commit', '-m', 'payment source')
        (self.parent / 'AGENTS.md').write_text('Shared instructions\n')
        (self.parent / 'README.md').write_text('Optional common documentation\n')
        (self.parent / '.env').write_text('DO_NOT_EXPORT=fixture\n')

    def register(self, **updates):
        value = dict(name='业务系统', source_path=str(self.parent), repositories=self.names, shared_files=['AGENTS.md'])
        value.update(updates)
        response = self.client.post('/api/v1/projects', json=value)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def prepared_multi(self, project=None, selected=None):
        project = project or self.register()
        response = self.client.post('/api/v1/requirements', json=dict(project_id=project['id'], title='跨仓库业务需求',
            **({'repositories': selected} if selected is not None else {})))
        self.assertEqual(response.status_code, 201, response.text)
        req = response.json(); self.action(req, 'prepare')
        with patch('agent_workbench.runtime.docker', return_value=SimpleNamespace(stdout=json.dumps([{'Id': 'sha256:fixed'}]))):
            self.assertTrue(self.runner.run_once())
        detail = self.detail(req)
        self.assertEqual(detail['jobs'][0]['status'], 'SUCCEEDED', detail['jobs'][0].get('error'))
        return detail, Path(detail['workspace']['path'])

    def save_profile(self, project, content):
        response = self.client.post(f"/api/v1/projects/{project['id']}/profiles", json=dict(content=content, actor='Fixture', reason='multi-repository validation'))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_scan_parent_discovers_independent_nested_repositories_and_common_docs(self):
        value = discover(str(self.parent), self.data)
        self.assertEqual([item['path'] for item in value['repositories']], self.names)
        self.assertEqual(value['shared_files'], ['AGENTS.md', 'README.md'])
        self.assertFalse(value['limited'])
        self.assertEqual(self.client.post('/api/v1/projects/discover', json={'source_path': str(self.source)}).status_code, 422)
        self.client.headers.pop('Authorization')
        self.assertEqual(self.client.post('/api/v1/projects/discover', json={'source_path': str(self.parent)}).status_code, 401)

    def test_parent_registration_and_one_requirement_select_only_registered_repositories(self):
        project = self.register()
        self.assertEqual(project['repositories'], self.names)
        for selected in ([], ['../escape'], [self.names[0], self.names[0]]):
            response = self.client.post('/api/v1/requirements', json=dict(project_id=project['id'], title='invalid selection', repositories=selected))
            self.assertEqual(response.status_code, 422)
        req, workspace = self.prepared_multi(project, [self.names[1]])
        self.assertEqual(req['repositories'], [self.names[1]])
        self.assertTrue((workspace / 'repo' / self.names[1] / '.git').is_dir())
        self.assertFalse((workspace / 'repo' / self.names[0]).exists())
        self.assertTrue((workspace / 'repo/AGENTS.md').is_file())
        self.assertFalse((workspace / 'repo/README.md').exists())
        self.assertFalse((workspace / 'repo/.env').exists())
        self.assertFalse((workspace / 'repo/.git').exists())

    def harness_link(self, directory, target, name='.harness'):
        (directory / name).write_text(target)
        git(directory, 'add', '--all')
        oid = git(directory, 'hash-object', '-w', name).decode().strip()
        git(directory, 'update-index', '--cacheinfo', f'120000,{oid},{name}')
        git(directory, '-c', 'commit.gpgsign=false', 'commit', '-m', 'committed harness link')

    def test_child_harness_links_copy_literal_content_without_turning_parent_into_a_git_repository(self):
        targets = ['main.txt', '../missing-parent-config']
        for name, target in zip(self.names, targets):
            self.harness_link(self.parent / name, target)
        req, workspace = self.prepared_multi()
        value = json.loads((workspace / 'workspace.json').read_text())
        self.assertFalse((self.parent / '.git').exists())
        for index, name in enumerate(self.names):
            child = workspace / 'repo' / name
            self.assertEqual((child / '.harness').read_text(), targets[index])
            self.assertFalse((child / '.harness').is_symlink())
            self.assertEqual(value['repositories'][index]['copied_link_files'], [{'path': '.harness', 'source_mode': '120000', 'stored_as': 'file'}])
        self.assertEqual(len(req['workspace']['snapshot']['repositories']), 2)
        # Read-only analysis copies use ordinary files and can still include dirty edits.
        (workspace / 'repo' / self.names[0] / '.harness').write_text('requirement-only change')
        second = create_workspace(workspace / 'repo', self.root / 'analysis', 'analysis-copy', 'backend',
            include_dirty=True, include_untracked=True)
        self.assertEqual((Path(second['workspace']) / 'repo' / self.names[0] / '.harness').read_text(), 'requirement-only change')

    def test_failed_child_copy_reports_repository_path_and_retries_without_partial_workspace(self):
        directory = self.parent / self.names[1]
        self.harness_link(directory, '../outside-project', name='source-link')
        project = self.register()
        req = self.requirement(project)
        self.action(req, 'prepare'); self.assertTrue(self.runner.run_once())
        first = self.detail(req)
        self.assertEqual(first['jobs'][0]['status'], 'FAILED')
        self.assertIn(self.names[1], first['jobs'][0]['error'])
        self.assertIn('source-link', first['jobs'][0]['error'])
        self.assertFalse(Path(first['workspace']['path']).exists())
        self.harness_link(directory, 'main.txt', name='source-link')
        self.action(req, 'prepare'); self.assertTrue(self.runner.run_once())
        self.assertEqual(self.detail(req)['jobs'][0]['status'], 'SUCCEEDED')

    def test_invalid_overlapping_case_colliding_and_escaping_paths_are_rejected(self):
        with self.assertRaises(WorkbenchError): relative_names(['a', 'a-b', 'a/b'])
        for names in (['../other'], ['/absolute'], ['order service', 'ORDER SERVICE'], [self.names[0], self.names[0] + '/nested'], ['services\\payment 中文'], ['.git'], ['.']):
            response = self.client.post('/api/v1/projects', json=dict(name='invalid', source_path=str(self.parent), repositories=names))
            self.assertEqual(response.status_code, 422, names)
        for shared in (['.env'], [self.names[0] + '/main.txt'], ['../secret']):
            response = self.client.post('/api/v1/projects', json=dict(name='invalid', source_path=str(self.parent), repositories=self.names, shared_files=shared))
            self.assertEqual(response.status_code, 422, shared)

    def test_source_symlinks_and_workbench_data_parent_cannot_be_scanned(self):
        link = self.root / 'linked'; link.symlink_to(self.parent, target_is_directory=True)
        self.assertEqual(self.client.post('/api/v1/projects/discover', json={'source_path': str(link)}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/projects/discover', json={'source_path': str(self.root)}).status_code, 422)
        (self.parent / 'outside').symlink_to(self.source, target_is_directory=True)
        self.assertNotIn('outside', [item['path'] for item in discover(str(self.parent), self.data)['repositories']])

    def test_parent_under_an_unrelated_ancestor_git_worktree_can_be_registered(self):
        git(self.root, 'init', '-b', 'main')
        self.assertEqual(self.register()['repositories'], self.names)

    def test_each_requirement_has_independent_repositories_branches_and_common_files(self):
        original = [snapshot(self.parent / name) for name in self.names]
        project = self.register()
        rename = Path.rename
        def another_drive(path, target):
            if path.name == 'repo': raise OSError(errno.EXDEV, 'controlled cross-drive move')
            return rename(path, target)
        with patch.object(Path, 'rename', autospec=True, side_effect=another_drive): first, a = self.prepared_multi(project)
        second, b = self.prepared_multi(project)
        for name in self.names:
            self.assertEqual(git(a / 'repo' / name, 'branch', '--show-current').decode().strip(), 'req/' + first['id'])
            self.assertEqual(git(b / 'repo' / name, 'remote').strip(), b'')
            (a / 'repo' / name / 'main.txt').write_text('isolated edit\n')
            self.assertNotEqual((a / 'repo' / name / 'main.txt').read_bytes(), (b / 'repo' / name / 'main.txt').read_bytes())
        (a / 'repo/AGENTS.md').write_text('private shared change\n')
        self.assertEqual((b / 'repo/AGENTS.md').read_text(), 'Shared instructions\n')
        self.assertEqual([snapshot(self.parent / name) for name in self.names], original)

    def test_joint_snapshot_includes_modified_added_deleted_binary_and_shared_files(self):
        req, workspace = self.prepared_multi()
        original = snapshot(workspace / 'repo')
        (workspace / 'repo' / self.names[0] / 'main.txt').unlink()
        (workspace / 'repo' / self.names[1] / 'binary.dat').write_bytes(b'\x00\xff\x00\x01')
        (workspace / 'repo/AGENTS.md').write_text('new instructions\n')
        (workspace / 'repo/plan.md').write_text('new plan\n')
        observed = self.runner.executor.observe(workspace)
        self.assertNotEqual(observed['snapshot']['tree_digest'], original['tree_digest'])
        self.assertTrue(all(item['changed'] for item in observed['snapshot']['repositories']))
        self.assertIn('GIT binary patch', observed['diff'])
        self.assertIn('a/' + self.names[0] + '/main.txt', observed['diff'])
        self.assertIn('b/plan.md', observed['diff'])
        self.assertIn('b/AGENTS.md', observed['diff'])
        role = Path(create_workspace(workspace / 'repo', self.data / 'attempts' / ('job_' + 'a' * 32), req['id'], req['project_id'], include_dirty=True, include_untracked=True)['workspace'])
        self.assertEqual(snapshot(role / 'repo')['tree_digest'], observed['snapshot']['tree_digest'])
        self.assertEqual([item['path'] for item in layout(role / 'repo')['repositories']], self.names)

    def test_repository_patches_apply_independently_to_clean_exported_baselines(self):
        req, workspace = self.prepared_multi()
        (workspace / 'repo' / self.names[0] / 'main.txt').write_text('changed order\n')
        (workspace / 'repo' / self.names[1] / 'added.bin').write_bytes(b'\x00binary\xff')
        for name in self.names:
            patch_bytes = diff(workspace, repository=name)
            result = subprocess.run(['git', '-C', str(workspace / 'repo' / name), 'apply', '--reverse', '--check', '-'], input=patch_bytes, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        combined = full_diff(self.runner.executor, workspace, snapshot(workspace / 'repo')['files'])
        self.assertIn(b'changed order', combined)
        self.assertIn(b'GIT binary patch', combined)

    def test_any_candidate_change_invalidates_confirmed_snapshot(self):
        req, workspace = self.prepared_multi()
        response = self.client.post(f"/api/v1/requirements/{req['id']}/reviews", json={'digest': req['workspace']['snapshot']['tree_digest'], 'decision': 'APPROVED'})
        self.assertEqual(response.status_code, 202); self.runner.run_once()
        self.assertEqual(self.detail(req)['workspace']['approval'], 'APPROVED')
        (workspace / 'repo' / self.names[1] / 'main.txt').write_text('payment changed\n')
        observed = self.runner.executor.observe(workspace)
        self.store.idle_observation(req['id'], observed['snapshot'], observed['diff'])
        self.assertEqual(self.detail(req)['workspace']['approval'], 'STALE')

    def test_child_git_metadata_and_shared_baseline_cannot_redirect_to_host(self):
        _, workspace = self.prepared_multi()
        alternates = workspace / 'repo' / self.names[1] / '.git/objects/info/alternates'
        alternates.write_text(str(self.source / '.git/objects'))
        with self.assertRaisesRegex(WorkbenchError, '外部 Git'): Executor.guard_git(workspace / 'repo')
        alternates.unlink()
        (workspace / 'shared-base/AGENTS.md').write_text('tampered\n')
        with self.assertRaisesRegex(WorkbenchError, '基线摘要'): checked_shared_base(workspace, layout(workspace / 'repo'))

    def test_unregistered_git_and_external_common_links_block_observation(self):
        _, workspace = self.prepared_multi()
        (workspace / 'repo/new/.git').mkdir(parents=True)
        with self.assertRaisesRegex(WorkbenchError, 'Git 元数据'): snapshot(workspace / 'repo')
        shutil.rmtree(workspace / 'repo/new')
        (workspace / 'repo/outside').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(WorkbenchError): snapshot(workspace / 'repo')

    def test_workflow_pins_verification_commands_for_exact_selected_repositories(self):
        project = self.register()
        content = Content(verification_command='', repository_commands=[{'path': path, 'verification_command': 'node --test'} for path in self.names]).model_dump()
        profile = self.save_profile(project, content)
        req, _ = self.prepared_multi(project, [self.names[1]])
        body = dict(review_models=['opencode/glm-5.3-flash-free', 'opencode/big-pickle'], use_repository_commands=True,
            reason='repository validation', digest=req['workspace']['snapshot']['tree_digest'])
        response = self.client.post(f"/api/v1/requirements/{req['id']}/workflow", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['config']['repository_verification'], [{'path': self.names[1], 'argv': ['node', '--test']}])
        self.assertEqual(self.detail(req)['profile']['id'], profile['id'])

    def test_incomplete_repository_validation_and_foreign_configuration_are_rejected(self):
        project = self.register()
        content = Content(verification_command='', repository_commands=[{'path': self.names[0], 'verification_command': 'node --test'}]).model_dump()
        self.save_profile(project, content)
        req, _ = self.prepared_multi(project)
        response = self.client.post(f"/api/v1/requirements/{req['id']}/workflow", json=dict(review_models=['opencode/glm-5.3-flash-free', 'opencode/big-pickle'], use_repository_commands=True,
            reason='missing candidate validation', digest=req['workspace']['snapshot']['tree_digest']))
        self.assertEqual(response.status_code, 409)
        content['repository_commands'][0]['path'] = 'foreign'
        self.assertEqual(self.client.post(f"/api/v1/projects/{project['id']}/profiles", json=dict(content=content, actor='Fixture', reason='foreign')).status_code, 409)
        content['repository_commands'][0]['path'] = '../escape'
        self.assertEqual(self.client.post(f"/api/v1/projects/{project['id']}/profiles", json=dict(content=content, actor='Fixture', reason='escape')).status_code, 422)

    def test_per_repository_execution_uses_cwd_and_retains_failure_records(self):
        req, workspace = self.prepared_multi()
        write_json(workspace / 'runtime.json', {'container': 'fixture'})
        self.addCleanup(lambda: (workspace / 'runtime.json').unlink(missing_ok=True))
        plans = [{'path': path, 'argv': ['fixture-test']} for path in self.names]
        calls = []
        def command(*args):
            calls.append(args)
            return [sys.executable, '-c', 'import sys;print("executed fixture");sys.exit(' + ('1' if len(calls) == 2 else '0') + ')']
        with patch('agent_workbench.workflow_execution.docker_argv', side_effect=command):
            with self.assertRaises(WorkbenchError) as failed:
                verify_command(self.runner.executor, {}, workspace, lambda *_: None, threading.Event(), plans=plans)
        records = failed.exception.command_output['repositories']
        self.assertEqual([item['exit_code'] for item in records], [0, 1])
        self.assertEqual([item[2] for item in calls], ['/workspace/project/' + path for path in self.names])
        self.action(req, 'refresh')
        job = self.store.claim('verification-results')
        self.store.finish(job['id'], 'verification-results', job['epoch'], 'FAILED', {'command_output': failed.exception.command_output}, 'controlled failure')
        response = self.client.get('/api/v1/jobs/' + job['id'] + '/verification')
        self.assertEqual(response.json(), {'repositories': records})
        self.assertNotIn('snapshot', response.json())
        self.assertNotIn('command_output', self.detail(req)['jobs'][0].get('result') or {})
        self.client.headers.pop('Authorization')
        self.assertEqual(self.client.get('/api/v1/jobs/' + job['id'] + '/verification').status_code, 401)

    def test_backup_restores_all_git_histories_shared_baseline_and_repository_selection(self):
        req, workspace = self.prepared_multi()
        (workspace / 'repo' / self.names[1] / 'main.txt').write_text('preserved change\n')
        (workspace / 'repo/AGENTS.md').write_text('preserved common edit\n')
        observed = self.runner.executor.observe(workspace)
        self.store.idle_observation(req['id'], observed['snapshot'], observed['diff'])
        with self.store.engine.begin() as c:
            c.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)'))
            c.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
        with patch('agent_workbench.resources.inventory', return_value=[]): report = backup(self.settings)
        destination = self.root / 'restored'; restore(Path(report['file']), destination)
        reopened = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            self.assertEqual(reopened.get(requirements, req['id'])['repositories'], self.names)
            restored = destination / workspace.relative_to(self.data)
            self.assertEqual(snapshot(restored / 'repo')['tree_digest'], observed['snapshot']['tree_digest'])
            self.assertEqual(diff(restored), diff(workspace))
            self.assertEqual((restored / 'shared-base/AGENTS.md').read_text(), 'Shared instructions\n')
            Executor.guard_git(restored / 'repo')
        finally: reopened.engine.dispose()

    def test_partial_success_report_cannot_satisfy_multi_repository_verification_gate(self):
        project = self.register()
        self.save_profile(project, Content(verification_command='', repository_commands=[{'path': path, 'verification_command': 'node --test'} for path in self.names]).model_dump())
        req, workspace = self.prepared_multi(project)
        endpoint = f"/api/v1/requirements/{req['id']}/workflow"
        response = self.client.post(endpoint, json=dict(review_models=['opencode/glm-5.3-flash-free', 'opencode/big-pickle'], use_repository_commands=True,
            reason='all repositories are required', digest=req['workspace']['snapshot']['tree_digest']))
        self.assertEqual(response.status_code, 201, response.text)
        flow = response.json()
        for step in ('analysis', 'scope', 'development', 'verification'):
            queued = self.client.post(endpoint + '/steps', json=dict(flow_id=flow['id'], step_key=step, decision='APPROVED', reason='Fixture gate'))
            self.assertEqual(queued.status_code, 202, queued.text)
            job = self.store.claim('fixture')
            output = ({'summary': 'candidate analysis', 'impacted_paths': [self.names[0] + '/main.txt'], 'plan': 'cross-repository plan', 'risks': []} if step == 'analysis' else
                {'summary': 'controlled development fixture', 'verification_notes': 'verification is separate'} if step == 'development' else
                {'command': [], 'exit_code': 0, 'repositories': [{'path': self.names[0], 'command': ['node', '--test'], 'exit_code': 0}]} if step == 'verification' else job['input']['request'])
            self.store.finish(job['id'], 'fixture', job['epoch'], 'SUCCEEDED', {**self.runner.executor.observe(workspace), 'workflow_output': output}, None)
            self.assertEqual(self.store.get(jobs, job['id'])['status'], 'FAILED' if step == 'verification' else 'SUCCEEDED')
        self.assertEqual(self.client.get(endpoint).json()['current']['next_step'], 'verification')

    def test_full_delivery_preserves_repository_metadata_and_independent_patches(self):
        req, workspace = self.prepared_multi()
        for name in self.names: (workspace / 'repo' / name / 'main.txt').write_text('delivery fixture\n')
        observed = snapshot(workspace / 'repo')
        payload = {'group_id': 'controlled-group', 'revision': {'id': 'controlled-revision', 'content': {}},
            'context_digest': 'a' * 64, 'scope_decision': {}, 'integration': {}, 'request': {'kind': 'capture'},
            'members': [{'required': True, 'workspace_path': str(workspace), 'project_id': req['project_id'], 'requirement_id': req['id'],
                'baseline': {'code_digest': observed['tree_digest'], 'commit_sha': observed['commit_sha']}, 'history': {}}]}
        job_id = 'job_' + 'a' * 32
        build_delivery(self.runner.executor, {'id': job_id, 'input': payload}, {})
        archive_path = self.data / 'deliveries' / (job_id + '.zip')
        header = verify(archive_path)
        self.assertEqual([item['path'] for item in header['projects'][0]['repositories']], self.names)
        with zipfile.ZipFile(archive_path) as archive:
            for name in self.names:
                prefix = 'projects/' + req['project_id']
                self.assertIn(b'delivery fixture', archive.read(prefix + '/code/' + name + '/main.txt'))
                self.assertIn(b'delivery fixture', archive.read(prefix + '/repositories/' + name + '/changes.patch'))

    def test_legacy_0009_backup_restores_single_repository_with_empty_new_metadata(self):
        # A legacy archive really omits the new columns; its original digest is checked first.
        project = self.project(); req = self.requirement(project)
        self.action(req, 'prepare'); self.assertTrue(self.runner.run_once())
        with self.store.engine.begin() as c:
            c.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)'))
            c.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
        with patch('agent_workbench.resources.inventory', return_value=[]): report = backup(self.settings)
        header = verify(Path(report['file'])); header.pop('entries')
        header['schema_revision'] = '0009'
        entries, data = [], {}
        with zipfile.ZipFile(report['file']) as archive:
            for name in archive.namelist():
                if name == 'manifest.json': continue
                content = archive.read(name)
                if name.startswith('database/'):
                    table = name.removeprefix('database/').removesuffix('.json')
                    rows = json.loads(content)
                    if table in ('projects', 'requirements'):
                        for row in rows:
                            row.pop('repositories_json', None); row.pop('shared_files_json', None)
                    data[table] = rows; content = json.dumps(rows).encode()
                entries.append((name, content))
        header['metadata'] = metadata_digest(data)
        legacy = self.root / 'legacy-0009.zip'; build_archive(legacy, header, entries)
        destination = self.root / 'legacy-restored'; restore(legacy, destination)
        reopened = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            self.assertEqual(reopened.get(projects, project['id'])['repositories'], [])
            self.assertEqual(reopened.get(requirements, req['id'])['repositories'], [])
            self.assertTrue((destination / 'workspaces' / req['id'] / project['id'] / 'repo/.git').is_dir())
        finally: reopened.engine.dispose()
