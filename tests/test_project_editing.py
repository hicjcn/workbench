import json
from pathlib import Path
import shutil

from agent_workbench.workspace import git
from test_control_plane import ControlFixture


class ProjectEditingTests(ControlFixture):
    def edit(self, project, **changes):
        value = {key: project[key] for key in ("name", "source_path", "image", "repositories", "shared_files")}
        return self.client.put(f"/api/v1/projects/{project['id']}", json={**value, **changes})

    def multi_project(self):
        parent = self.root / "business"
        parent.mkdir()
        for name in ("first", "second"):
            shutil.copytree(self.source, parent / name)
        response = self.client.post("/api/v1/projects", json={
            "name": "多仓库", "source_path": str(parent), "repositories": ["first", "second"],
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_edit_preserves_identity_and_existing_workspace_while_new_workspaces_use_new_source(self):
        project = self.project()
        requirement = self.requirement(project)
        self.action(requirement, "prepare")
        self.runner.run_once()
        before = self.detail(requirement)
        workspace = Path(before["workspace"]["path"])
        manifest = (workspace / "workspace.json").read_bytes()
        replacement = self.root / "replacement"
        shutil.copytree(self.source, replacement)
        (replacement / "main.txt").write_text("replacement source\n")
        git(replacement, "add", "--all")
        git(replacement, "-c", "commit.gpgsign=false", "commit", "-m", "replacement")

        response = self.edit(project, name=" 更新项目 ", source_path=str(replacement), image="custom-runtime:v2")
        self.assertEqual(response.status_code, 200, response.text)
        edited = response.json()
        self.assertEqual(edited["id"], project["id"])
        self.assertEqual(edited["created_at"], project["created_at"])
        self.assertEqual(edited["name"], "更新项目")
        self.assertEqual(edited["image"], "custom-runtime:v2")
        self.assertEqual((workspace / "workspace.json").read_bytes(), manifest)
        self.assertEqual((workspace / "repo/main.txt").read_text(), "original\n")
        self.assertEqual(self.detail(requirement)["workspace"]["snapshot"], before["workspace"]["snapshot"])
        self.assertEqual(self.detail(requirement)["project"]["name"], "更新项目")

        newer = self.requirement(edited)
        self.action(newer, "prepare")
        self.runner.run_once()
        self.assertEqual((Path(self.detail(newer)["workspace"]["path"]) / "repo/main.txt").read_text(), "replacement source\n")

    def test_edit_uses_registration_validation_and_authentication(self):
        project = self.project()
        for changes in ({"name": " "}, {"source_path": str(self.root)}, {"image": "invalid image"}, {"shared_files": ["README.md"]}):
            self.assertEqual(self.edit(project, **changes).status_code, 422)
        self.assertEqual(self.edit({**project, "id": "missing"}).status_code, 404)
        self.client.headers.pop("Authorization")
        self.assertEqual(self.edit(project).status_code, 401)

    def test_source_edit_waits_for_pending_jobs_but_name_and_image_can_change(self):
        project = self.project()
        requirement = self.requirement(project)
        job = self.action(requirement, "prepare").json()
        replacement = self.root / "replacement"
        shutil.copytree(self.source, replacement)
        self.assertEqual(self.edit(project, source_path=str(replacement)).status_code, 409)
        self.assertEqual(self.edit(project, name="renamed", image="runtime:v2").status_code, 200)
        self.client.post(f"/api/v1/jobs/{job['id']}/cancel", json={})
        self.assertEqual(self.edit(project, source_path=str(replacement)).status_code, 200)

    def test_unprepared_requirements_keep_candidates_and_layout_while_unused_repositories_can_be_removed(self):
        project = self.multi_project()
        requirement = self.client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "选中 first", "repositories": ["first"],
        }).json()
        self.assertEqual(self.edit(project, repositories=["second"]).status_code, 409)
        self.assertEqual(self.edit(project, source_path=str(self.source), repositories=[]).status_code, 409)
        response = self.edit(project, repositories=["first"])
        self.assertEqual(response.status_code, 200, response.text)
        self.action(requirement, "prepare")
        self.runner.run_once()
        detail = self.detail(requirement)
        self.assertEqual(detail["jobs"][0]["status"], "SUCCEEDED")
        manifest = json.loads((Path(detail["workspace"]["path"]) / "workspace.json").read_text())
        response = self.edit(response.json(), repositories=["second"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(json.loads((Path(detail["workspace"]["path"]) / "workspace.json").read_text()), manifest)

    def test_edit_cannot_invalidate_current_runtime_repository_commands(self):
        project = self.multi_project()
        saved = self.client.post(f"/api/v1/projects/{project['id']}/profiles", json={
            "content": {"repository_commands": [{"path": "first", "verification_command": "node --test"}]},
            "actor": "Fixture", "reason": "固定仓库命令",
        })
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(self.edit(project, repositories=["second"]).status_code, 409)
        self.assertEqual(self.edit(project, name="仍可改名").status_code, 200)
