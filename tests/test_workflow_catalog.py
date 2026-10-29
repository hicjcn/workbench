"""Round selections stay bound to the CLI catalog, including archived rounds."""
import json
from pathlib import Path

from agent_workbench.workspace import write_json
from test_control_plane import ControlFixture


class WorkflowCatalogTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.req = self.prepared()
        self.workspace = Path(self.req["workspace"]["path"])
        self.endpoint = f"/api/v1/requirements/{self.req['id']}"
        manifest = json.loads((self.workspace / "workspace.json").read_text())
        cli = manifest["agent_cli"]
        self.catalog = dict(schema="workbench-model-catalog/v1", status="READY",
            models=[{"id": identity, "label": identity} for identity in ("team/analyzer", "team/reviewer-a", "team/reviewer-b")],
            agent_cli_id=cli["id"], agent_cli_digest=cli["content_digest"], image="", profile_digest=None, checked_at=123)
        write_json(self.workspace / "model-catalog.json", self.catalog)

    def start_round(self, **changes):
        body = dict(development_model="team/analyzer", review_models=["team/reviewer-a", "team/reviewer-b"],
            verification_command="node --test", reason="Use actual listed CLI models",
            digest=self.req["workspace"]["snapshot"]["tree_digest"])
        body.update(changes)
        return self.client.post(self.endpoint + "/workflow", json=body)

    def test_round_pins_primary_reviewers_and_catalog_identity(self):
        response = self.start_round()
        self.assertEqual(response.status_code, 201, response.text)
        first = response.json()
        self.assertEqual(first["config"]["development_model"], "team/analyzer")
        self.assertEqual(first["config"]["review_models"], ["team/reviewer-a", "team/reviewer-b"])
        self.assertEqual(first["config"]["model_source"], "agent_cli")
        self.assertEqual(first["config"]["model_catalog"]["agent_cli_digest"], self.catalog["agent_cli_digest"])
        self.assertEqual(first["config"]["model_catalog"]["checked_at"], 123)
        second = self.start_round(development_model="team/reviewer-a")
        self.assertEqual(second.status_code, 201, second.text)
        archived = self.client.get(self.endpoint + "/workflow/runs/" + first["id"]).json()
        self.assertEqual(archived["config"]["development_model"], "team/analyzer")
        self.assertEqual(archived["state"], "SUPERSEDED")

    def test_unlisted_primary_or_reviewer_and_duplicates_are_rejected(self):
        for change in ({"development_model": "team/unlisted"},
                {"review_models": ["team/reviewer-a", "team/unlisted"]},
                {"review_models": ["team/reviewer-a", "team/reviewer-a"]}):
            with self.subTest(change=change):
                self.assertIn(self.start_round(**change).status_code, (409, 422))

    def test_failed_empty_or_stale_catalog_cannot_create_a_round(self):
        for change in ({"status": "FAILED", "models": []}, {"status": "EMPTY", "models": []},
                {"agent_cli_digest": "different"}, {"profile_digest": "new-profile"}):
            with self.subTest(change=change):
                write_json(self.workspace / "model-catalog.json", {**self.catalog, **change})
                self.assertEqual(self.start_round().status_code, 422)
