import io
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from agent_workbench import logging_support as logs
from agent_workbench.workspace import WorkbenchError
from test_control_plane import ControlFixture


class CaptureLogs:
    def capture(self, mode="concise"):
        saved = [(logging.getLogger(name), list(logging.getLogger(name).handlers), logging.getLogger(name).level,
            logging.getLogger(name).propagate) for name in ("agent_workbench", "uvicorn", "uvicorn.error")]
        previous_secrets = logs._secrets
        def restore():
            for logger, handlers, level, propagate in saved:
                logger.handlers[:] = handlers; logger.setLevel(level); logger.propagate = propagate
            logs._secrets = previous_secrets
        self.addCleanup(restore)
        output = io.StringIO()
        logs.configure(mode, stream=output, secrets=("CONFIGURED-PRIVATE-TOKEN",))
        return output


class LoggingTests(CaptureLogs, TestCase):
    def test_modes_filter_details_and_reconfiguration_does_not_duplicate_lines(self):
        output = self.capture()
        logs.event('fixture','start','简要进度')
        logs.event('fixture','detail','内部步骤',detailed=True)
        self.assertIn('简要进度',output.getvalue()); self.assertNotIn('内部步骤',output.getvalue())
        self.assertRegex(output.getvalue(),r'^\d{4}-\d\d-\d\dT.*Z INFO \[agent_workbench.fixture\]')
        logs.configure('detailed',stream=output)
        logs.configure('detailed',stream=output)
        logs.event('fixture','detail','内部步骤',detailed=True)
        self.assertEqual(output.getvalue().count('内部步骤'),1)
        self.assertIn('DEBUG',output.getvalue())

    def test_known_secrets_and_control_characters_are_removed_and_unknown_fields_are_ignored(self):
        output = self.capture('detailed')
        with patch.dict(os.environ, {'TEAM_API_KEY':'ENVIRONMENT-PRIVATE-TOKEN'}):
            logs.event('fixture','redaction','CONFIGURED-PRIVATE-TOKEN\nENVIRONMENT-PRIVATE-TOKEN\x1b[31m',
                job_id='job_fixture', password='NEVER-LOG-THIS')
        value=output.getvalue()
        for secret in ('CONFIGURED-PRIVATE-TOKEN','ENVIRONMENT-PRIVATE-TOKEN','NEVER-LOG-THIS'):
            self.assertNotIn(secret,value)
        self.assertIn('[REDACTED]',value); self.assertIn('\\n',value); self.assertNotIn('\x1b',value)
        self.assertEqual(len(value.splitlines()),1)

    def test_task_context_is_isolated_between_threads_and_reset_after_scope(self):
        output = self.capture('detailed'); barrier=threading.Barrier(2)
        def worker(identity):
            with logs.context(job_id=identity,requirement_id='req_'+identity):
                barrier.wait(timeout=5)
                logs.event('fixture','work','parallel '+identity)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(worker,identity) for identity in ('first','second')]
            for future in futures: future.result(timeout=10)
        logs.event('fixture','outside','no context')
        for line in output.getvalue().splitlines():
            if 'parallel first' in line:self.assertIn('job_id="first"',line);self.assertNotIn('second',line)
            if 'parallel second' in line:self.assertIn('job_id="second"',line);self.assertNotIn('first',line)
            if 'no context' in line:self.assertNotIn('job_id=',line)

    def test_operation_failure_records_stage_duration_and_locations_without_exception_values(self):
        output=self.capture('detailed')
        with self.assertRaises(RuntimeError):
            with logs.operation('fixture','copy','复制副本'):
                raise RuntimeError('UNREGISTERED-EXCEPTION-PRIVATE-VALUE')
        value=output.getvalue()
        self.assertIn('duration_ms=',value);self.assertIn('RuntimeError',value);self.assertIn('test_logging_support.py',value)
        self.assertNotIn('UNREGISTERED-EXCEPTION-PRIVATE-VALUE',value)


