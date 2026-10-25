"""Controlled resource failures and backup integrity; real database proof is in the PoC."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import text

from agent_workbench import dependencies, resources, runtime
from agent_workbench.checked_archive import verify
from agent_workbench.execution import Unknown
from agent_workbench.maintenance import backup, restore
from agent_workbench.profiles import Content, inherit
from agent_workbench.workspace import WorkbenchError, create_workspace, write_json
from test_control_plane import ControlFixture


class DependencyTests(ControlFixture):
    def setUp(self):
        super().setUp(); self.project_record = self.project()
        self.content = Content(dependencies=[{"kind": "postgres"}]).model_dump()
        self.profile_url = f"/api/v1/projects/{self.project_record['id']}/profiles"

    def save(self, content=None):
        return self.client.post(self.profile_url, json={"expected_profile": None, "content": content or self.content, "actor": "fixture", "reason": "controlled dependency tests"})

    def prepared_dependency(self):
        self.assertEqual(self.save().status_code, 201)
        req = self.requirement(self.project_record); self.action(req, "prepare")
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout=json.dumps([{"Id": "sha256:fixture"}]))): self.runner.run_once()
        self.req = self.detail(req); self.workspace = Path(self.req["workspace"]["path"])
        self.assertEqual(self.req["jobs"][0]["status"], "SUCCEEDED")
        return self.workspace

    def checkpoint(self, phase="STOPPED"):
        data = {"schema": "workbench-postgres/v1", "data_id": "fixture-dataset", "initialized": True, "phase": phase}
        write_json(self.workspace / dependencies.STATE, data)
        path = self.workspace / dependencies.DUMP; path.parent.mkdir(); path.write_bytes(b"PGDMP controlled bytes, not a real database")
        info = {"format": "postgres17/custom", "data_id": data["data_id"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
        write_json(self.workspace / dependencies.SNAPSHOT, info)
        return data, info

    def test_template_rejects_unsupported_images_types_and_multiple_instances(self):
        for deps in [[{"kind": "redis"}], [{"kind": "postgres", "image": "postgres:18"}], [{"kind": "postgres"}] * 2, [{"kind": "postgres", "memory_mib": 1}]]:
            self.assertEqual(self.save({**self.content, "dependencies": deps}).status_code, 422)

    def test_prepare_fixes_dependency_image_without_starting_it(self):
        workspace = self.prepared_dependency()
        fixed = json.loads((workspace / "workspace.json").read_text())["runtime_profile"]
        self.assertEqual(fixed["dependency_images"]["postgres"], "sha256:fixture")
        self.assertFalse((workspace / dependencies.STATE).exists())
        self.assertIsNone(self.req["dependency"])

    def test_whole_environment_capacity_is_checked_before_dependency_mutation(self):
        workspace = self.prepared_dependency()
        with patch("agent_workbench.resources.inventory", return_value=[{}] * 3), patch("agent_workbench.dependencies.needed", return_value=1), patch("agent_workbench.dependencies.ensure") as ensure, patch("agent_workbench.runtime.start") as start:
            with self.assertRaisesRegex(WorkbenchError, "上限"): self.runner.executor.start_runtime(workspace, "fixture")
            ensure.assert_not_called(); start.assert_not_called()

    def test_readonly_role_inherits_tools_but_never_gets_database_connection(self):
        workspace = self.prepared_dependency()
        role = Path(create_workspace(workspace / "repo", self.data / "attempts/job_fixture", self.req["id"], self.req["project_id"])["workspace"])
        inherit(workspace, role, self.data)
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.dependencies.ensure") as ensure, patch("agent_workbench.runtime.start") as start:
            self.runner.executor.start_runtime(role, "fixture", read_only_source=True, opencode_model="opencode/glm-5.3-flash-free")
            ensure.assert_not_called()
            self.assertNotIn("dependency_environment", start.call_args.kwargs)

    def test_resource_names_change_for_other_requirements_and_restore_directories(self):
        workspace = self.prepared_dependency()
        other = self.data / "workspaces/req_other" / self.req["project_id"]
        restored = self.root / "new-data/workspaces" / self.req["id"] / self.req["project_id"]
        for key in ("volume", "network", "container"):
            self.assertEqual(len({dependencies.names(path)[key] for path in (workspace, other, restored)}), 3)

    def test_password_is_redacted_from_command_output_and_errors(self):
        self.prepared_dependency(); password = "controlled-secret-value"
        write_json(self.workspace / dependencies.PRIVATE, {"password": password}, private=True)
        self.assertEqual(self.runner.executor.redact("value=" + password, self.workspace), "value=[REDACTED]")

    def test_snapshot_requires_matching_dataset_and_hash(self):
        self.prepared_dependency(); data, info = self.checkpoint()
        self.assertEqual(dependencies.snapshot_info(self.workspace, True), info)
        (self.workspace / dependencies.DUMP).write_bytes(b"changed")
        with self.assertRaisesRegex(WorkbenchError, "摘要不一致"): dependencies.snapshot_info(self.workspace, True)
        (self.workspace / dependencies.DUMP).write_bytes(b"PGDMP controlled bytes, not a real database")
        data["data_id"] = "other"; write_json(self.workspace / dependencies.STATE, data)
        with self.assertRaises(WorkbenchError): dependencies.snapshot_info(self.workspace, True)

    def test_backup_refuses_running_or_missing_dependency_data(self):
        self.prepared_dependency(); self.checkpoint("RUNNING")
        with self.assertRaisesRegex(WorkbenchError, "状态尚未核实"): dependencies.backup_files(self.workspace)
        state = dependencies.read(self.workspace, dependencies.STATE); state["phase"] = "STOPPED"; write_json(self.workspace / dependencies.STATE, state)
        (self.workspace / dependencies.DUMP).unlink()
        with self.assertRaises(WorkbenchError): dependencies.backup_files(self.workspace)

    def test_v2_backup_restores_data_and_metadata_without_password_or_runtime(self):
        self.prepared_dependency(); _, info = self.checkpoint()
        write_json(self.workspace / dependencies.PRIVATE, {"password": "do-not-back-up"}, private=True)
        with self.store.engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")); connection.execute(text("INSERT INTO alembic_version VALUES ('0006')"))
        with patch("agent_workbench.resources.inventory", return_value=[]): result = backup(self.settings)
        manifest = verify(Path(result["file"]))
        self.assertEqual(manifest["schema"], "workbench-backup/v2")
        self.assertFalse(any(item["path"].endswith(dependencies.PRIVATE) or item["path"].endswith(dependencies.JOURNAL) for item in manifest["entries"]))
        restored = self.root / "restored"; restore(Path(result["file"]), restored)
        workspace = restored / "workspaces" / self.req["id"] / self.req["project_id"]
        self.assertEqual(dependencies.snapshot_info(workspace, True)["sha256"], info["sha256"])
        self.assertEqual(dependencies.read(workspace, dependencies.STATE)["phase"], "RESTORED")
        self.assertFalse((workspace / dependencies.PRIVATE).exists())

    def test_unexpected_volume_loss_cannot_silently_restore_a_stale_snapshot(self):
        self.prepared_dependency(); self.checkpoint("RUNNING")
        with patch("agent_workbench.dependencies.inspect", return_value=None), patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaisesRegex(Unknown, "意外缺失"): dependencies.ensure(self.data, self.workspace)
            docker.assert_not_called()

    def test_unowned_container_or_volume_is_never_adopted(self):
        self.prepared_dependency(); data, _ = self.checkpoint()
        detail = {"Config": {"Labels": {"awb.controller": "other"}}, "Mounts": []}
        with patch("agent_workbench.dependencies.inspect", return_value=detail), patch("agent_workbench.runtime.docker") as docker:
            with self.assertRaisesRegex(WorkbenchError, "归属不一致"): dependencies.checked_container(self.data, self.workspace, data)
            docker.assert_not_called()

    def test_failed_dump_keeps_data_and_blocks_requirement_as_unknown(self):
        self.prepared_dependency(); self.checkpoint("RUNNING")
        self.action(self.req, "stop")
        with patch("agent_workbench.resources.inventory", return_value=[]), patch("agent_workbench.dependencies.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout="")), patch("agent_workbench.runtime.docker_stream", side_effect=WorkbenchError("controlled dump failure")):
            self.runner.run_once()
        detail = self.detail(self.req)
        self.assertEqual(detail["jobs"][0]["status"], "UNKNOWN")
        self.assertEqual(detail["dependency"]["phase"], "UNKNOWN")
        self.assertTrue((self.workspace / dependencies.DUMP).exists())
        self.assertEqual(self.action(self.req, "start").status_code, 409)

    def test_orphan_writer_is_removed_before_database_checkpoint(self):
        self.prepared_dependency(); sequence = []
        found = [{"workspace": str(self.workspace), "kind": "agent", "verified": True, "id": "writer"}, {"workspace": str(self.workspace), "kind": "postgres", "verified": True, "id": "database"}]
        with patch("agent_workbench.resources.inventory", return_value=found), patch("agent_workbench.runtime.docker", side_effect=lambda *args: sequence.append(args)), patch("agent_workbench.dependencies.stop", side_effect=lambda *_: sequence.append(("checkpoint",))), patch("agent_workbench.resources.cleanup_networks"):
            resources.stop_workspace(self.data, self.workspace)
        self.assertEqual(sequence, [("rm", "--force", "writer"), ("checkpoint",)])

    def test_missing_resource_errors_are_distinguished_from_engine_failure(self):
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(returncode=1, stderr="get owned-volume: no such volume")):
            self.assertIsNone(dependencies.inspect("volume", "fixture"))
        with patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(returncode=1, stderr="permission denied")):
            with self.assertRaises(WorkbenchError): dependencies.inspect("volume", "fixture")

    def test_unfinished_database_backends_prevent_snapshot_and_removal(self):
        self.prepared_dependency(); self.checkpoint("RUNNING")
        with patch("agent_workbench.dependencies.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.runtime.docker", return_value=SimpleNamespace(stdout="f\n")) as docker, patch("agent_workbench.runtime.docker_stream") as stream:
            with self.assertRaises(Unknown): dependencies.stop(self.data, self.workspace)
            stream.assert_not_called()
            self.assertFalse(any(call.args[0] == "rm" for call in docker.call_args_list))

    def test_stream_failure_never_includes_database_contents_in_error(self):
        with patch("agent_workbench.runtime.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr=b"private database content")):
            with self.assertRaises(WorkbenchError) as error: runtime.docker_stream("exec", "fixture", "pg_dump")
            self.assertNotIn("private database", str(error.exception))

    def test_stopping_failed_restore_preserves_certified_input_snapshot(self):
        self.prepared_dependency(); _, info = self.checkpoint("RESTORING")
        with patch("agent_workbench.dependencies.checked_container", return_value={"State": {"Running": True}}), patch("agent_workbench.dependencies.inspect", return_value=None), patch("agent_workbench.runtime.docker"), patch("agent_workbench.runtime.docker_stream") as stream:
            dependencies.stop(self.data, self.workspace); stream.assert_not_called()
        self.assertEqual(dependencies.read(self.workspace, dependencies.STATE)["phase"], "RESTORE_PENDING")
        self.assertEqual(dependencies.snapshot_info(self.workspace, True)["sha256"], info["sha256"])
        self.assertIn(dependencies.DUMP, dependencies.backup_files(self.workspace))

    def test_failed_restore_cleanup_keeps_restore_intent_after_unknown(self):
        self.prepared_dependency(); self.checkpoint("RESTORING")
        with patch("agent_workbench.dependencies.checked_container", return_value=None), patch("agent_workbench.dependencies.inspect", return_value={"Labels": {}, "Containers": {"unknown": {}}}), patch("agent_workbench.runtime.docker_stream") as stream:
            with self.assertRaises(Unknown): dependencies.stop(self.data, self.workspace)
            stream.assert_not_called()
        data = dependencies.read(self.workspace, dependencies.STATE)
        self.assertTrue(data["restore_required"]); self.assertEqual(data["phase"], "UNKNOWN")
        with patch("agent_workbench.dependencies.checked_container", return_value=None), patch("agent_workbench.dependencies.inspect", return_value=None), patch("agent_workbench.runtime.docker_stream") as stream:
            dependencies.stop(self.data, self.workspace); stream.assert_not_called()
        self.assertEqual(dependencies.read(self.workspace, dependencies.STATE)["phase"], "RESTORE_PENDING")
