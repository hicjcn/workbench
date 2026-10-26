from agent_workbench import locking
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch, Mock

from sqlalchemy import text, update
from test_control_plane import ControlFixture
from agent_workbench import docker_execution
from agent_workbench.docker_management import Images
from agent_workbench.store import docker_jobs, docker_recipes
from agent_workbench.execution import Unknown
from agent_workbench.maintenance import backup, restore
from agent_workbench.workspace import WorkbenchError


class DockerManagementTests(ControlFixture):
    def recipe(self, **updates):
        data = dict(expected_recipe=None, name="后端环境", reference="awb-user/backend:v1", dockerfile="FROM scratch\nLABEL fixture=true\n", actor="开发者", reason="首次版本")
        data.update(updates)
        response = self.client.post("/api/v1/docker/recipes", json=data)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def operation(self, kind="pull", key="intent-one", **updates):
        data = dict(kind=kind, reference="node:22-bookworm-slim", recipe_id=None, expected_image_id=None, actor="开发者", reason="测试维护")
        data.update(updates)
        return self.client.post("/api/v1/docker/operations", json=data, headers={"Idempotency-Key": key})

    def test_auth_no_docker_in_api_and_safe_empty_report(self):
        with patch("agent_workbench.runtime.docker", side_effect=AssertionError("API must not execute Docker")):
            state = self.client.get("/api/v1/docker").json()
            self.assertFalse(state["connected"]); self.assertEqual(state["images"], [])
            self.recipe(); self.assertEqual(self.operation().status_code, 202)
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/api/v1/docker").status_code, 401)
        self.assertEqual(self.client.post("/internal/docker/claim", json={"owner": "forged"}).status_code, 401)

    def test_recipe_versions_immutable_and_stale_editor_rejected(self):
        first = self.recipe(context_dir=str(self.root / "build context 中文"))
        second = self.recipe(expected_recipe=first["id"], reference="awb-user/backend:v2", dockerfile="FROM scratch\nLABEL fixture=two", context_dir=str(self.root / "another context"))
        self.assertEqual(second["number"], 2)
        self.assertEqual(self.store.get(docker_recipes, first["id"])["dockerfile"], first["dockerfile"])
        self.assertEqual(self.store.get(docker_recipes, first["id"])["context_dir"], first["context_dir"])
        self.assertNotEqual(second["context_dir"], first["context_dir"])
        listing = self.client.get("/api/v1/docker").json()["recipes"]
        self.assertEqual([r["id"] for r in listing if r["current"]], [second["id"]])
        failed = self.client.post("/api/v1/docker/recipes", json={k: v for k, v in dict(second, expected_recipe=first["id"]).items() if k in ("expected_recipe", "name", "reference", "dockerfile", "actor", "reason")})
        self.assertEqual(failed.status_code, 409)

    def test_reference_validation_and_missing_build_binding(self):
        for value in ("--privileged", "node", "https://registry/image:v1", "node:22;rm", "Node:22", "node:22\n--host=x"):
            self.assertEqual(self.operation(reference=value).status_code, 422, value)
        self.assertEqual(self.operation(kind="build", reference="awb-user/backend:v1").status_code, 409)
        self.assertEqual(self.operation(kind="remove").status_code, 409)

    def test_idempotency_cancel_and_distinct_new_intent(self):
        first = self.operation(); repeated = self.operation()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        self.assertEqual(self.operation(reference="node:24").status_code, 409)
        self.assertEqual(self.operation(key="another").status_code, 409)
        self.assertEqual(self.client.post(f"/api/v1/docker/operations/{first.json()['id']}/cancel", json={}).status_code, 200)
        self.assertFalse(self.runner.image_once())
        self.assertEqual(self.operation(key="another").status_code, 202)

    def test_build_claim_binds_saved_version_and_success_is_not_replayed(self):
        recipe = self.recipe()
        self.operation("build", reference=recipe["reference"], recipe_id=recipe["id"])
        with patch("agent_workbench.docker_execution.execute", return_value={"image": {"id": "sha256:" + "1" * 64}}) as execute:
            self.assertTrue(self.runner.image_once()); self.assertFalse(self.runner.image_once())
        bound = execute.call_args.args[1]
        self.assertEqual(bound["recipe"]["dockerfile"], recipe["dockerfile"])
        self.assertEqual(self.store.list(docker_jobs)[0]["status"], "SUCCEEDED")

    def test_expiry_unknown_no_replay_and_old_owner_cannot_finish(self):
        self.operation(); service = Images(self.store); job = service.claim("old")
        with self.store.engine.begin() as c: c.execute(update(docker_jobs).values(lease_until=time.time() - 1))
        service.expire()
        self.assertIsNone(service.claim("replacement"))
        self.assertEqual(self.operation(key="new").status_code, 409)
        with self.assertRaisesRegex(Exception, "租约"): service.finish(job["id"], "old", job["epoch"], "SUCCEEDED", {}, None)
        locks = self.data / "locks"; locks.mkdir(exist_ok=True)
        for name in ("docker-images.lock", "docker-command.lock"):
            with (locks / name).open("a+b") as lock:
                locking.flock(lock, locking.LOCK_EX)
                response = self.client.post(f"/api/v1/docker/operations/{job['id']}/acknowledge", json={"actor": "本人", "reason": "已实际核实", "checked_daemon": True})
                self.assertEqual(response.status_code, 409)
        response = self.client.post(f"/api/v1/docker/operations/{job['id']}/acknowledge", json={"actor": "本人", "reason": "已在 Docker 主机检查原任务结束", "checked_daemon": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ACKNOWLEDGED")
        self.assertFalse(response.json()["result"]["execution_succeeded"])
        self.assertEqual(self.operation(key="new").status_code, 202)

    def test_image_tasks_share_active_limit_with_workspace_tasks(self):
        self.store.max_active_jobs = 1
        self.operation(); service = Images(self.store); image_job = service.claim("images")
        req = self.requirement(); self.action(req, "prepare")
        self.assertIsNone(self.store.claim("normal"))
        service.finish(image_job["id"], "images", image_job["epoch"], "SUCCEEDED", {}, None)
        normal = self.store.claim("normal")
        self.operation(key="second")
        self.assertIsNone(service.claim("images"))
        self.assertIsNotNone(normal)

    def test_owned_inventory_does_not_return_env(self):
        raw = {"Id": "sha256:" + "1" * 64, "RepoTags": ["fixture:v1"], "Size": 5, "Created": "2026-01-01", "Config": {"Env": ["PASSWORD=do-not-return"]}}
        with patch("agent_workbench.runtime.docker", side_effect=[Mock(stdout=raw["Id"] + "\n" + raw["Id"]), Mock(stdout=json.dumps([raw]), returncode=0)]): result = docker_execution.inventory()
        self.assertEqual(len(result), 1); self.assertNotIn("PASSWORD", json.dumps(result))

    def test_protected_reference_and_container_block_image_remove(self):
        request = {"reference": "awb-user/backend:v1", "expected_image_id": "sha256:" + "1" * 64}
        job = {"kind": "remove", "input": request, "protected_references": ["alias:v1"]}
        image = {"id": request["expected_image_id"], "tags": [request["reference"]], "size": 0}
        with patch.object(docker_execution, "image_info", return_value=image), patch.object(docker_execution, "command") as command:
            with self.assertRaisesRegex(WorkbenchError, "配置引用"): docker_execution.execute(self.settings, job)
            command.assert_not_called()
        job["protected_references"] = []
        with patch.object(docker_execution, "image_info", return_value=image), patch("agent_workbench.runtime.docker", return_value=Mock(stdout="existing-container")), patch.object(docker_execution, "command") as command:
            with self.assertRaisesRegex(WorkbenchError, "容器引用"): docker_execution.execute(self.settings, job)
            command.assert_not_called()

    def test_image_changed_id_refuses_remove_and_build_tag_cannot_overwrite(self):
        image = {"id": "sha256:" + "1" * 64}
        job = {"kind": "remove", "input": {"reference": "fixture:v1", "expected_image_id": "sha256:" + "2" * 64}, "protected_references": []}
        with patch.object(docker_execution, "image_info", return_value=image), patch.object(docker_execution, "command") as command:
            with self.assertRaisesRegex(WorkbenchError, "已变化"): docker_execution.execute(self.settings, job)
            command.assert_not_called()
        job = {"kind": "build", "input": {"reference": "awb-user/backend:v1"}}
        with patch.object(docker_execution, "image_info", return_value=image):
            with self.assertRaisesRegex(WorkbenchError, "标签已经存在"): docker_execution.execute(self.settings, job)

    def test_build_context_contains_only_saved_dockerfile(self):
        import tarfile
        recipe = self.recipe(); image = {"id": "sha256:" + "1" * 64}
        def inspect(args, lock, source=None):
            self.assertEqual(args[0], "build")
            with tarfile.open(fileobj=source) as archive:
                self.assertEqual(archive.getnames(), ["Dockerfile"])
                self.assertEqual(archive.extractfile("Dockerfile").read().decode(), recipe["dockerfile"])
        with patch.object(docker_execution, "image_info", side_effect=[None, image]), patch.object(docker_execution, "command", side_effect=inspect):
            result = docker_execution.execute(self.settings, {"kind": "build", "input": {"reference": recipe["reference"]}, "recipe": recipe})
        self.assertEqual(result["image"], image)

    def test_context_directory_requires_an_absolute_local_path(self):
        for value in ("relative/folder", "https://example.invalid/context", "--build-arg", "/bad\x00path"):
            with self.subTest(path=value):
                response = self.client.post("/api/v1/docker/recipes", json={"name": "context", "reference": "awb-user/test:v1", "dockerfile": "FROM scratch", "context_dir": value, "actor": "fixture", "reason": "validation"})
                self.assertEqual(response.status_code, 422)

    def test_queued_build_keeps_the_original_context_and_saved_dockerfile(self):
        first = self.recipe(context_dir=str(self.root / "first context"))
        self.operation("build", reference=first["reference"], recipe_id=first["id"])
        self.recipe(expected_recipe=first["id"], reference="awb-user/backend:v2", context_dir=str(self.root / "second context"))
        claimed = Images(self.store).claim("fixture-runner")
        self.assertEqual(claimed["recipe"]["context_dir"], first["context_dir"])
        self.assertEqual(claimed["recipe"]["dockerfile"], first["dockerfile"])

    def test_directory_build_sends_saved_dockerfile_and_native_path_separately(self):
        context = self.root / "build context 中文"; context.mkdir()
        (context / "Dockerfile").write_text("this file must not replace the saved version", encoding="utf-8")
        recipe = self.recipe(context_dir=str(context), dockerfile="FROM scratch\nCOPY payload.txt /payload.txt\n")
        image = {"id": "sha256:" + "1" * 64}
        def inspect(args, lock, source=None):
            self.assertEqual(args[-3:], ["--file", "-", str(context.resolve())])
            self.assertEqual(source.read(), recipe["dockerfile"].encode())
        with patch.object(docker_execution, "image_info", side_effect=[None, image]), patch.object(docker_execution, "command", side_effect=inspect):
            result = docker_execution.execute(self.settings, {"kind": "build", "input": {"reference": recipe["reference"]}, "recipe": recipe})
        self.assertEqual(result["build_context"], {"directory": str(context.resolve()), "dockerfile_source": "saved_recipe"})

    def test_missing_or_file_context_fails_before_any_docker_command(self):
        file = self.root / "not-a-directory"; file.write_text("fixture")
        for context in (self.root / "missing", file):
            recipe = self.recipe(context_dir=str(context))
            with patch.object(docker_execution, "image_info") as info, patch.object(docker_execution, "command") as command:
                with self.assertRaisesRegex(WorkbenchError, "上下文"): docker_execution.execute(self.settings, {"kind": "build", "input": {"reference": recipe["reference"]}, "recipe": recipe})
                info.assert_not_called(); command.assert_not_called()

    def test_command_timeout_is_unknown_and_docker_output_is_not_exposed(self):
        locks = self.data / "locks"; locks.mkdir()
        target = "agent_workbench.docker_worker.run" if docker_execution.IS_WINDOWS else "agent_workbench.docker_execution.subprocess.run"
        with (locks / "test.lock").open("a+b") as lock:
            with patch(target, side_effect=subprocess.TimeoutExpired("docker secret", 900)):
                with self.assertRaises(Unknown) as raised: docker_execution.command(["pull", "node:22"], lock)
                self.assertNotIn("secret", str(raised.exception))
            with patch(target, return_value=Mock(returncode=1, stderr="PASSWORD=secret")):
                with self.assertRaises(WorkbenchError) as raised: docker_execution.command(["pull", "node:22"], lock)
                self.assertNotIn("PASSWORD", str(raised.exception))

    def test_backup_restores_dockerfiles_and_completed_jobs_without_running_them(self):
        context = self.root / "external-build-context"; context.mkdir()
        (context / "private-file.txt").write_text("fixture-context-not-in-backup")
        self.recipe(context_dir=str(context)); record = self.operation().json(); self.client.post(f"/api/v1/docker/operations/{record['id']}/cancel", json={})
        with self.store.engine.begin() as c:
            c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")); c.execute(text("INSERT INTO alembic_version VALUES ('0010')"))
        with patch("agent_workbench.resources.inventory", return_value=[]): report = backup(self.settings)
        import zipfile
        with zipfile.ZipFile(report["file"]) as archive:
            self.assertFalse(any("private-file.txt" in name for name in archive.namelist()))
        destination = self.root / "restored"; restore(Path(report["file"]), destination)
        from agent_workbench.store import Store
        restored = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            self.assertEqual(restored.list(docker_recipes), self.store.list(docker_recipes))
            self.assertEqual(restored.list(docker_jobs)[0]["status"], "CANCELED")
        finally: restored.engine.dispose()

    def test_legacy_0008_backup_restores_dockerfiles_with_empty_context(self):
        from agent_workbench.checked_archive import build
        from agent_workbench.maintenance import rows_snapshot, metadata_digest
        from agent_workbench.store import Store, encoded
        self.recipe()
        data = rows_snapshot(self.store)
        for row in data["docker_recipes"]: row.pop("context_dir")
        archive = self.root / "legacy-0008.zip"
        build(archive, {"schema": "workbench-backup/v2", "schema_revision": "0008", "source_root": str(self.data), "metadata": metadata_digest(data), "code_checkpoints": {}}, [("database/" + name + ".json", encoded(rows).encode()) for name, rows in data.items()])
        destination = self.root / "legacy-restored"
        restore(archive, destination)
        restored = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            self.assertEqual(restored.list(docker_recipes)[0]["context_dir"], "")
            self.assertEqual(restored.list(docker_jobs), [])
        finally: restored.engine.dispose()
