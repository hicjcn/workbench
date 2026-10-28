import io
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock, patch

from sqlalchemy import text, update
from test_control_plane import ControlFixture
from agent_workbench import agent_clis, runtime
from agent_workbench.agent_clis import Content, builtin
from agent_workbench.maintenance import backup, restore
from agent_workbench.store import Store, agent_cli_configs, workspace_agents, workspaces
from agent_workbench.workspace import WorkbenchError, write_json


class AgentCliTests(ControlFixture):
    def test_opencode_official_schema_metadata_does_not_disable_model_permission_guards(self):
        expected = runtime.model_config(self.settings.model, True)
        self.assertTrue(runtime.matches_model_config(expected, expected))
        self.assertTrue(runtime.matches_model_config({**expected, "$schema": "https://opencode.ai/config.json"}, expected))
        self.assertFalse(runtime.matches_model_config({**expected, "$schema": "https://unknown.invalid/config.json"}, expected))
        self.assertFalse(runtime.matches_model_config({**expected, "permission": "allow"}, expected))
        self.assertFalse(runtime.matches_model_config({**expected, "model": "other/model"}, expected))
        self.assertFalse(runtime.matches_model_config({**expected, "provider": {}}, expected))

    def save(self, **changes):
        payload = dict(name="团队封装", expected_config=None, content=Content(
            serve_command="team-code serve --hostname {host} --port {port}", run_command="team-code run --attach {endpoint} --model {model} --format json", version_command="team-code --version", credential_env=["TEAM_API_KEY"]).model_dump(), actor="开发者", reason="接入封装程序", set_default=False)
        payload.update(changes)
        response = self.client.post("/api/v1/agent-clis", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def activate(self, identity):
        response = self.client.post(f"/api/v1/agent-clis/{identity}/default", json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepared_with(self, selected):
        self.activate(selected["id"])
        return self.prepared()

    def test_authentication_and_no_host_execution_when_editing_configuration(self):
        with patch("agent_workbench.runtime.docker", side_effect=AssertionError("API must not run Docker")):
            saved = self.save(); self.activate(saved["id"])
            response = self.client.get("/api/v1/agent-clis").json()
            self.assertEqual(response["default_id"], saved["id"])
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/api/v1/agent-clis").status_code, 401)
        self.assertEqual(self.client.post(f"/api/v1/agent-clis/{saved['id']}/default", json={}).status_code, 401)

    def test_config_versions_are_immutable_stale_editors_rejected_and_builtin_cloned(self):
        first = self.save(); second = self.save(expected_config=first["id"], name="封装新版")
        self.assertEqual(second["family"], first["family"]); self.assertEqual(second["number"], 2)
        self.assertEqual(self.store.get(agent_cli_configs, first["id"])["name"], first["name"])
        response = self.client.post("/api/v1/agent-clis", json=dict(expected_config=first["id"], name="过期编辑", content=first["content"], actor="本人", reason="过期"))
        self.assertEqual(response.status_code, 409)
        clone = self.save(expected_config=agent_clis.BUILTIN)
        self.assertNotEqual(clone["family"], agent_clis.BUILTIN)

    def test_invalid_commands_protocols_placeholders_and_system_variables_rejected(self):
        for field, value in (("serve_command", "team-code serve; echo private"), ("run_command", "team-code run {prompt}"), ("run_command", "team-code run --format json"), ("serve_command", "team-code\nserve"), ("protocol", "plain-text"), ("credential_env", ["AWB_ACCESS_TOKEN"]), ("credential_env", ["DOCKER_HOST"]), ("credential_env", ["OPENCODE_SERVER_PASSWORD"]), ("credential_env", ["LD_PRELOAD"]), ("credential_env", ["TEAM_API_KEY", "TEAM_API_KEY"])):
            content = Content().model_dump(); content[field] = value
            response = self.client.post("/api/v1/agent-clis", json=dict(name="非法配置", content=content, actor="本人", reason="测试"))
            self.assertEqual(response.status_code, 422, (field, value))

    def test_defaults_are_fixed_when_prepare_is_enqueued_and_old_workspaces_do_not_switch(self):
        first = self.save(set_default=True); req = self.requirement()
        queued = self.action(req, "prepare", "one-prepare").json()
        self.activate(agent_clis.BUILTIN)
        self.runner.run_once()
        detail = self.detail(req)
        self.assertEqual(detail["agent_cli"]["id"], first["id"]); self.assertTrue(detail["agent_cli"]["fixed"])
        self.assertEqual(self.store.get(workspace_agents, req["id"])["config_id"], first["id"])
        self.assertEqual(self.action(req, "prepare", "one-prepare").json()["id"], queued["id"])
        new_req = self.requirement(); self.assertEqual(self.detail(new_req)["agent_cli"]["id"], agent_clis.BUILTIN)

    def test_legacy_prepared_workspace_remains_builtin_after_global_default_changes(self):
        detail = self.prepared()
        from sqlalchemy import delete
        with self.store.engine.begin() as c: c.execute(delete(workspace_agents).where(workspace_agents.c.requirement_id == detail["id"]))
        path = Path(detail["workspace"]["path"])/"workspace.json"; content = json.loads(path.read_text()); content.pop("agent_cli", None); write_json(path, content)
        self.save(set_default=True)
        self.assertEqual(self.detail(detail)["agent_cli"]["id"], agent_clis.BUILTIN)

    def test_project_explicit_selection_overrides_global_default_and_old_version_is_kept(self):
        first = self.save(); self.save(set_default=True)
        project = self.project()
        from agent_workbench.profiles import Content as Profile
        response = self.client.post(f"/api/v1/projects/{project['id']}/profiles", json=dict(expected_profile=None, content=Profile(agent_cli_id=first["id"]).model_dump(), actor="本人", reason="项目固定 CLI"))
        self.assertEqual(response.status_code, 201)
        req = self.requirement(project); self.action(req, "prepare")
        job = self.store.claim("fixture")
        self.assertEqual(job["agent_cli"]["id"], first["id"])
        second = self.save(expected_config=first["id"], name="新版本", set_default=True)
        self.assertNotEqual(self.detail(req)["agent_cli"]["id"], second["id"])

    def test_missing_project_cli_version_is_rejected(self):
        project = self.project()
        from agent_workbench.profiles import Content as Profile
        response = self.client.post(f"/api/v1/projects/{project['id']}/profiles", json=dict(expected_profile=None, content=Profile(agent_cli_id="missing-config").model_dump(), actor="本人", reason="错误选择"))
        self.assertEqual(response.status_code, 404)

    def test_commands_substitute_only_public_endpoint_and_model_never_prompt_or_secret(self):
        selected = self.save()
        self.assertEqual(agent_clis.command(selected, "serve"), ["team-code", "serve", "--hostname", "0.0.0.0", "--port", "4096"])
        args = agent_clis.command(selected, "run", model="opencode/glm-5.3-flash-free")
        self.assertEqual(args[0], "team-code"); self.assertIn("http://127.0.0.1:4096", args)
        changed = json.loads(json.dumps(selected)); changed["content"]["run_command"] = "other-code run {endpoint} {model}"
        with self.assertRaisesRegex(WorkbenchError, "摘要"): agent_clis.command(changed, "run")

    def test_workspace_descriptor_cannot_override_database_binding(self):
        first = self.save(); req = self.prepared_with(first); workspace = Path(req["workspace"]["path"])
        second = self.save(); manifest = json.loads((workspace/"workspace.json").read_text()); manifest["agent_cli"] = {key: second[key] for key in ("id", "name", "number", "content", "content_digest")}; write_json(workspace/"workspace.json", manifest)
        with self.assertRaisesRegex(WorkbenchError, "控制服务固定版本"): agent_clis.verify_workspace(workspace, first)

    def test_readonly_inherit_preserves_cli_without_changing_profile(self):
        selected = self.save(); req = self.prepared_with(selected); source = Path(req["workspace"]["path"])
        target = self.root / "isolated"; target.mkdir(); write_json(target/"workspace.json", {"fixture": True})
        agent_clis.inherit(source, target)
        inherited = json.loads((target/"workspace.json").read_text())
        self.assertEqual(inherited["agent_cli"]["id"], selected["id"]); self.assertNotIn("runtime_profile", inherited)

    def test_runtime_preserves_image_startup_and_launches_manifest_cli_separately(self):
        selected = self.save(); req = self.prepared_with(selected); workspace = Path(req['workspace']['path'])
        def docker(*args, **options):
            if args[:2] == ('network','inspect'):
                return subprocess.CompletedProcess([],1,'','missing')
            return subprocess.CompletedProcess([],0,'127.0.0.1:4096\n' if args[0]=='port' else '','')
        with patch('agent_workbench.runtime.docker',side_effect=docker) as calls:
            runtime.start(workspace,'wrapper:fixture',opencode_model=self.settings.model)
        args = next(call.args for call in calls.call_args_list if call.args[0]=='run')
        from agent_workbench.agent_service import launch_argv
        self.assertFalse(any(arg.startswith('--entrypoint') for arg in args))
        self.assertEqual(args[-1], 'wrapper:fixture')
        launched = next(call.args for call in calls.call_args_list if call.args[:2] == ('exec', '--detach'))
        state = json.loads((workspace/'runtime.json').read_text())
        self.assertEqual(launched[2], state['container'])
        self.assertEqual(list(launched[3:]), launch_argv(agent_clis.command(selected, 'serve'), directory=state['agent_service']['directory']))
        self.assertLess(calls.call_args_list.index(next(call for call in calls.call_args_list if call.args == args)),
                        calls.call_args_list.index(next(call for call in calls.call_args_list if call.args == launched)))
        self.assertEqual(state['agent_cli']['id'], selected['id'])
        self.assertIn('agent_service', state)

    def test_runtime_absolute_cli_path_is_one_exec_argument(self):
        selected = self.save(content=Content(serve_command='/opt/team/bin/team-code serve --hostname {host} --port {port}').model_dump())
        req = self.prepared_with(selected); workspace = Path(req['workspace']['path'])
        def docker(*args, **options):
            if args[:2] == ('network','inspect'): return subprocess.CompletedProcess([],1,'','missing')
            return subprocess.CompletedProcess([],0,'127.0.0.1:4096\n' if args[0]=='port' else '','')
        with patch('agent_workbench.runtime.docker',side_effect=docker) as calls:
            runtime.start(workspace,'wrapper:fixture')
        args = next(call.args for call in calls.call_args_list if call.args[0]=='run')
        from agent_workbench.agent_service import launch_argv
        self.assertEqual(args[-1], 'wrapper:fixture')
        launched = next(call.args for call in calls.call_args_list if call.args[:2] == ('exec', '--detach'))
        state = json.loads((workspace/'runtime.json').read_text())
        self.assertEqual(list(launched[3:]), launch_argv(['/opt/team/bin/team-code','serve','--hostname','0.0.0.0','--port','4096'], directory=state['agent_service']['directory']))
        self.assertEqual(launched.count('/opt/team/bin/team-code'), 1)

    def test_missing_cli_error_identifies_pinned_command_and_retains_runtime_journal(self):
        selected = self.save(); req = self.prepared_with(selected); workspace = Path(req['workspace']['path'])
        def docker(*args, **options):
            if args[:2] == ('network','inspect'): return subprocess.CompletedProcess([],1,'','missing')
            if args[:2] == ('exec', '--detach'): raise WorkbenchError('exec: "team-code": executable file not found in $PATH')
            return subprocess.CompletedProcess([],0,'127.0.0.1:4096\n' if args[0]=='port' else '','')
        with patch('agent_workbench.runtime.docker',side_effect=docker):
            with self.assertRaises(WorkbenchError) as failure: runtime.start(workspace,'wrapper:fixture')
        message = str(failure.exception)
        for expected in ('团队封装','team-code serve','wrapper:fixture','绝对路径','/home/node','executable file not found'):
            self.assertIn(expected,message)
        self.assertEqual(json.loads((workspace/'runtime.json').read_text())['phase'],'NEEDS_RECONCILIATION')

    def test_missing_published_port_reports_container_exit_and_pinned_cli(self):
        selected = self.save(); req = self.prepared_with(selected); workspace = Path(req['workspace']['path'])
        def docker(*args, **options):
            if args[:2] == ('network', 'inspect'):
                return subprocess.CompletedProcess([], 1, '', 'missing')
            if args[0] == 'port':
                return subprocess.CompletedProcess([], 1, '', 'no public port 4096/tcp published')
            if args[0] == 'inspect':
                return subprocess.CompletedProcess([], 0, json.dumps([{
                    'State': {'Status': 'exited', 'Running': False, 'ExitCode': 7, 'Error': 'image startup failed'},
                    'Config': {},
                }]), '')
            if args[0] == 'logs':
                return subprocess.CompletedProcess([], 0, 'code-server startup failed', '')
            return subprocess.CompletedProcess([], 0, '', '')
        with patch('agent_workbench.runtime.docker', side_effect=docker) as calls:
            with self.assertRaises(WorkbenchError) as failure:
                runtime.start(workspace, 'wrapper:fixture')
        message = str(failure.exception)
        for expected in ('团队封装', 'team-code serve', '4096', 'exited', '7'):
            self.assertIn(expected, message)
        self.assertEqual(json.loads((workspace/'runtime.json').read_text())['phase'], 'NEEDS_RECONCILIATION')
        self.assertFalse(any(call.args[:2] == ('exec', '--detach') for call in calls.call_args_list))

    def test_agent_exit_is_not_masked_by_running_code_server_or_healthy_control_endpoint(self):
        selected = self.save(); req = self.prepared_with(selected); workspace = Path(req['workspace']['path'])
        state = {'container': 'fixture-code-server', 'phase': 'STARTED', 'agent_cli': selected,
                 'serve_argv': agent_clis.command(selected, 'serve'), 'base_url': 'http://127.0.0.1:4096',
                 'password': 'fixture-password', 'agent_service': {'directory': '/tmp/awb-agent-fixture'}}
        write_json(workspace/'runtime.json', state, private=True)
        details = {'State': {'Status': 'running', 'Running': True, 'ExitCode': 0}, 'Config': {}, 'Mounts': []}
        observed = {'agent_status': 'exited', 'agent_exit_code': 7, 'log_tail': 'wrapper failed to start'}
        with patch('agent_workbench.runtime.docker', return_value=subprocess.CompletedProcess([], 0, json.dumps([details]), '')), \
                patch('agent_workbench.runtime.service_status', return_value=observed) as status, \
                patch('agent_workbench.runtime.OpenCodeClient') as client:
            client.return_value.probe.return_value = {'health': {'healthy': True}}
            with self.assertRaises(WorkbenchError) as failure:
                runtime.probe(workspace)
        self.assertTrue(status.called)
        client.return_value.probe.assert_not_called()
        for expected in ('团队封装', 'team-code serve', '7'):
            self.assertIn(expected, str(failure.exception))

    def test_runtime_rejects_cli_conflicting_with_manifest_before_docker(self):
        selected = self.save(); req = self.prepared_with(selected); workspace = Path(req['workspace']['path'])
        other = self.save()
        with patch('agent_workbench.runtime.docker') as docker:
            with self.assertRaisesRegex(WorkbenchError,'固定版本不一致'):
                runtime.start(workspace,'wrapper:fixture',agent_cli=other)
            docker.assert_not_called()

    def test_task_uses_wrapper_command_redacts_custom_credential_and_records_source(self):
        selected = self.save(); req = self.prepared_with(selected); workspace = Path(req["workspace"]["path"])
        config = workspace/"agent-home/.config/opencode"; config.mkdir(parents=True); write_json(config/"opencode.json", runtime.model_config(self.settings.model))
        write_json(workspace/"runtime.json", {"container": "controlled-fixture", "agent_cli": selected, "run_as_root": True})
        sink = io.StringIO(); sink.close = lambda: None
        stream = '\n'.join(json.dumps(item) for item in [{"type":"text","part":{"text":"fixture TEAM-PRIVATE-KEY"}}, {"type":"step_finish","part":{"reason":"stop"}}])+'\n'
        process = Mock(stdin=sink, stdout=io.StringIO(stream), stderr=io.StringIO(""), returncode=0); process.poll.return_value=0
        events=[]
        with patch.dict(os.environ, {"TEAM_API_KEY": "TEAM-PRIVATE-KEY"}), patch("agent_workbench.execution.subprocess.Popen", return_value=process) as started, patch.object(self.runner.executor, "observe", return_value={}):
            result = self.runner.executor.task({"agent_cli": selected}, workspace, lambda k,p: events.append((k,p)), threading.Event(), prompt="private task input", collect_text=True)
        args = started.call_args.args[0]
        from agent_workbench.container_shell import shell_argv
        self.assertEqual(args[5:], shell_argv(agent_clis.command(selected, "run", model=self.settings.model), root=True))
        self.assertNotIn("private task input", args); self.assertEqual(sink.getvalue(), "private task input")
        self.assertNotIn("TEAM-PRIVATE-KEY", json.dumps(events)); self.assertEqual(result["agent_cli"]["id"], selected["id"])
        self.assertEqual([p for k,p in events if k=="model_completion"][0]["agent_cli_id"], selected["id"])

    def test_configurations_and_pinned_default_survive_offline_backup_restore(self):
        selected = self.save(set_default=True); req = self.prepared()
        with self.store.engine.begin() as c:
            c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")); c.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
        with patch("agent_workbench.resources.inventory", return_value=[]): saved = backup(self.settings)
        destination = self.root/"restored"; restore(Path(saved["file"]), destination)
        restored = Store(f"sqlite:///{destination/'workbench.db'}")
        try:
            self.assertEqual(agent_clis.Configurations(restored).read()["default_id"], selected["id"])
            self.assertEqual(restored.get(workspace_agents, req["id"])["config_id"], selected["id"])
        finally: restored.engine.dispose()
