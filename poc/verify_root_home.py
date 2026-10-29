"""Real Docker/API regression for HOME-derived credentials in root containers.

The CLI and credentials are controlled fixtures. No model inference or real
provider credentials are used; successful task events are fixture output only.
"""
import argparse
import json
from pathlib import Path
import secrets
import uuid

from fastapi.testclient import TestClient
import httpx

from agent_workbench import resources, runtime
from agent_workbench.agent_clis import Content as AgentContent
from agent_workbench.api import create_app
from agent_workbench.profiles import Content as ProfileContent
from agent_workbench.runner import Runner
from agent_workbench.settings import Settings
from agent_workbench.store import Store, metadata
from agent_workbench.workspace import git, snapshot, write_json


CLI = r'''#!/usr/local/bin/node
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const [verb, ...args] = process.argv.slice(2);
if (process.env.HOME !== '/root') throw new Error('CONTROLLED_WRONG_HOME');
fs.accessSync(path.join(process.env.HOME, '.team-login/auth.json'), fs.constants.R_OK);
const config = JSON.parse(fs.readFileSync(process.env.OPENCODE_CONFIG, 'utf8'));
if (verb === '--version') { console.error('root-home-fixture/1'); process.exit(0); }
const metadata = {home:process.env.HOME, config_path:process.env.OPENCODE_CONFIG,
  permission:config.permission, cwd:process.cwd(), controlled_credentials_readable:true};
if (verb === 'run') {
  process.stdin.resume();
  process.stdin.on('end', () => {
    fs.writeFileSync('/root/.local/state/opencode/root-home-run.json', JSON.stringify(metadata));
    console.log(JSON.stringify({type:'text',part:{text:'Controlled HOME fixture completed; no model called'}}));
    console.log(JSON.stringify({type:'step_finish',part:{reason:'stop'}}));
  });
} else if (verb === 'serve') {
  fs.writeFileSync('/root/.local/share/opencode/root-home-serve.json', JSON.stringify(metadata));
  const authorization = 'Basic ' + Buffer.from('workbench:' + process.env.OPENCODE_SERVER_PASSWORD).toString('base64');
  let sequence = 0;
  http.createServer((request, response) => {
    if (request.headers.authorization !== authorization) { response.writeHead(401); response.end(); return; }
    request.resume(); response.setHeader('Content-Type','application/json');
    let body;
    if (request.url === '/global/health') body={healthy:true,version:'controlled-root-home'};
    else if (request.url === '/doc') body={openapi:'3.1.0',paths:{'/session':{post:{}},'/skill':{get:{}}}};
    else if (request.url === '/config') body=config;
    else if (request.url === '/skill') body=[{name:'using-superpowers'}];
    else if (request.url === '/session' && request.method === 'POST') body={id:'ses_root_' + (++sequence)};
    else if (request.url.startsWith('/session/') && ['POST','DELETE'].includes(request.method)) body=true;
    else { response.writeHead(404); response.end('{}'); return; }
    response.end(JSON.stringify(body));
  }).listen(4096,'0.0.0.0');
} else throw new Error('Unsupported fixture invocation');
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    root = parser.parse_args().root.resolve()
    if root.exists(): raise ValueError('Use a new dedicated verification directory')
    root.mkdir(parents=True)
    data = root / 'data'; data.mkdir()
    settings = Settings(data, f'sqlite:///{data / "test.db"}', secrets.token_urlsafe(32),
                        secrets.token_urlsafe(32), model='opencode/glm-5.3-flash-free')
    store = Store(settings.database_url); metadata.create_all(store.engine)
    client = TestClient(create_app(settings, store))
    client.headers['Authorization'] = 'Bearer ' + settings.access_token
    def dispatch(request):
        response = client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    runner = Runner(settings, 'http://testserver', transport=httpx.MockTransport(dispatch))
    image = 'awb-user/root-home:' + uuid.uuid4().hex[:12]
    context = root / 'context'; context.mkdir()
    (context / 'team-code').write_text(CLI, encoding='utf-8')
    (context / 'root.bashrc').write_text(
        'case $- in *i*) ;; *) return ;; esac\n'
        'export PATH="/root/.nvm/bin:$PATH"\n'
        'export OPENCODE_CONFIG=/tmp/incorrect-config.json\n', encoding='utf-8')
    (context / 'Dockerfile').write_text(
        'FROM node:22-bookworm-slim\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n'
        'COPY --chmod=755 team-code /root/.nvm/bin/team-code\n'
        'COPY root.bashrc /root/.bashrc\n'
        'CMD ["sleep", "infinity"]\n', encoding='utf-8')
    source = root / 'source'; source.mkdir()
    (source / 'main.txt').write_text('original\n')
    for argv in [('init','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.invalid'),
                 ('add','--all'),('-c','commit.gpgsign=false','commit','-m','fixture')]: git(source,*argv)
    original = snapshot(source)
    credentials = root / 'controlled-credentials'; credentials.mkdir()
    (credentials / 'auth.json').write_text('{"controlled":true}\n')
    checks = []
    report = dict(status='RUNNING', real_docker=True, real_api_and_runner=True, model_inference=False,
                  real_user_credentials=False, task_output='CONTROLLED_FIXTURE_ONLY', checks=checks)
    def check(name): checks.append(name); print(name, flush=True)
    def get(path):
        response=client.get('/api/v1'+path); response.raise_for_status(); return response.json()
    def post(path, body):
        response=client.post('/api/v1'+path,json=body); response.raise_for_status(); return response.json()
    def action(endpoint,name,**payload):
        job=post(endpoint+'/actions',dict(action=name,**payload)); assert runner.run_once()
        result=get('/jobs/'+job['id']); assert result['status']=='SUCCEEDED',result.get('error'); return result
    def inspect(workspace):
        state=json.loads((workspace/'runtime.json').read_text())
        return state,json.loads(runtime.docker('inspect',state['container']).stdout)[0]
    try:
        runtime.docker('build','--tag',image,str(context),timeout=300)
        check('built_controlled_cli_under_image_root_nvm_and_root_bashrc')
        cli=post('/agent-clis',dict(name='Root HOME fixture',actor='Fixture',reason='Controlled credential lookup, no model calls',set_default=True,
            content=AgentContent(serve_command='team-code serve',run_command='team-code run --attach {endpoint} --model {model} --format json',
                version_command='team-code --version',expected_version='root-home-fixture/1').model_dump()))
        project=post('/projects',dict(name='Root HOME regression',source_path=str(source),image=image))
        profile=ProfileContent(image=image,run_as_root=True,agent_cli_id=cli['id'],
            volume_mounts=[dict(source=str(credentials),target='/root/.team-login',read_only=True)],
            build_command='team-code --version',verification_command='team-code --version')
        post(f'/projects/{project["id"]}/profiles',dict(content=profile.model_dump()))
        req=post('/requirements',dict(project_id=project['id'],title='Read root-mounted credentials through HOME'))
        endpoint='/requirements/'+req['id']; action(endpoint,'prepare')
        workspace=Path(get(endpoint)['workspace']['path'])
        action(endpoint,'preflight')
        detail=get(endpoint); assert detail['preflight']['status']=='PASSED'
        attempts=list((data/'attempts').glob(f'*/{req["id"]}/*/agent-home/.local/share/opencode/root-home-serve.json'))
        assert len(attempts)==1
        readonly=json.loads(attempts[0].read_text()); assert readonly['home']=='/root'
        assert readonly['permission']['*']=='deny' and readonly['permission']['read']=='allow'
        assert readonly['controlled_credentials_readable'] is True
        assert not (workspace/'agent-home/.local/share/opencode/root-home-serve.json').exists()
        check('readonly_preflight_uses_root_credentials_and_separate_pinned_readonly_opencode_configuration')
        action(endpoint,'start'); state,detail=inspect(workspace)
        environment=dict(item.split('=',1) for item in detail['Config']['Env'] if '=' in item)
        assert environment['HOME']=='/root' and environment['OPENCODE_CONFIG']=='/home/node/.config/opencode/opencode.json'
        assert all(row['Destination']!='/root' for row in detail['Mounts'])
        runtime.docker('exec',state['container'],'/bin/bash','-c','test -r /root/.bashrc && test -x /root/.nvm/bin/team-code && test -r "$HOME/.team-login/auth.json"')
        normal=json.loads((workspace/'agent-home/.local/share/opencode/root-home-serve.json').read_text())
        assert normal['home']=='/root' and normal['permission']=='allow'
        check('normal_start_preserves_image_root_programs_and_home_based_readonly_credentials')
        action(endpoint,'task',prompt='Controlled fixture only; verify HOME, no inference')
        task=json.loads((workspace/'agent-home/.local/state/opencode/root-home-run.json').read_text())
        assert task['home']=='/root' and task['config_path']=='/home/node/.config/opencode/opencode.json'
        assert task['permission']=='allow' and task['controlled_credentials_readable']
        action(endpoint,'build'); action(endpoint,'verify')
        check('task_build_and_verification_share_root_home_and_bashrc_cannot_redirect_pinned_config')
        action(endpoint,'stop'); action(endpoint,'force_start')
        debug=get(endpoint)['debug_runtime']; assert debug['home']=='/root'
        assert (workspace/'agent-home/.local/state/opencode/root-home-run.json').exists()
        state,detail=inspect(workspace)
        runtime.docker('exec',state['container'],'/bin/bash','-c','test "$HOME" = /root && test -r "$HOME/.team-login/auth.json"')
        action(endpoint,'stop')
        check('debug_start_reports_root_home_and_opencode_state_survives_stop_and_restart')

        # An explicit credential parent owns its subtree; the managed data bind
        # must not mask an auth file in that parent even when mounted read-only.
        parent=root/'controlled-share'; (parent/'opencode').mkdir(parents=True)
        (parent/'opencode/auth.json').write_text('{"controlled":true}\n')
        runtime.start(workspace,image,run_as_root=True,start_agent=False,
            volume_mounts=[dict(source=str(parent),target='/root/.local/share',read_only=True)])
        state,detail=inspect(workspace)
        assert all(row['Destination']!='/root/.local/share/opencode' for row in detail['Mounts'])
        runtime.docker('exec',state['container'],'/bin/bash','-c','test -r "$HOME/.local/share/opencode/auth.json" && test -r "$OPENCODE_CONFIG"')
        runtime.stop(workspace)
        check('explicit_readonly_credential_ancestor_is_not_hidden_by_managed_opencode_state_bind')

        runtime.start(workspace,image,start_agent=False,run_as_root=False)
        state,detail=inspect(workspace)
        environment=dict(item.split('=',1) for item in detail['Config']['Env'] if '=' in item)
        assert environment['HOME']=='/home/node' and 'OPENCODE_CONFIG' not in environment
        assert all(not row['Destination'].startswith('/root/') for row in detail['Mounts'])
        runtime.docker('exec',state['container'],'/bin/bash','-c','test "$HOME" = /home/node')
        runtime.stop(workspace)
        assert snapshot(source)==original
        check('ordinary_mode_keeps_independent_node_home_and_source_remains_unchanged')
        report['status']='PASSED'
    except BaseException as error:
        report.update(status='FAILED',error_type=type(error).__name__,error=str(error)[:2000]); raise
    finally:
        try: resources.stop_all(data)
        finally:
            runtime.docker('image','rm',image,check=False)
            runner.client.close(); client.close(); store.engine.dispose()
            write_json(root/'report.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
