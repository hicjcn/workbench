"""Configured wrapper preflight; no model calls or Docker daemon in these tests."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_workbench import preflight
from agent_workbench.agent_clis import Content as AgentContent
from agent_workbench.profiles import Content as ProfileContent
from agent_workbench.workspace import WorkbenchError, snapshot, write_json
from test_control_plane import ControlFixture


class PreflightTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.cli = self.client.post('/api/v1/agent-clis', json=dict(name='团队封装 CLI', actor='Fixture', reason='configured preflight', set_default=True,
            content=AgentContent(serve_command='team-code serve --hostname {host} --port {port}',
                run_command='team-code run --attach {endpoint} --model {model} --format json', version_command='team-code version', expected_version='team/1.0', credential_env=['TEAM_API_KEY']).model_dump())).json()
        project = self.project()
        self.client.post(f"/api/v1/projects/{project['id']}/profiles", json=dict(content=ProfileContent(image='wrapper:fixture').model_dump()))
        req = self.requirement(project)
        self.action(req, 'prepare')
        with patch('agent_workbench.runtime.docker', return_value=SimpleNamespace(stdout='[{"Id":"sha256:wrapper"}]')):
            self.runner.run_once()
        self.req = self.detail(req); self.workspace = Path(self.req['workspace']['path'])
        self.original = snapshot(self.workspace / 'repo')
        self.isolated = None

    def run_preflight(self, *, version='team/1.0', probe_failure=False, exited=False, start_failure=False, inspection_failure=False,
                      root_mode=False, implicit_database_credentials=False):
        def start(isolated, image, *_args, **options):
            self.isolated = isolated
            manifest = json.loads((isolated / 'workspace.json').read_text())
            self.assertEqual(manifest['agent_cli']['id'], self.cli['id'])
            self.assertTrue(options['read_only_source'])
            write_json(isolated / 'runtime.json', {'container':'fixture-container', 'phase':'STARTED',
                'run_as_root':root_mode, 'serve_argv':['team-code','serve']})
            if start_failure: raise WorkbenchError('exec: "team-code": executable file not found in $PATH TEAM-PRIVATE-MARKER')
        def docker(*args, **_options):
            if args[0] == 'inspect':
                if inspection_failure: raise WorkbenchError('inspection failed TEAM-PRIVATE-MARKER')
                return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps([{'Image':'sha256:wrapper',
                    'Path':'team-code','Config':{'Labels':{'org.agent-workbench.superpowers-commit':preflight.SUPERPOWERS},
                        'Env':['PATH=/opt/team/bin:/usr/bin','HOME=/home/node','TEAM_API_KEY=TEAM-PRIVATE-MARKER','UNRELATED=DO-NOT-COPY-THIS'], 'User':'1000:1000'},
                    'NetworkSettings':{'Networks':{}}, 'State':{'Status':'exited' if exited else 'running','ExitCode':2 if exited else 0}}]))
            if args[0] == 'logs':
                return SimpleNamespace(stdout='wrapper startup fixture TEAM-PRIVATE-MARKER',stderr='')
            self.assertEqual(args[:3], ('exec','fixture-container','/bin/bash'))
            self.assertEqual(args[-4:-2], ('node','-e'))
            config = json.loads(args[-1])
            self.assertEqual(config['version_argv'], ['team-code','version'])
            from agent_workbench.container_shell import shell_argv
            self.assertEqual(list(args[2:]), shell_argv(['node','-e',preflight.CHECK_SCRIPT,args[-1]], root=root_mode))
            return SimpleNamespace(stdout=json.dumps({'node':{'ok':True},'git':{'ok':True},'agent_cli':{'ok':True,'value':version},'commands':[],'mounts':[], 'no_database_credentials':not implicit_database_credentials}))
        def stop(isolated):
            (isolated / 'runtime.json').unlink()
        capabilities = {'health':{'healthy':True},'superpowers_configured':True,'skill_names':['using-superpowers'],'runtime':{'source_read_only':True}}
        self.action(self.req,'preflight')
        fake_time = SimpleNamespace(time=lambda:123.0, monotonic=Mock(side_effect=[0,1] if probe_failure=='retry' else [0,100]), sleep=Mock())
        probe_effect = [WorkbenchError('control not ready'),capabilities] if probe_failure=='retry' else WorkbenchError('control not ready') if probe_failure else None
        with patch.dict('os.environ', {'TEAM_API_KEY':'TEAM-PRIVATE-MARKER'}), patch.object(self.runner.executor,'start_runtime',side_effect=start), \
             patch('agent_workbench.runtime.probe',side_effect=probe_effect, return_value=capabilities), \
             patch('agent_workbench.runtime.docker',side_effect=docker), patch('agent_workbench.runtime.stop',side_effect=stop), patch.object(preflight,'time',fake_time):
            self.runner.run_once()
        detail = self.detail(self.req)
        self.assertEqual(snapshot(self.workspace / 'repo'), self.original)
        self.assertFalse((self.isolated / 'runtime.json').exists())
        return detail

    def test_preflight_uses_pinned_wrapper_commands_and_reports_neutral_checks(self):
        detail = self.run_preflight()
        self.assertEqual(detail['jobs'][0]['status'],'SUCCEEDED')
        report = detail['preflight']
        self.assertEqual(report['agent_cli']['id'], self.cli['id'])
        self.assertEqual(report['commands']['serve'][0], 'team-code')
        self.assertEqual(report['commands']['version'], ['team-code','version'])
        self.assertEqual(report['tools']['agent_cli']['value'],'team/1.0')
        names = {item['name'] for item in report['checks']}
        self.assertTrue({'agent_cli_version','agent_cli_service'} <= names)
        self.assertNotIn('opencode_version', names)

    def test_wrapper_version_mismatch_still_blocks_preflight(self):
        detail = self.run_preflight(version='wrong/2.0')
        self.assertEqual(detail['jobs'][0]['status'],'FAILED')
        self.assertFalse(next(item for item in detail['preflight']['checks'] if item['name']=='agent_cli_version')['passed'])

    def test_root_preflight_reloads_bash_environment_for_its_node_probe(self):
        detail = self.run_preflight(root_mode=True)
        self.assertEqual(detail['jobs'][0]['status'],'SUCCEEDED')

    def test_credentials_exported_by_startup_file_block_readonly_preflight(self):
        detail = self.run_preflight(root_mode=True, implicit_database_credentials=True)
        self.assertEqual(detail['jobs'][0]['status'],'FAILED')
        self.assertFalse(next(item for item in detail['preflight']['checks'] if item['name']=='no_database_access')['passed'])

    def test_recovered_startup_does_not_leave_failure_diagnostics_in_passed_report(self):
        detail = self.run_preflight(probe_failure='retry')
        self.assertEqual(detail['preflight']['status'],'PASSED')
        self.assertNotIn('diagnostics',detail['preflight'])

    def test_exited_wrapper_reports_selected_cli_command_and_redacted_logs_without_waiting(self):
        detail = self.run_preflight(probe_failure=True,exited=True)
        self.assertEqual(detail['jobs'][0]['status'],'FAILED')
        self.assertIn('团队封装 CLI', detail['jobs'][0]['error'])
        self.assertIn('team-code serve', detail['jobs'][0]['error'])
        report = detail['preflight']
        self.assertEqual(report['diagnostics']['exit_code'],2)
        self.assertIn('[REDACTED]', report['diagnostics']['log_tail'])
        self.assertNotIn('TEAM-PRIVATE-MARKER', json.dumps(report))

    def test_unready_wrapper_records_configured_commands_and_timeout_diagnostics(self):
        detail = self.run_preflight(probe_failure=True)
        self.assertEqual(detail['jobs'][0]['status'],'FAILED')
        self.assertIn('团队封装 CLI', detail['preflight']['error'])
        self.assertIn('team-code serve', detail['preflight']['error'])
        self.assertEqual(detail['preflight']['diagnostics']['control_error'],'control not ready')

    def test_missing_executable_has_diagnostics_even_when_runtime_never_starts(self):
        detail = self.run_preflight(start_failure=True)
        self.assertEqual(detail['jobs'][0]['status'],'FAILED')
        report = detail['preflight']; diagnostic = report['diagnostics']
        self.assertEqual(report['checks'],[{'name':'agent_cli_service','passed':False}])
        self.assertEqual(diagnostic['container_path'],'/opt/team/bin:/usr/bin')
        self.assertEqual(diagnostic['container_home'],'/home/node')
        self.assertEqual(diagnostic['container_user'],'1000:1000')
        self.assertEqual(diagnostic['executable'],'team-code')
        self.assertIn('executable file not found',diagnostic['launch_error'])
        self.assertNotIn('TEAM-PRIVATE-MARKER',json.dumps(report))
        self.assertNotIn('DO-NOT-COPY-THIS',json.dumps(report))

    def test_diagnostic_error_does_not_replace_start_failure_or_prevent_cleanup(self):
        detail = self.run_preflight(start_failure=True,inspection_failure=True)
        report = detail['preflight']
        self.assertEqual(detail['jobs'][0]['status'],'FAILED')
        self.assertIn('executable file not found',report['error'])
        self.assertIn('inspection failed',report['diagnostics']['inspection_error'])
        self.assertNotIn('TEAM-PRIVATE-MARKER',json.dumps(report))

    def test_version_script_accepts_wrapper_stderr_and_does_not_fall_back_to_opencode(self):
        base = {'commands':{},'mounts':[]}
        version = [sys.executable,'-c',"import sys; sys.stderr.write('wrapper-stderr/1.0\\n')"]
        result = subprocess.run(['node','-e',preflight.CHECK_SCRIPT,json.dumps({**base,'version_argv':version})],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['agent_cli']['value'],'wrapper-stderr/1.0')
        result = subprocess.run(['node','-e',preflight.CHECK_SCRIPT,json.dumps({**base,'version_argv':['missing-wrapper-fixture']})],capture_output=True,text=True,timeout=30)
        tool = json.loads(result.stdout)['agent_cli']
        self.assertFalse(tool['ok']); self.assertEqual(tool['error'],'ENOENT')
        result = subprocess.run(['node','-e',preflight.CHECK_SCRIPT,json.dumps(base)],capture_output=True,text=True,timeout=30)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Missing configured Agent CLI version command',result.stderr)
