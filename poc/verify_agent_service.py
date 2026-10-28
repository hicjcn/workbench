"""Real Docker checks for a configured Agent CLI beside an image's default service.

The default HTTP process is a controlled editor fixture, not code-server itself.
The Agent HTTP fixture uses no model and makes no external provider requests.
"""
import argparse
import json
from pathlib import Path
import shutil
import socket
import time
from urllib.request import ProxyHandler, build_opener
import uuid

from agent_workbench import agent_clis, runtime
from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot, write_json


EDITOR_SCRIPT = """const http = require('node:http');
http.createServer((request, response) => {
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({service:'editor-fixture', healthy:true}));
}).listen(8080, '0.0.0.0');
"""


def free_port():
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        return reserved.getsockname()[1]


def selected_cli(executable, verb='serve'):
    content = agent_clis.Content(
        serve_command=f'{executable} {verb} --hostname {{host}} --port {{port}}',
        run_command=f'{executable} run --attach {{endpoint}} --model {{model}} --format json',
        version_command=f'{executable} --version',
    ).model_dump()
    return dict(id='cli_' + uuid.uuid4().hex, name='Controlled ' + executable,
                number=1, content=content, content_digest=agent_clis.digest(content))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists(): raise ValueError('Use a new dedicated verification directory')
    root.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[1]
    image = 'awb-user/agent-service:' + uuid.uuid4().hex[:12]
    context = root / 'context'
    context.mkdir()
    fixture = repository / 'poc/preflight-wrapper-fixture.cjs'
    for executable in ('team-code', 'other-code'):
        shutil.copyfile(fixture, context / executable)
    (context / 'editor.cjs').write_text(EDITOR_SCRIPT, encoding='utf-8')
    (context / 'Dockerfile').write_text(
        'FROM node:22-bookworm-slim\n'
        'COPY --chmod=755 team-code other-code /usr/local/bin/\n'
        'COPY --chmod=644 editor.cjs /opt/editor.cjs\n'
        'ENTRYPOINT ["/usr/local/bin/node"]\n'
        'CMD ["/opt/editor.cjs"]\n', encoding='utf-8')
    source = root / 'source'
    source.mkdir()
    (source / 'main.txt').write_text('original source\n', encoding='utf-8')
    for argv in [('init', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                 ('config', 'user.email', 'fixture@example.invalid'), ('add', '--all'),
                 ('-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')]:
        git(source, *argv)
    original = snapshot(source)
    workspaces, checks = [], []
    report = dict(status='RUNNING', real_docker=True, model_inference=False,
                  actual_code_server_verified=False, checks=checks)
    opener = build_opener(ProxyHandler({}))

    def check(name):
        checks.append(name)
        print(name, flush=True)

    def start(executable, verb='serve'):
        cli = selected_cli(executable, verb)
        created = create_workspace(source, root / 'workspaces', 'req_' + uuid.uuid4().hex[:12], 'project')
        workspace = Path(created['workspace'])
        workspaces.append(workspace)
        agent_clis.configure(workspace, cli)
        editor_port = free_port()
        runtime.start(workspace, image, port_mappings=[dict(container_port=8080, host_port=editor_port, protocol='tcp')])
        state = json.loads((workspace / 'runtime.json').read_text(encoding='utf-8'))
        deadline = time.monotonic() + 10
        while True:
            try:
                with opener.open(f'http://127.0.0.1:{editor_port}', timeout=1) as response:
                    assert json.load(response) == dict(service='editor-fixture', healthy=True)
                break
            except OSError:
                if time.monotonic() >= deadline: raise
                time.sleep(.1)
        inspected = json.loads(runtime.docker('inspect', state['container']).stdout)[0]
        assert inspected['Config']['Entrypoint'] == ['/usr/local/bin/node']
        assert inspected['Config']['Cmd'] == ['/opt/editor.cjs']
        assert inspected['State']['Status'] == 'running'
        assert state['serve_argv'][0] == executable
        assert state['agent_cli']['id'] == cli['id']
        return workspace, state

    def wait_agent(state, expected):
        deadline = time.monotonic() + 10
        while True:
            observed = runtime.service_status(state, logs=True)
            if observed['agent_status'] == expected: return observed
            if time.monotonic() >= deadline: raise AssertionError(observed)
            time.sleep(.1)

    def ready(workspace):
        deadline = time.monotonic() + 10
        while True:
            try:
                capabilities = runtime.probe(workspace)
                assert capabilities['health']['healthy']
                return
            except WorkbenchError:
                if time.monotonic() >= deadline: raise
                time.sleep(.1)

    def assert_probe_rejected(workspace, state, exit_code):
        observed = wait_agent(state, 'exited')
        assert observed['agent_exit_code'] == exit_code, observed
        assert json.loads(runtime.docker('inspect', state['container']).stdout)[0]['State']['Status'] == 'running'
        started = time.monotonic()
        try:
            runtime.probe(workspace)
        except WorkbenchError as error:
            assert str(exit_code) in str(error), str(error)
        else:
            raise AssertionError('Exited Agent CLI was accepted because editor is still running')
        assert time.monotonic() - started < 5
        return observed

    try:
        runtime.docker('build', '--tag', image, str(context), timeout=180)
        check('built_single_image_with_editor_entrypoint_and_two_unstarted_agent_clis')
        for executable in ('team-code', 'other-code'):
            workspace, state = start(executable)
            wait_agent(state, 'running')
            ready(workspace)
            check(executable + '_starts_from_pinned_configuration_beside_preserved_editor')
            runtime.stop(workspace)
            assert runtime.docker('inspect', state['container'], check=False).returncode != 0
        workspace, state = start('other-code', 'fail-serve')
        failure = assert_probe_rejected(workspace, state, 7)
        assert 'CONTROLLED_WRAPPER_STARTUP_FAILED' in failure['log_tail']
        check('agent_exit_7_is_reported_with_log_while_editor_remains_running')
        runtime.stop(workspace)
        workspace, state = start('missing-agent-fixture')
        failure = assert_probe_rejected(workspace, state, 127)
        assert 'missing-agent-fixture' in failure['log_tail']
        check('missing_configured_agent_fails_without_falling_back_to_editor_or_another_cli')
        runtime.stop(workspace)
        assert snapshot(source) == original
        check('stop_removes_container_and_preserves_original_source')
        report['status'] = 'PASSED'
    except BaseException as error:
        report.update(status='FAILED', error_type=type(error).__name__, error=str(error)[:3000])
        raise
    finally:
        for workspace in reversed(workspaces):
            if (workspace / 'runtime.json').exists(): runtime.stop(workspace)
        runtime.docker('image', 'rm', image, check=False)
        write_json(root / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