class ProgressTests(CaptureLogs, ControlFixture):
    def test_http_logs_use_route_templates_without_query_body_headers_or_unmatched_path(self):
        output=self.capture('detailed')
        response=self.client.get('/api/v1/requirements/PRIVATE-PATH?token=PRIVATE-QUERY')
        self.assertEqual(response.status_code,404)
        self.assertRegex(response.headers['X-Request-ID'],r'^[a-f0-9]{16}$')
        self.client.post('/api/v1/session',json={'token':'PRIVATE-BODY'})
        self.client.get('/PRIVATE-UNMATCHED?key=PRIVATE-QUERY')
        value=output.getvalue()
        self.assertIn('/api/v1/requirements/{requirement_id}',value)
        self.assertIn('http_status="404"',value)
        for secret in ('PRIVATE-PATH','PRIVATE-QUERY','PRIVATE-BODY','PRIVATE-UNMATCHED',self.settings.access_token,self.settings.runner_token):
            self.assertNotIn(secret,value)

    def test_concise_http_polling_is_quiet_but_failures_are_visible(self):
        output=self.capture()
        self.client.get('/api/v1/config')
        self.assertEqual(output.getvalue(),'')
        self.client.get('/api/v1/requirements/missing')
        self.assertIn('WARNING',output.getvalue())

    def run_prepare(self, mode):
        self.runner.settings=replace(self.settings,log_mode=mode)
        output=self.capture(mode)
        req=self.requirement(); job=self.action(req,'prepare').json()
        self.runner.run_once()
        self.assertEqual(self.detail(req)['jobs'][0]['status'],'SUCCEEDED')
        events=self.client.get(f"/api/v1/jobs/{job['id']}/events").json()
        self.assertEqual([item['seq'] for item in events],list(range(len(events))))
        self.assertIn(job['id'],output.getvalue());self.assertIn(req['id'],output.getvalue())
        self.assertIn('step="job.finished"',output.getvalue())
        return events,output

    def test_concise_task_retains_key_progress_and_excludes_detail_events(self):
        events,output=self.run_prepare('concise')
        steps=[item['payload'].get('step') for item in events]
        self.assertIn('workspace.copy',steps);self.assertIn('workspace.snapshot',steps)
        self.assertNotIn('workspace.profile',steps);self.assertNotIn('DEBUG',output.getvalue())

    def test_detailed_task_records_internal_steps_and_copy_progress(self):
        events,output=self.run_prepare('detailed')
        self.assertTrue(any(item['payload'].get('level')=='DEBUG' for item in events))
        self.assertIn('step="source.archive"',output.getvalue())
        self.assertIn('step="workspace.profile"',output.getvalue())

    def test_output_stays_in_task_events_and_is_not_repeated_in_host_logs(self):
        output=self.capture('detailed');self.runner.settings=replace(self.settings,log_mode='detailed')
        req=self.requirement();job=self.action(req,'prepare').json()
        original=self.runner.executor.execute
        def execute(job,emit,canceled):
            emit('agent',{'text':'PRIVATE-MODEL-OUTPUT'})
            return original(job,emit,canceled)
        with patch.object(self.runner.executor,'execute',side_effect=execute):self.runner.run_once()
        self.assertNotIn('PRIVATE-MODEL-OUTPUT',output.getvalue())
        events=self.client.get(f"/api/v1/jobs/{job['id']}/events").json()
        self.assertTrue(any(item['payload'].get('text')=='PRIVATE-MODEL-OUTPUT' for item in events))

    def test_failed_job_logs_location_and_status_without_exception_value(self):
        output=self.capture('detailed');self.runner.settings=replace(self.settings,log_mode='detailed')
        req=self.requirement();self.action(req,'prepare')
        with patch.object(self.runner.executor,'execute',side_effect=WorkbenchError('PRIVATE-FAILURE-TEXT')):self.runner.run_once()
        self.assertIn('status="FAILED"',output.getvalue());self.assertIn('job.failure_location',output.getvalue())
        self.assertNotIn('PRIVATE-FAILURE-TEXT',output.getvalue())
