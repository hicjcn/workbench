"""Requirement edits preserve workspaces and invalidate earlier confirmations."""
from pathlib import Path
import time

from sqlalchemy import insert, update

from agent_workbench.store import delivery_groups, jobs, lifecycles, reservations
from test_control_plane import ControlFixture
from test_materials import MaterialFixture


class RequirementEditingTests(ControlFixture):
    def revision(self, req):
        response = self.client.get(f"/api/v1/requirements/{req['id']}/workflow")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["revision"]

    def edit_body(self, req):
        return {"expected_revision": self.revision(req)["id"], "title": "修改后的需求",
            "description": "新的业务背景", "acceptance": "新的验收标准", "reason": "修改需求"}

    def edit(self, req, body):
        return self.client.post(f"/api/v1/requirements/{req['id']}/revisions", json=body)

    def test_unprepared_requirement_edit_keeps_identity_project_and_workspace(self):
        req = self.requirement()
        before = self.detail(req)
        body = self.edit_body(req)
        response = self.edit(req, body)
        self.assertEqual(response.status_code, 201, response.text)
        after = self.detail(req)
        for key in ("id", "project_id", "repositories", "created_at", "workspace"):
            self.assertEqual(after[key], before[key])
        for key in ("title", "description", "acceptance"):
            self.assertEqual(after[key], body[key])
        history = self.client.get(f"/api/v1/requirements/{req['id']}/workflow").json()["revisions"]
        self.assertEqual([item["number"] for item in history], [2, 1])
        self.assertEqual(history[1]["content"]["title"], before["title"])
        listed = self.client.get("/api/v1/requirements").json()
        self.assertEqual(next(item for item in listed if item["id"] == req["id"])["title"], body["title"])

    def test_prepared_edit_invalidates_direct_approval_and_preserves_files_and_history(self):
        req = self.prepared()
        digest = req["workspace"]["snapshot"]["tree_digest"]
        response = self.client.post(f"/api/v1/requirements/{req['id']}/reviews", json={"digest": digest, "decision": "APPROVED"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(self.runner.run_once())
        before = self.detail(req)
        self.assertEqual(before["workspace"]["approval"], "APPROVED")
        workspace = Path(before["workspace"]["path"])
        files = {path: (workspace / path).read_bytes() for path in ("workspace.json", "repo/main.txt")}
        response = self.edit(req, self.edit_body(req))
        self.assertEqual(response.status_code, 201, response.text)
        after = self.detail(req)
        self.assertEqual(after["workspace"]["approval"], "STALE")
        for key in ("path", "snapshot", "environment", "diff"):
            self.assertEqual(after["workspace"][key], before["workspace"][key])
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["reviews"], before["reviews"])
        self.assertEqual({path: (workspace / path).read_bytes() for path in files}, files)
        self.assertIsNone(self.client.get(f"/api/v1/requirements/{req['id']}/workflow").json()["current"])

    def test_stale_revision_and_unchanged_edits_are_rejected(self):
        req = self.requirement()
        body = self.edit_body(req)
        unchanged = {**body, **{key: req[key] for key in ("title", "description", "acceptance")}}
        self.assertEqual(self.edit(req, unchanged).status_code, 409)
        self.assertEqual(self.edit(req, body).status_code, 201)
        self.assertEqual(self.edit(req, {**body, "title": "过期表单"}).status_code, 409)
        self.assertEqual(self.detail(req)["title"], body["title"])

    def test_queued_running_and_unknown_jobs_block_edit_without_new_revision(self):
        for status in ("QUEUED", "RUNNING", "UNKNOWN"):
            with self.subTest(status=status):
                req = self.requirement()
                body = self.edit_body(req)
                job = self.action(req, "prepare").json()
                with self.store.engine.begin() as connection:
                    connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status=status, lease_until=time.time() + 3600))
                self.assertEqual(self.edit(req, body).status_code, 409)
                self.assertEqual(self.revision(req)["id"], body["expected_revision"])
                self.assertEqual(self.detail(req)["title"], req["title"])

    def test_delivery_reservations_and_archived_requirements_block_edit(self):
        for table in (reservations, lifecycles):
            with self.subTest(table=table.name):
                req = self.requirement()
                body = self.edit_body(req)
                group_id = "group_" + table.name
                with self.store.engine.begin() as connection:
                    connection.execute(insert(delivery_groups).values(id=group_id, state="ACTIVE", created_at=time.time()))
                if table is reservations:
                    job = self.action(req, "prepare").json()
                    with self.store.engine.begin() as connection:
                        connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="CANCELED"))
                        connection.execute(insert(reservations).values(requirement_id=req["id"], group_id=group_id, job_id=job["id"]))
                else:
                    with self.store.engine.begin() as connection:
                        connection.execute(insert(lifecycles).values(requirement_id=req["id"], group_id=group_id))
                response = self.edit(req, body)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("交付", response.json()["detail"])
                self.assertEqual(self.revision(req)["id"], body["expected_revision"])

    def test_edit_rejects_blank_title_immutable_fields_and_unauthenticated_requests(self):
        req = self.requirement()
        body = self.edit_body(req)
        for extra in ({"title": " "}, {"project_id": req["project_id"]}, {"repositories": []}):
            self.assertEqual(self.edit(req, {**body, **extra}).status_code, 422)
        self.client.headers.pop("Authorization")
        self.assertEqual(self.edit(req, body).status_code, 401)


class RequirementMaterialEditingTests(MaterialFixture):
    def test_requirement_edit_invalidates_material_round_and_retains_records(self):
        self.start()
        self.submit("summary_design")
        self.finish()
        before = self.state()
        revision = self.workflow.read(self.req["id"])["revision"]
        response = self.client.post(f"/api/v1/requirements/{self.req['id']}/revisions", json={
            "expected_revision": revision["id"], "title": "修改材料对应需求", "description": "",
            "acceptance": "更新验收", "reason": "修改需求"})
        self.assertEqual(response.status_code, 201, response.text)
        after = self.state()
        self.assertEqual(after["state"], "STALE")
        self.assertFalse(after["context_valid"])
        self.assertEqual(after["documents"]["summary_design"]["id"], before["documents"]["summary_design"]["id"])
        self.assertEqual(self.detail(self.req)["workspace"]["approval"], "STALE")
