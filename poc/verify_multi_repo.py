"""Browser -> API -> Runner -> real Docker, with an explicitly offline CLI fixture."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench import resources, runtime
from agent_workbench.agent_clis import Content as AgentContent
from agent_workbench.execution import Executor
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.profiles import inherit
from agent_workbench.settings import Settings
from agent_workbench.store import Store, requirements
from agent_workbench.workspace import create_workspace, git, snapshot, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = args.root.resolve()
    if root.exists(): raise ValueError('Use a new verification directory')
    root.mkdir(parents=True)
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0)); port = reserved.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    image = 'awb-user/multi-repo:' + uuid.uuid4().hex[:12]
    config = root / 'workbench.toml'
    config.write_text('[workbench]\nuse_sqlite=true\nport=' + str(port) + '\nmodel="opencode/glm-5.3-flash-free"\ndata_dir="data"\nweb_dir=' +
        json.dumps(str(repo / 'web/dist')) + '\n[launcher]\ntool_image=' + json.dumps(image) + '\n', encoding='utf-8')
    settings = Settings.load(config_file=config)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('AWB_')}
    environment.update(PYTHONUTF8='1', PYTHONPATH=str(repo / 'src'))
    processes, handles, checks = [], [], []
    report = {'status': 'RUNNING', 'model_inference': False, 'controlled_cli_reports': True, 'real_docker': True,
        'container_scheduling': 'development_and_readonly_serialized', 'checks': checks}
    names = ['order service', 'services/payment 中文']
    parent = root / 'business parent 中文'; parent.mkdir()
    for name in [*names, 'ignored-service']:
        folder = parent / name; folder.mkdir(parents=True)
        (folder / 'main.txt').write_text('original\n')
        (folder / 'test.cjs').write_text("const assert=require('node:assert/strict');const fs=require('node:fs');\nassert.equal(fs.readFileSync('main.txt','utf8'),'controlled multirepo change\\n');\nconsole.log('real per-repository verification');\n")
        for argv in [('init', '-b', 'main'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid'), ('add', '--all'), ('-c', 'commit.gpgsign=false', 'commit', '-m', 'source fixture')]: git(folder, *argv)
    (parent / 'AGENTS.md').write_text('Controlled offline multi-repository verification.\n')
    (parent / 'README.md').write_text('Not selected for export.\n')
    (parent / '.env').write_text('PRIVATE_FIXTURE_MUST_NOT_COPY=true\n')
    original = {name: snapshot(parent / name) for name in [*names, 'ignored-service']}
    client = httpx.Client(base_url=base + '/api/v1', headers={'Authorization': 'Bearer ' + settings.access_token}, trust_env=False, timeout=20)
    def check(name): checks.append(name); print(name, flush=True)
    def get(path):
        response = client.get(path); response.raise_for_status(); return response.json()
    def post(path, body):
        response = client.post(path, json=body, headers={'Idempotency-Key': uuid.uuid4().hex}); response.raise_for_status(); return response.json()
    def wait(job_id, expected='SUCCEEDED'):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            job = get('/jobs/' + job_id)
            if job['status'] not in ('QUEUED', 'RUNNING'):
                assert job['status'] == expected, {'status': job['status'], 'error': job.get('error')}
                return job
            time.sleep(.2)
        raise AssertionError('Job did not finish')
    def terminate():
        for process in reversed(processes):
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=25)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    def launch(module):
        handle = (root / (module.rsplit('.', 1)[-1] + '.log')).open('a'); handles.append(handle)
        processes.append(subprocess.Popen([sys.executable, '-m', module, '--config', str(config)], cwd=repo, env=environment, stdout=handle, stderr=subprocess.STDOUT))
    try:
        runtime.docker('build', '--tag', image, str(repo / 'poc/cli-wrapper'), timeout=180)
        launch('agent_workbench.server')
        for _ in range(100):
            try:
                if client.get('/session').status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        else: raise AssertionError('API did not start')
        cli = post('/agent-clis', dict(name='Offline multi-repository fixture', actor='Fixture', reason='Controlled local CLI, no model inference',
            content=AgentContent(serve_command='env OPENCODE_DISABLE_MODELS_FETCH=true OPENCODE_MODELS_PATH=/opt/awb-models.fixture.json awb-opencode serve --hostname {host} --port {port}',
                run_command='awb-opencode fixture-multi-run --attach {endpoint} --model {model} --format json',
                version_command='awb-opencode --version', expected_version='awb-opencode-fixture/1.0.0').model_dump(), set_default=True))
        launch('agent_workbench.runner')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, args=['--no-sandbox'], headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1100}); page.set_default_timeout(20000)
            errors = []; page.on('pageerror', lambda failure: errors.append(str(failure)))
            page.goto(base, wait_until='networkidle')
            page.get_by_label('本机访问令牌', exact=True).fill(settings.access_token)
            page.get_by_role('button', name='进入工作台', exact=True).click()
            page.get_by_role('button', name='＋ 登记项目', exact=True).click()
            dialog = page.get_by_role('dialog')
            dialog.get_by_label('项目目录类型', exact=True).select_option('multiple')
            dialog.get_by_label('项目名称', exact=True).fill('业务父目录 · 多 Git 样例')
            dialog.get_by_label('父目录绝对路径', exact=True).fill(str(parent))
            dialog.get_by_role('button', name='扫描 Git 仓库', exact=True).click()
            expect(dialog.get_by_role('checkbox', name='ignored-service', exact=False)).to_be_checked()
            dialog.get_by_role('checkbox', name='ignored-service', exact=False).uncheck()
            dialog.get_by_role('checkbox', name='README.md', exact=True).uncheck()
            expect(dialog.get_by_role('button', name='刷新镜像列表', exact=True)).to_be_enabled()
            dialog.get_by_role('combobox', name='运行镜像', exact=True).select_option(image)
            page.screenshot(path=str(root / 'registration-desktop.png'), full_page=True)
            dialog.get_by_role('button', name='登记项目', exact=True).click()
            expect(dialog).not_to_be_visible()
            project = get('/projects')[0]
            assert project['repositories'] == names and project['shared_files'] == ['AGENTS.md']
            check('browser_registers_parent_selects_two_git_repositories_and_shared_instructions')
            page.get_by_role('button', name='保存运行配置', exact=True).click()
            dialog.get_by_label('Agent CLI 配置', exact=True).select_option(cli['id'])
            for name in names: dialog.get_by_label(name + ' 验证命令', exact=True).fill('node --test test.cjs')
            dialog.get_by_label('登记人', exact=True).fill('Fixture')
            dialog.get_by_label('配置原因', exact=True).fill('Real Docker per-repository test commands')
            dialog.get_by_role('button', name='保存新版本', exact=True).click()
            expect(dialog).not_to_be_visible()
            check('browser_saves_wrapper_cli_and_per_repository_verification_in_immutable_profile')
            page.get_by_role('button', name='＋ 新建需求', exact=True).click()
            dialog.get_by_label('需求名称', exact=True).fill('一次录入的跨 Git 业务需求')
            expect(dialog.get_by_role('checkbox', name=names[0], exact=True)).to_be_checked()
            expect(dialog.get_by_role('checkbox', name=names[1], exact=True)).to_be_checked()
            dialog.get_by_label('验收标准', exact=True).fill('两个仓库均完成受控改动并分别通过真实测试；这是离线样例。')
            dialog.get_by_role('button', name='创建需求', exact=True).click()
            expect(dialog).not_to_be_visible()
            req = get('/requirements')[0]; endpoint = '/requirements/' + req['id']
            wait(post(endpoint + '/actions', {'action': 'prepare'})['id'])
            detail = get(endpoint); workspace = Path(detail['workspace']['path'])
            assert len(detail['workspace']['snapshot']['repositories']) == 2
            assert not (workspace / 'repo/.git').exists() and not (workspace / 'repo/ignored-service').exists()
            assert not (workspace / 'repo/README.md').exists() and not (workspace / 'repo/.env').exists()
            other = post('/requirements', dict(project_id=project['id'], title='并行隔离对照需求'))
            wait(post('/requirements/' + other['id'] + '/actions', {'action': 'prepare'})['id'])
            other_workspace = Path(get('/requirements/' + other['id'])['workspace']['path'])
            check('single_business_requirement_prepares_independent_git_repositories_and_parallel_control_copy')
            wait(post(endpoint + '/actions', {'action': 'preflight'})['id'])
            assert get(endpoint)['preflight']['status'] == 'PASSED'
            wait(post(endpoint + '/actions', {'action': 'start'})['id'])
            state = json.loads((workspace / 'runtime.json').read_text())
            inspected = json.loads(runtime.docker('inspect', state['container']).stdout)[0]
            assert inspected['Config']['WorkingDir'] == '/workspace/project'
            check('real_docker_wrapper_service_and_preflight_run_from_shared_parent')
            page.get_by_role('button', name='开启默认流程', exact=True).click()
            expect(page.get_by_role('checkbox', name='按固定运行配置逐仓库验证', exact=False)).to_be_checked()
            page.get_by_role('button', name='保存并开启本轮', exact=True).click()
            for _ in range(100):
                current = get(endpoint + '/workflow')['current']
                if current: break
                time.sleep(.1)
            else: raise AssertionError('Workflow not created')
            assert len(current['config']['repository_verification']) == 2
            def development_environment(enabled):
                if enabled != (workspace / 'runtime.json').exists():
                    wait(post(endpoint + '/actions', {'action': 'start' if enabled else 'stop'})['id'])
            for key in ['analysis', 'scope', 'development', 'verification', 'model_review_0', 'model_review_1', 'resolution', 'self_review']:
                development_environment(key in ('development', 'verification'))
                current = get(endpoint + '/workflow')['current']; assert current['next_step'] == key, current['next_step']
                job = post(endpoint + '/workflow/steps', dict(flow_id=current['id'], step_key=key, decision='APPROVED', reason='Controlled fixture inspection', prompt='', dispositions={}))
                finished = wait(job['id'])
                if key == 'verification':
                    assert [item['exit_code'] for item in finished['result']['workflow_output']['repositories']] == [0, 0]
            assert get(endpoint + '/workflow')['current']['state'] == 'COMPLETE'
            assert get(endpoint)['workspace']['approval'] == 'APPROVED'
            assert all((workspace / 'repo' / name / 'main.txt').read_text() == 'controlled multirepo change\n' for name in names)
            assert all((other_workspace / 'repo' / name / 'main.txt').read_text() == 'original\n' for name in names)
            assert {name: snapshot(parent / name) for name in original} == original
            check('actual_cli_stdin_json_transport_edits_both_repositories_tests_each_and_completes_two_readonly_reviews')
            created = create_workspace(workspace / 'repo', settings.data_dir / 'attempts/job_readonly_probe', req['id'], req['project_id'], include_dirty=True, include_untracked=True)
            role = Path(created['workspace']); inherit(workspace, role, settings.data_dir)
            try:
                Executor(settings).start_runtime(role, image, read_only_source=True, opencode_model=settings.model)
                readonly = json.loads((role / 'runtime.json').read_text())
                code = "const fs=require('node:fs');for(const p of " + json.dumps(names) + "){if(!fs.readFileSync(p+'/main.txt','utf8').includes('controlled'))process.exit(2);try{fs.writeFileSync(p+'/main.txt','bad');process.exit(3)}catch(e){if(!['EROFS','EACCES'].includes(e.code))throw e}}"
                assert runtime.docker('exec', readonly['container'], 'node', '-e', code, check=False).returncode == 0
            finally: runtime.stop(role)
            check('kernel_denies_writes_to_each_repository_in_joint_readonly_copy')
            material = post(endpoint + '/materials', dict(flow_id=current['id'], reason='Controlled multi-repository document transport', digest=get(endpoint)['workspace']['snapshot']['tree_digest']))
            material_context = get(endpoint + '/materials')['current']['context_digest']
            wait(post(endpoint + '/materials/steps', dict(round_id=material['id'], step_key='summary_design', context_digest=material_context, prompt='Controlled fixture only'))['id'])
            assert get(endpoint + '/materials')['current']['documents']['summary_design']['content']['title'].startswith('CONTROLLED_FIXTURE_NOT_MODEL')
            check('overview_document_generation_uses_complete_multi_repository_readonly_context')
            # The second command really fails; the successful first command cannot hide it.
            development_environment(True)
            (workspace / 'repo' / names[1] / 'test.cjs').write_text("process.exit(7);\n")
            failed = wait(post(endpoint + '/actions', {'action': 'verify'})['id'], expected='FAILED')
            records = failed['result']['command_output']['repositories']
            assert records[0]['exit_code'] == 0 and records[1]['exit_code'] != 0
            check('actual_failed_repository_preserves_per_repository_exit_codes_and_fails_the_job')
            page.reload(wait_until='networkidle')
            page.get_by_role('button', name='一次录入的跨 Git 业务需求', exact=False).click()
            expect(page.get_by_role('heading', name='一次录入的跨 Git 业务需求', exact=True)).to_be_visible()
            page.locator('.job-tabs').get_by_role('button', name='项目验证', exact=False).click()
            expect(page.get_by_role('heading', name='逐仓库验证记录', exact=True)).to_be_visible()
            summaries = page.locator('.log-panel .repository-options summary').all_text_contents()
            assert any(names[0] in value and '通过' in value for value in summaries)
            assert any(names[1] in value and '失败' in value for value in summaries)
            expect(page.get_by_label('代码差异', exact=True)).to_contain_text('controlled multirepo change')
            page.screenshot(path=str(root / 'workspace-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(root / 'workspace-mobile.png'), full_page=True)
            assert not errors, errors
            browser.close()
            check('browser_displays_repository_changes_and_mobile_layout_without_script_errors')
            wait(post(endpoint + '/actions', {'action': 'stop'})['id'])
        terminate(); stop(settings)
        archived = backup(settings); destination = root / 'restored'; restore(Path(archived['file']), destination)
        database = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            assert database.get(requirements, req['id'])['repositories'] == names
            restored = destination / workspace.relative_to(settings.data_dir)
            assert snapshot(restored / 'repo')['tree_digest'] == snapshot(workspace / 'repo')['tree_digest']
            assert all((restored / 'repo' / name / '.git').is_dir() for name in names)
        finally: database.engine.dispose()
        check('sqlite_0010_backup_restore_preserves_both_git_histories_common_baseline_and_one_requirement')
        report.update(status='PASSED_MULTI_REPOSITORY_WORKSPACE', checks_passed=len(checks))
    except BaseException as error:
        report.update(status='FAILED', checks_passed=len(checks), error_type=type(error).__name__)
        raise
    finally:
        client.close(); terminate()
        for handle in handles: handle.close()
        try: resources.stop_all(settings.data_dir)
        finally:
            runtime.docker('image', 'rm', image, check=False)
            write_json(root / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
