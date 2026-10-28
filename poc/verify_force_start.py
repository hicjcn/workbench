"""Real Docker, API and Runner verification of container-only force startup.

The CLI deliberately exits with a login-required error. Credential files are
controlled placeholders; no model, provider API or real user credentials run.
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
from agent_workbench.container_shell import shell_argv
from agent_workbench.profiles import Content as ProfileContent
from agent_workbench.runner import Runner
from agent_workbench.settings import Settings
from agent_workbench.store import Store, metadata
from agent_workbench.workspace import git, snapshot, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists():
        raise ValueError('Use a new dedicated verification directory')
    root.mkdir(parents=True)
    data = root / 'data'
    data.mkdir()
    # Keep the default OpenRouter model, without provisioning a model credential.
    settings = Settings(data, f'sqlite:///{data / "test.db"}',
                        secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    store = Store(settings.database_url)
    metadata.create_all(store.engine)
    client = TestClient(create_app(settings, store))
    client.headers['Authorization'] = 'Bearer ' + settings.access_token

    def dispatch(request):
        response = client.request(request.method, request.url.path,
                                  content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    runner = Runner(settings, 'http://testserver', transport=httpx.MockTransport(dispatch))
    image = 'awb-user/force-start:' + uuid.uuid4().hex[:12]
    context = root / 'context'
    context.mkdir()
    (context / 'team-code').write_text(
        '#!/bin/bash\n'
        'if [[ "$1" == --version ]]; then printf "controlled-login/1\\n"; exit 0; fi\n'
        'printf "attempted\\n" >> "$HOME/cli-attempts"\n'
        'printf "CONTROLLED_LOGIN_REQUIRED\\n" >&2\n'
        'exit 23\n', encoding='utf-8')
    (context / 'Dockerfile').write_text(
        'FROM debian:bookworm-slim\n'
        'COPY --chmod=755 team-code /usr/local/bin/team-code\n'
        'ENTRYPOINT ["/bin/bash"]\n'
        'CMD ["-c", "exec sleep infinity"]\n', encoding='utf-8')
    source = root / 'source'
    source.mkdir()
    (source / 'main.txt').write_text('original source\n', encoding='utf-8')
    for argv in [('init', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                 ('config', 'user.email', 'fixture@example.invalid'), ('add', '--all'),
                 ('-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')]:
        git(source, *argv)
    original = snapshot(source)
    credentials = root / 'controlled-credentials'
    credentials.mkdir()
    credential_file = credentials / 'auth.json'
    credential_file.write_text('{"account":"controlled-placeholder"}\n', encoding='utf-8')
    checks = []
    report = dict(status='RUNNING', real_docker=True, real_api_and_runner=True,
                  model_inference=False, real_user_credentials=False, checks=checks)

    def check(name):
        checks.append(name)
        print(name, flush=True)

    def get(path):
        response = client.get('/api/v1' + path)
        response.raise_for_status()
        return response.json()

    def post(path, body):
        response = client.post('/api/v1' + path, json=body)
        response.raise_for_status()
        return response.json()

    def action(endpoint, name, expected='SUCCEEDED', **payload):
        job = post(endpoint + '/actions', dict(action=name, **payload))
        assert runner.run_once(), 'Runner did not claim the queued action'
        completed = get('/jobs/' + job['id'])
        assert completed['status'] == expected, completed.get('error')
        return completed

    try:
        runtime.docker('build', '--tag', image, str(context), timeout=180)
        check('built_login_required_cli_with_independent_default_container_process')
        cli = post('/agent-clis', dict(name='受控登录排查 CLI', actor='Fixture',
            reason='Controlled login-required CLI, no model calls', set_default=True,
            content=AgentContent(
                serve_command='team-code serve --hostname {host} --port {port}',
                run_command='team-code run --attach {endpoint} --model {model} --format json',
                version_command='team-code --version', expected_version='controlled-login/1').model_dump()))
        project = post('/projects', dict(name='强制启动排查验收', source_path=str(source), image=image))
        post(f'/projects/{project["id"]}/profiles', dict(content=ProfileContent(
            image=image, agent_cli_id=cli['id'], run_as_root=True,
            cpus=0.5, memory_mib=512, pids_limit=128,
            mounts=[dict(source=str(credential_file), target='/run/workbench-config/auth.json')],
            volume_mounts=[dict(source=str(credentials), target='/root/.team-login', read_only=True)],
            environment={'TEAM_CONFIG_FILE': '/root/.team-login/auth.json'},
            port_mappings=[dict(container_port=8080)],
            build_command='team-code --version', verification_command='team-code --version',
        ).model_dump()))
        req = post('/requirements', dict(project_id=project['id'], title='检查登录凭据挂载'))
        endpoint = '/requirements/' + req['id']
        action(endpoint, 'prepare')
        detail = get(endpoint)
        workspace = Path(detail['workspace']['path'])
        action(endpoint, 'preflight', expected='FAILED')
        detail = get(endpoint)
        assert detail['preflight']['status'] == 'FAILED'
        assert detail['preflight']['diagnostics']['exit_code'] == 23
        assert 'CONTROLLED_LOGIN_REQUIRED' in detail['preflight']['diagnostics']['log_tail']
        failed_preflight = (workspace / 'preflight-report.json').read_bytes()
        check('real_preflight_detects_cli_login_failure_and_cleans_its_temporary_container')

        action(endpoint, 'force_start')
        detail = get(endpoint)
        assert detail['workspace']['environment'] == 'DEBUG_RUNNING'
        assert (workspace / 'preflight-report.json').read_bytes() == failed_preflight
        debug = detail['debug_runtime']
        state = json.loads((workspace / 'runtime.json').read_text(encoding='utf-8'))
        container = state['container']
        assert debug['container'] == container
        assert debug['shell_command'] == f'docker exec -it {container} /bin/bash'
        assert debug['serve_command'] == 'team-code serve --hostname 0.0.0.0 --port 4096'
        assert 'password' not in debug and state['password'] not in json.dumps(detail)
        assert not (workspace / 'agent-home/cli-attempts').exists()
        inspected = json.loads(runtime.docker('inspect', container).stdout)[0]
        assert inspected['State']['Running'] is True
        assert inspected['Config']['Entrypoint'] == ['/bin/bash']
        assert inspected['Config']['Cmd'] == ['-c', 'exec sleep infinity']
        assert inspected['Image'] == json.loads((workspace / 'workspace.json').read_text())['runtime_profile']['image_id']
        assert inspected['Config']['User'].startswith('0:')
        assert inspected['HostConfig']['ReadonlyRootfs'] is True
        assert inspected['HostConfig']['NanoCpus'] == 500000000
        assert inspected['HostConfig']['Memory'] == 512 * 1024 ** 2
        assert inspected['HostConfig']['PidsLimit'] == 128
        assert any(row['Destination'] == '/run/workbench-config/auth.json' and not row['RW'] for row in inspected['Mounts'])
        assert any(row['Destination'] == '/root/.team-login' and not row['RW'] for row in inspected['Mounts'])
        assert inspected['NetworkSettings']['Ports']['8080/tcp'][0]['HostIp'] == '127.0.0.1'
        runtime.docker('exec', container, '/bin/bash', '-c',
            'test "$HOME" = /home/node && test "$TEAM_CONFIG_FILE" = /root/.team-login/auth.json '
            '&& test -r "$TEAM_CONFIG_FILE" && test -r /run/workbench-config/auth.json')
        check('force_start_keeps_default_process_fixed_image_resources_user_mounts_environment_and_ports_without_starting_cli')
        check('force_start_preserves_failed_preflight_and_does_not_require_model_readiness_or_expose_runtime_password')

        for name in ('start', 'task', 'build', 'verify', 'preflight'):
            action(endpoint, name, expected='FAILED', **({'prompt': 'controlled no-op'} if name == 'task' else {}))
            assert get(endpoint)['workspace']['environment'] == 'DEBUG_RUNNING'
            assert json.loads(runtime.docker('inspect', container).stdout)[0]['State']['Running'] is True
        check('normal_start_model_tasks_build_verification_and_preflight_stay_blocked_while_debug_container_is_active')

        manual = runtime.docker('exec', container, *shell_argv(
            ['team-code', 'serve', '--hostname', '0.0.0.0', '--port', '4096'], root=True), check=False)
        assert manual.returncode == 23 and 'CONTROLLED_LOGIN_REQUIRED' in manual.stderr
        assert (workspace / 'agent-home/cli-attempts').exists()
        assert json.loads(runtime.docker('inspect', container).stdout)[0]['State']['Running'] is True
        check('manual_cli_reproduces_login_error_without_stopping_debug_container')

        action(endpoint, 'stop')
        detail = get(endpoint)
        assert detail['workspace']['environment'] == 'STOPPED' and detail['debug_runtime'] is None
        assert not (workspace / 'runtime.json').exists()
        assert runtime.docker('inspect', container, check=False).returncode != 0
        assert (workspace / 'agent-home/cli-attempts').exists()
        assert (workspace / 'preflight-report.json').read_bytes() == failed_preflight
        assert snapshot(source) == original
        action(endpoint, 'start', expected='FAILED')
        assert not (workspace / 'runtime.json').exists()
        check('stop_removes_container_preserves_agent_state_source_and_failed_preflight_and_normal_start_still_requires_preflight')
        report['status'] = 'PASSED'
    except BaseException as error:
        report.update(status='FAILED', error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        try:
            resources.stop_all(data)
        finally:
            runtime.docker('image', 'rm', image, check=False)
            runner.client.close()
            client.close()
            store.engine.dispose()
            write_json(root / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
