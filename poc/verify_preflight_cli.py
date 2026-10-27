"""Real Docker/API/browser preflight with a controlled wrapper-only PATH and ENTRYPOINT.

No model calls. The HTTP service is a protocol fixture, not the user's wrapper.
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists(): raise ValueError('Use a new dedicated verification directory')
    root.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0)); port = reserved.getsockname()[1]
    config = root/'workbench.toml'
    config.write_text('[workbench]\nuse_sqlite=true\ndata_dir="data"\nmodel="opencode/glm-5.3-flash-free"\n'
        + f'port={port}\nweb_dir=' + json.dumps(str(repo/'web/dist')) + '\n',encoding='utf-8')
    settings = Settings.load(config_file=config)
    image = 'awb-user/preflight-cli:' + uuid.uuid4().hex[:12]
    context = root/'context'; binary = context/'rootfs/opt/awb-preflight-bin'; binary.mkdir(parents=True)
    shutil.copyfile(repo/'poc/preflight-wrapper-fixture.cjs',binary/'team-code')
    for name, target in [('node','/usr/local/bin/node'),('git','/usr/bin/git'),('sh','/bin/sh')]:
        (binary/name).write_text('#!/bin/sh\nexec '+target+' "$@"\n')
    (context/'Dockerfile').write_text('FROM agent-workbench-m1:local\nCOPY --chmod=755 rootfs/ /\n'
        'ENV PATH=/opt/awb-preflight-bin\nENTRYPOINT ["team-code"]\nCMD ["serve","--hostname","0.0.0.0","--port","4096"]\n')
    base = f'http://127.0.0.1:{port}'
    environment = {key:value for key,value in os.environ.items() if not key.startswith('AWB_')}
    environment.update(PYTHONUTF8='1',PYTHONPATH=str(repo/'src'))
    client = httpx.Client(base_url=base+'/api/v1',trust_env=False,timeout=20,headers={'Authorization':'Bearer '+settings.access_token})
    processes, handles, checks = [], [], []
    report = {'status':'RUNNING','real_docker':True,'model_inference':False,'controlled_compatible_http_service':True,
        'user_wrapper_verified':False,'checks':checks}
    def check(name): checks.append(name); print(name,flush=True)
    def get(path):
        response=client.get(path); response.raise_for_status(); return response.json()
    def post(path,body):
        response=client.post(path,json=body); response.raise_for_status(); return response.json()
    def wait(identity, expected='SUCCEEDED'):
        deadline=time.monotonic()+130
        while time.monotonic()<deadline:
            job=get('/jobs/'+identity)
            if job['status'] not in ('QUEUED','RUNNING'):
                assert job['status']==expected,job.get('error'); return job
            time.sleep(.2)
        raise AssertionError('Job did not finish')
    def launch(module):
        handle=(root/(module.rsplit('.',1)[-1]+'.log')).open('a'); handles.append(handle)
        processes.append(subprocess.Popen([sys.executable,'-m',module,'--config',str(config)],env=environment,cwd=repo,stdout=handle,stderr=subprocess.STDOUT))
    def save_cli(name,verb='serve'):
        return post('/agent-clis',dict(name=name,actor='Fixture',reason='Controlled preflight, no model inference',set_default=True,
            content=AgentContent(serve_command=f'team-code {verb} --hostname {{host}} --port {{port}}',
                run_command='team-code run --attach {endpoint} --model {model} --format json',version_command='team-code --version',expected_version='team-preflight-fixture/1.0').model_dump()))
    try:
        runtime.docker('build','--tag',image,str(context),timeout=180)
        missing=runtime.docker('run','--rm','--network','none','--entrypoint','/bin/sh',image,'-c','command -v opencode',check=False)
        assert missing.returncode!=0
        check('image_has_wrapper_only_path_and_custom_entrypoint_without_opencode_command')
        launch('agent_workbench.server')
        for _ in range(100):
            try:
                if client.get('/session').status_code==200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        else: raise AssertionError('API did not start')
        launch('agent_workbench.runner')
        chosen=save_cli('封装 CLI 预检样例')
        source=root/'source'; source.mkdir(); (source/'main.txt').write_text('original\n')
        for argv in [('init','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.invalid'),('add','--all'),('-c','commit.gpgsign=false','commit','-m','fixture')]: git(source,*argv)
        original=snapshot(source)
        project=post('/projects',dict(name='封装 CLI 镜像预检',source_path=str(source),image=image))
        post(f"/projects/{project['id']}/profiles",dict(content=ProfileContent(image=image,verification_command='node --version').model_dump()))
        req=post('/requirements',dict(project_id=project['id'],title='封装 CLI 预检成功'))
        endpoint='/requirements/'+req['id']
        for action in ('prepare','preflight'):wait(post(endpoint+'/actions',{'action':action})['id'])
        detail=get(endpoint); passed=detail['preflight']
        assert passed['status']=='PASSED' and passed['agent_cli']['id']==chosen['id']
        assert passed['tools']['agent_cli']['value']=='team-preflight-fixture/1.0'
        assert passed['commands']['serve'][0]=='team-code' and all(item['passed'] for item in passed['checks'])
        check('real_readonly_preflight_uses_pinned_wrapper_serve_and_stderr_version_commands')
        wait(post(endpoint+'/actions',{'action':'start'})['id'])
        workspace=Path(get(endpoint)['workspace']['path'])
        state=json.loads((workspace/'runtime.json').read_text())
        inspected=json.loads(runtime.docker('inspect',state['container']).stdout)[0]
        assert not inspected['Config']['Entrypoint'] and inspected['Config']['Cmd'][0]=='team-code'
        assert runtime.probe(workspace)['health']['healthy']
        wait(post(endpoint+'/actions',{'action':'stop'})['id'])
        check('development_runtime_honors_wrapper_command_over_image_entrypoint')
        broken=save_cli('启动失败的封装 CLI','fail-serve')
        failed_req=post('/requirements',dict(project_id=project['id'],title='封装 CLI 启动失败诊断'))
        failed_endpoint='/requirements/'+failed_req['id']
        wait(post(failed_endpoint+'/actions',{'action':'prepare'})['id'])
        started=time.monotonic()
        failed=wait(post(failed_endpoint+'/actions',{'action':'preflight'})['id'],expected='FAILED')
        elapsed=time.monotonic()-started
        detail=get(failed_endpoint); diagnostic=detail['preflight']
        assert elapsed<25,elapsed
        assert diagnostic['agent_cli']['id']==broken['id']
        assert 'team-code fail-serve' in failed['error']
        assert diagnostic['diagnostics']['exit_code']==7
        assert 'CONTROLLED_WRAPPER_STARTUP_FAILED' in diagnostic['diagnostics']['log_tail']
        check('exited_wrapper_fails_promptly_with_cli_identity_command_exit_code_and_log_tail')
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100}); errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base); page.get_by_label('本机访问令牌',exact=True).fill(settings.access_token)
            page.get_by_role('button',name='进入工作台',exact=True).click()
            page.locator('.requirement-card').filter(has_text='封装 CLI 启动失败诊断').click()
            page.get_by_text('环境与手动工具',exact=True).click()
            panel=page.locator('.environment-panel')
            expect(panel.get_by_text('本次检查：启动失败的封装 CLI · v1',exact=True)).to_be_visible()
            panel.get_by_text('查看 CLI 启动诊断',exact=True).click()
            expect(panel.locator('pre')).to_contain_text('CONTROLLED_WRAPPER_STARTUP_FAILED')
            page.screenshot(path=str(root/'failed-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(root/'failed-mobile.png'),full_page=True)
            check('browser_displays_selected_cli_commands_and_failure_diagnostics_on_desktop_and_mobile')
            page.locator('.requirement-card').filter(has_text='封装 CLI 预检成功').click()
            page.get_by_text('环境与手动工具',exact=True).click()
            expect(panel.get_by_text('环境预检通过',exact=True)).to_be_visible()
            expect(panel.get_by_text('检测到的版本：team-preflight-fixture/1.0',exact=True)).to_be_visible()
            assert not errors,errors
            browser.close()
        assert snapshot(source)==original
        check('source_unchanged_and_no_browser_errors_or_model_calls')
        report.update(status='PASSED',completed_checks=len(checks))
    except BaseException as error:
        report.update(status='FAILED',completed_checks=len(checks),error_type=type(error).__name__)
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:process.terminate()
        for process in processes:
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        client.close()
        for handle in handles:handle.close()
        try:resources.stop_all(settings.data_dir)
        finally:runtime.docker('image','rm',image,check=False)
        write_json(root/'report.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
