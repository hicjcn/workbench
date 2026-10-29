"""Real Docker/API/browser model-selection acceptance with controlled CLI output.

All task reports come from a local protocol fixture, not model inference.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench import resources, runtime
from agent_workbench.agent_clis import Content as AgentContent
from agent_workbench.profiles import Content as ProfileContent
from agent_workbench.settings import Settings
from agent_workbench.workspace import git, snapshot, write_json


IDS = ['team/analyzer', 'team/reviewer-a', 'team/reviewer-b']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists(): raise ValueError('Use a new dedicated verification directory')
    root.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0)); port = reserved.getsockname()[1]
    config = root / 'workbench.toml'
    config.write_text('[workbench]\nuse_sqlite=true\ndata_dir="data"\nmodel="opencode/glm-5.3-flash-free"\n'
        + f'port={port}\nweb_dir=' + json.dumps(str(repo / 'web/dist')) + '\n', encoding='utf-8')
    settings = Settings.load(config_file=config)
    image = 'awb-user/model-catalog:' + uuid.uuid4().hex[:12]
    context = root / 'context'; context.mkdir()
    shutil.copyfile(repo / 'poc/model-catalog-fixture.cjs', context / 'team-code')
    (context / 'Dockerfile').write_text('FROM node:22-bookworm-slim\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n'
        'COPY --chmod=755 team-code /usr/local/bin/team-code\n'
        'CMD ["sleep", "infinity"]\n', encoding='utf-8')
    credentials = root / 'controlled-cli-config'; credentials.mkdir()
    mode = credentials / 'catalog-mode'; mode.write_text('lines')
    source = root / 'source'; source.mkdir(); (source / 'main.txt').write_text('original\n')
    for argv in [('init', '-b', 'main'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid'),
                 ('add', '--all'), ('-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')]: git(source, *argv)
    original = snapshot(source)
    base = f'http://127.0.0.1:{port}'
    environment = {key: value for key, value in os.environ.items() if not key.startswith('AWB_')}
    environment.update(PYTHONUTF8='1', PYTHONPATH=str(repo / 'src'))
    client = httpx.Client(base_url=base + '/api/v1', trust_env=False, timeout=30,
        headers={'Authorization': 'Bearer ' + settings.access_token})
    processes, handles, checks = [], [], []
    report = dict(status='RUNNING', real_docker=True, real_api_and_runner=True, real_browser=True,
        model_inference=False, real_user_credentials=False, task_output='CONTROLLED_FIXTURE_ONLY', checks=checks)
    def check(name): checks.append(name); print(name, flush=True)
    def get(path):
        result=client.get(path); result.raise_for_status(); return result.json()
    def post(path, value):
        result=client.post(path,json=value); result.raise_for_status(); return result.json()
    def wait(identity, expected='SUCCEEDED'):
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            job=get('/jobs/'+identity)
            if job['status'] not in ('QUEUED','RUNNING'):
                assert job['status']==expected,job.get('error'); return job
            time.sleep(.2)
        raise AssertionError('Queued fixture job did not finish')
    def action(endpoint, name, expected='SUCCEEDED'):
        return wait(post(endpoint+'/actions',{'action':name})['id'],expected)
    def launch(module):
        handle=(root/(module.rsplit('.',1)[-1]+'.log')).open('a'); handles.append(handle)
        processes.append(subprocess.Popen([sys.executable,'-m',module,'--config',str(config)],
            env=environment,cwd=repo,stdout=handle,stderr=subprocess.STDOUT))
    try:
        runtime.docker('build','--tag',image,str(context),timeout=300)
        labels=json.loads(runtime.docker('image','inspect',image).stdout)[0]['Config'].get('Labels') or {}
        assert 'org.agent-workbench.superpowers-commit' not in labels
        launch('agent_workbench.server')
        for _ in range(100):
            try:
                if client.get('/session').status_code==200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        else: raise AssertionError('API did not start')
        launch('agent_workbench.runner')
        cli=post('/agent-clis',dict(name='Controlled model catalog',actor='Fixture',reason='Controlled CLI catalog, no model calls',set_default=True,
            content=AgentContent(serve_command='team-code serve',run_command='team-code run --attach {endpoint} --model {model} --format json',
                version_command='team-code --version',list_models_command='team-code models',expected_version='catalog-fixture/1').model_dump()))
        project=post('/projects',dict(name='CLI model catalog acceptance',source_path=str(source),image=image))
        post(f'/projects/{project["id"]}/profiles',dict(content=ProfileContent(image=image,run_as_root=True,agent_cli_id=cli['id'],
            volume_mounts=[dict(source=str(credentials),target='/root/.team-login',read_only=True)],
            verification_command='node --version').model_dump()))
        req=post('/requirements',dict(project_id=project['id'],title='CLI 模型下拉选择验收'))
        endpoint='/requirements/'+req['id']
        action(endpoint,'prepare'); action(endpoint,'preflight')
        detail=get(endpoint); workspace=Path(detail['workspace']['path'])
        assert detail['preflight']['status']=='PASSED'
        assert 'superpowers_revision' not in {item['name'] for item in detail['preflight']['checks']}
        assert next(item for item in detail['preflight']['checks'] if item['name']=='superpowers_loaded')['passed']
        check('image_without_superpowers_commit_label_passes_with_required_skill_available')
        action(endpoint,'start')
        assert get(endpoint+'/models')['status']=='NOT_LOADED'

        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100}); errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.goto(base)
            page.get_by_label('本机访问令牌',exact=True).fill(settings.access_token)
            page.get_by_role('button',name='进入工作台',exact=True).click()
            page.locator('.requirement-card').filter(has_text='CLI 模型下拉选择验收').click()
            panel=page.locator('.workflow-panel').filter(has=page.get_by_role('heading',name='分析与方案',exact=True))
            panel.get_by_role('button',name='开启默认流程',exact=True).click()
            form=panel.locator('.flow-start')
            expect(form.get_by_role('button',name='保存并开启本轮',exact=True)).to_be_disabled()
            form.get_by_role('button',name='读取模型列表',exact=True).click()
            primary=form.locator('select[name="development-model"]')
            expect(primary).to_be_enabled(timeout=30000)
            assert primary.locator('option').all_text_contents()==['请选择模型',*IDS]
            primary.select_option(IDS[0])
            form.locator('select[name="model-a"]').select_option(IDS[1])
            form.locator('select[name="model-b"]').select_option(IDS[1])
            expect(form.get_by_text('请选择两个不同的评审模型。',exact=True)).to_be_visible()
            expect(form.get_by_role('button',name='保存并开启本轮',exact=True)).to_be_disabled()
            form.locator('select[name="model-b"]').select_option(IDS[2])
            form.get_by_label('本轮原因',exact=True).fill('Controlled catalog dropdown selection')
            form.get_by_role('button',name='保存并开启本轮',exact=True).click()
            expect(form).not_to_be_visible(timeout=15000)
            state=get(endpoint+'/workflow'); flow=state['current']
            assert flow['config']['development_model']==IDS[0] and flow['config']['review_models']==IDS[1:]
            assert flow['config']['model_source']=='agent_cli'
            panel.get_by_role('button',name='新建流程轮次',exact=True).click()
            expect(primary).to_have_value(IDS[0])
            expect(form.locator('select[name="model-a"]')).to_have_value(IDS[1])
            expect(form.locator('select[name="model-b"]')).to_have_value(IDS[2])
            page.screenshot(path=str(root/'model-dropdown-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(root/'model-dropdown-mobile.png'),full_page=True)
            assert not errors,errors
            browser.close()
        catalog=get(endpoint+'/models')
        assert [item['id'] for item in catalog['models']]==IDS
        assert catalog['agent_cli_id']==cli['id']
        assert json.loads((workspace/'agent-home/model-command.json').read_text())['home']=='/root'
        check('browser_reads_real_cli_lines_deduplicates_models_and_saves_three_dropdown_choices_with_distinct_reviews')
        check('new_round_retains_only_catalog_choices_and_browser_has_no_errors_or_mobile_overflow')

        def workflow_step(key, **value):
            return wait(post(endpoint+'/workflow/steps',dict(flow_id=flow['id'],step_key=key,**value))['id'])
        workflow_step('analysis')
        analysis=get(endpoint+'/workflow')['current']['nodes'][0]
        assert analysis['model']==IDS[0] and analysis['output']['summary']=='CONTROLLED_FIXTURE_ANALYSIS'
        attempts=list((settings.data_dir/'attempts').glob(f'*/{req["id"]}/*/agent-home/last-task.json'))
        assert len(attempts)==1
        readonly=json.loads(attempts[0].read_text()); assert readonly['model']==IDS[0] and readonly['permission']['*']=='deny'
        workflow_step('scope',decision='APPROVED',reason='Controlled fixture scope confirmation')
        workflow_step('development')
        task=json.loads((workspace/'agent-home/last-task.json').read_text())
        journal=json.loads((workspace/'runtime.json').read_text())
        assert task['model']==IDS[0] and task['config_model']==IDS[0]
        assert journal['model']==IDS[0] and journal['model_source']=='agent_cli'
        check('readonly_analysis_and_development_use_selected_nonlegacy_model_and_pinned_runtime_configuration')
        workflow_step('verification')
        workflow_step('model_review_0'); workflow_step('model_review_1')
        review_models={json.loads(path.read_text())['model'] for path in (settings.data_dir/'attempts').glob(f'*/{req["id"]}/*/agent-home/last-task.json')}
        assert review_models==set(IDS)
        check('both_distinct_review_models_execute_through_their_actual_cli_model_arguments')

        mode.write_text('json'); action(endpoint,'list_models')
        refreshed=get(endpoint+'/models')
        assert [item['label'] for item in refreshed['models']]==['Analyzer','Reviewer A','Reviewer B']
        mode.write_text('fail'); action(endpoint,'list_models',expected='FAILED')
        failed=get(endpoint+'/models')
        assert failed['status']=='FAILED' and failed['models']==[]
        assert 'CONTROLLED_PRIVATE_FAILURE_DETAIL' not in json.dumps(failed)
        assert get(endpoint)['workspace']['environment']=='RUNNING'
        mode.write_text('empty'); action(endpoint,'list_models')
        assert get(endpoint+'/models')['status']=='EMPTY'
        mode.write_text('lines'); action(endpoint,'list_models')
        check('json_refresh_empty_list_and_failed_refresh_preserve_runtime_without_stale_or_private_output')
        action(endpoint,'stop'); action(endpoint,'force_start'); action(endpoint,'list_models')
        assert get(endpoint)['workspace']['environment']=='DEBUG_RUNNING'
        assert [item['id'] for item in get(endpoint+'/models')['models']]==IDS
        action(endpoint,'stop')
        check('cli_catalog_can_be_read_in_diagnostic_container_without_starting_agent_or_inference')
        assert snapshot(source)==original
        report.update(status='PASSED',completed_checks=len(checks))
    except BaseException as error:
        report.update(status='FAILED',error_type=type(error).__name__,error=str(error)[:2000]); raise
    finally:
        for process in reversed(processes):
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        client.close()
        for handle in handles: handle.close()
        try: resources.stop_all(settings.data_dir)
        finally: runtime.docker('image','rm',image,check=False)
        write_json(root/'report.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
