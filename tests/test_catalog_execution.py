"""CLI-discovered model IDs execute with the pinned round's model configuration."""
import io
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from agent_workbench import runtime
from agent_workbench.execution import Unknown
from agent_workbench.workflow_execution import ensure_development_runtime, execute_step, readonly_agent
from agent_workbench.workspace import WorkbenchError, snapshot, write_json
from test_control_plane import ControlFixture


class CatalogModelConfigurationTests(unittest.TestCase):
    def test_selected_catalog_provider_model_retains_whitelist_and_readonly_permissions(self):
        model = 'company/vendor/model:latest'
        config = runtime.model_config(model, True, agent_cli_model=True)
        self.assertEqual(config['model'], model)
        self.assertEqual(config['small_model'], model)
        self.assertEqual(config['enabled_providers'], ['company'])
        self.assertEqual(config['provider'], {'company': {'whitelist': ['vendor/model:latest']}})
        self.assertEqual(config['permission']['*'], 'deny')
        self.assertEqual(config['permission']['read'], 'allow')
        self.assertEqual(config['plugin'], ['/opt/superpowers'])

    def test_legacy_verification_remains_limited_to_free_models(self):
        for model in ('company/model', 'opencode/paid-model', 'openrouter/vendor/paid-model'):
            with self.subTest(model=model), self.assertRaises(WorkbenchError):
                runtime.model_config(model)

    def test_selected_model_requires_a_bounded_literal_provider_model_id(self):
        for model in ('model-only', '/model', 'provider/', 'p/model\nother', 'p/model x', '-p/model', 'p/$(cmd)', 'p/' + 'x' * 240):
            with self.subTest(model=model), self.assertRaises(WorkbenchError):
                runtime.model_config(model, agent_cli_model=True)


class CatalogExecutionTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.req = self.prepared()
        self.workspace = Path(self.req['workspace']['path'])
        self.model = 'company/selected-model'
        self.events = []
        self.emit = lambda key, payload: self.events.append((key, payload))
        self.canceled = threading.Event()

    def docker(self, *args, **_kwargs):
        if args[:2] == ('network', 'inspect'):
            return subprocess.CompletedProcess([], 1, '', 'No such network')
        return subprocess.CompletedProcess([], 0, '127.0.0.1:4096\n' if args[0] == 'port' else '', '')

    def job(self, step='development', *, material=False):
        config = {'development_model': self.model, 'review_models': ['company/review-one', 'company/review-two'], 'model_source': 'agent_cli'}
        bundle = {'code_digest': snapshot(self.workspace / 'repo')['tree_digest'], 'config': config}
        if material:
            bundle.pop('config')
            bundle['model_source'] = 'agent_cli'
        return {'id': 'job-controlled-catalog', 'requirement_id': self.req['id'], 'project': self.req['project'],
            'input': {'step_key': step, 'request': {'prompt': 'controlled input'}, 'bundle': bundle}}

    def test_runtime_pins_catalog_model_without_legacy_provider_credentials(self):
        with patch('agent_workbench.runtime.docker', side_effect=self.docker), patch.dict('os.environ', {'OPENROUTER_API_KEY': ''}):
            runtime.start(self.workspace, 'fixture:local', agent_model='openrouter/vendor/paid-model', read_only_source=True)
        state = json.loads((self.workspace / 'runtime.json').read_text())
        self.assertEqual(state['model_source'], 'agent_cli')
        self.assertEqual(state['model'], 'openrouter/vendor/paid-model')
        config = json.loads((self.workspace / 'agent-home/.config/opencode/opencode.json').read_text())
        self.assertEqual(config, runtime.model_config(state['model'], True, agent_cli_model=True))

    def test_runtime_rejects_mixed_catalog_and_legacy_model_options_before_docker(self):
        with patch('agent_workbench.runtime.docker') as docker:
            with self.assertRaisesRegex(WorkbenchError, 'only one'):
                runtime.start(self.workspace, 'fixture:local', agent_model=self.model, opencode_model=self.settings.model)
        docker.assert_not_called()

    def test_development_switches_existing_environment_before_selected_model_task(self):
        write_json(self.workspace / 'runtime.json', {'phase': 'STARTED', 'container': 'old-fixture'})
        config_file = self.workspace / 'agent-home/.config/opencode/opencode.json'
        def stop(workspace):
            (workspace / 'runtime.json').unlink()
        def start(workspace, image, ca, **options):
            self.assertEqual(options, {'agent_model': self.model})
            write_json(workspace / 'runtime.json', {'phase': 'STARTED', 'container': 'selected-fixture', 'model_source': 'agent_cli', 'model': self.model})
            write_json(config_file, runtime.model_config(self.model, agent_cli_model=True))
        def task(job, workspace, emit, canceled, **options):
            self.assertEqual(options['model'], self.model)
            self.assertEqual(json.loads(config_file.read_text())['model'], self.model)
            return {'agent_text': '{"summary":"controlled development","verification_notes":"run separately"}'}
        with patch('agent_workbench.runtime.stop', side_effect=stop) as stopped, \
                patch.object(self.runner.executor, 'start_runtime', side_effect=start) as started, \
                patch('agent_workbench.runtime.probe', return_value={'health': {'healthy': True}}), \
                patch.object(self.runner.executor, 'task', side_effect=task):
            result = execute_step(self.runner.executor, self.job(), self.workspace, self.emit, self.canceled)
        stopped.assert_called_once_with(self.workspace)
        started.assert_called_once()
        self.assertEqual(result['workflow_output']['summary'], 'controlled development')

    def test_matching_selected_runtime_is_reused_and_missing_or_debug_runtime_is_rejected(self):
        write_json(self.workspace / 'runtime.json', {'phase': 'STARTED', 'model_source': 'agent_cli', 'model': self.model})
        with patch('agent_workbench.runtime.stop') as stop, patch.object(self.runner.executor, 'start_runtime') as start:
            ensure_development_runtime(self.runner.executor, self.job(), self.workspace, self.emit, self.canceled, self.model)
        stop.assert_not_called()
        start.assert_not_called()
        for phase in (None, 'DEBUG_RUNNING', 'NEEDS_RECONCILIATION'):
            with self.subTest(phase=phase):
                if phase is None: (self.workspace / 'runtime.json').unlink()
                else: write_json(self.workspace / 'runtime.json', {'phase': phase})
                with patch('agent_workbench.runtime.stop') as stop:
                    with self.assertRaises(WorkbenchError):
                        ensure_development_runtime(self.runner.executor, self.job(), self.workspace, self.emit, self.canceled, self.model)
                stop.assert_not_called()

    def test_unverified_old_environment_stop_blocks_model_switch(self):
        write_json(self.workspace / 'runtime.json', {'phase': 'STARTED'})
        with patch('agent_workbench.runtime.stop', side_effect=WorkbenchError('unverified')), \
                patch.object(self.runner.executor, 'start_runtime') as start:
            with self.assertRaises(Unknown):
                ensure_development_runtime(self.runner.executor, self.job(), self.workspace, self.emit, self.canceled, self.model)
        start.assert_not_called()

    def test_readonly_workflow_and_material_models_use_catalog_source_and_separate_state(self):
        homes = []
        def start(workspace, image, ca, **options):
            self.assertEqual(options, {'read_only_source': True, 'agent_model': self.model})
            homes.append(workspace / 'agent-home')
            write_json(workspace / 'runtime.json', {})
        def stop(workspace):
            (workspace / 'runtime.json').unlink()
        for material in (False, True):
            job = self.job('analysis', material=material)
            job['id'] += '-material' if material else '-workflow'
            with patch.object(self.runner.executor, 'start_runtime', side_effect=start), \
                    patch('agent_workbench.runtime.probe', return_value={'health': {'healthy': True}}), \
                    patch.object(self.runner.executor, 'task', return_value={'agent_text': 'controlled report'}), \
                    patch('agent_workbench.runtime.stop', side_effect=stop):
                result = readonly_agent(self.runner.executor, job, self.workspace, self.emit, self.canceled, self.model, 'controlled', lambda value: {'summary': value})
            self.assertEqual(result['model'], self.model)
        self.assertNotEqual(homes[0], homes[1])
        self.assertNotEqual(homes[0], self.workspace / 'agent-home')

    def test_selected_model_is_passed_to_real_task_command_without_public_catalog_lookup(self):
        model = 'openrouter/vendor/selected-paid-model'
        write_json(self.workspace / 'runtime.json', {'container': 'fixture', 'model_source': 'agent_cli', 'model': model})
        write_json(self.workspace / 'agent-home/.config/opencode/opencode.json', runtime.model_config(model, agent_cli_model=True))
        sink = io.StringIO()
        sink.close = lambda: None
        stream = '\n'.join(json.dumps(value) for value in [
            {'type': 'text', 'part': {'text': 'controlled response'}}, {'type': 'step_finish', 'part': {'reason': 'stop'}}]) + '\n'
        process = Mock(stdin=sink, stdout=io.StringIO(stream), stderr=io.StringIO(''), returncode=0)
        process.poll.return_value = 0
        with patch('agent_workbench.execution.subprocess.Popen', return_value=process) as started, \
                patch('agent_workbench.execution.openrouter_catalog_model') as public_catalog, \
                patch.object(self.runner.executor, 'observe', return_value={}):
            result = self.runner.executor.task({}, self.workspace, self.emit, self.canceled, model=model, prompt='controlled input')
        argv = started.call_args.args[0]
        self.assertEqual(argv[argv.index('--model') + 1], model)
        self.assertEqual(result['model'], model)
        public_catalog.assert_not_called()
