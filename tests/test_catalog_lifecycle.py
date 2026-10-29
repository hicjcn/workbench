"""Listing models owns no development environment and cannot reuse stale images."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from sqlalchemy import update
from agent_workbench import runtime
from agent_workbench.execution import Canceled
from agent_workbench.store import workspaces
from agent_workbench.workspace import write_json
from test_control_plane import ControlFixture


class CatalogLifecycleTests(ControlFixture):
    def test_canceling_model_list_preserves_development_environment(self):
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        state = {"container": "controlled", "phase": "STARTED"}
        write_json(workspace / "runtime.json", state)
        with self.store.engine.begin() as connection:
            connection.execute(update(workspaces).where(workspaces.c.requirement_id == detail["id"]).values(environment="RUNNING"))
        self.action(detail, "list_models")
        with patch("agent_workbench.model_catalog.execute", side_effect=Canceled("读取已取消")), \
                patch("agent_workbench.resources.stop_workspace") as stop:
            self.runner.run_once()
        stop.assert_not_called()
        result = self.detail(detail)
        self.assertEqual(result["jobs"][0]["status"], "CANCELED")
        self.assertEqual(result["workspace"]["environment"], "RUNNING")
        self.assertEqual(json.loads((workspace / "runtime.json").read_text()), state)

    def test_starting_unprofiled_mutable_image_discards_previous_catalog(self):
        detail = self.prepared()
        workspace = Path(detail["workspace"]["path"])
        report = workspace / "model-catalog.json"
        write_json(report, {"status": "READY", "models": [{"id": "old/model"}]})
        def docker(*args, **_kwargs):
            if args[:2] == ("network", "inspect"): return subprocess.CompletedProcess([], 1, "", "No such network")
            output = "127.0.0.1:4096\n" if args[0] == "port" else '[{"State":{"Running":true}}]' if args[0] == "inspect" else ""
            return subprocess.CompletedProcess([], 0, output, "")
        with patch("agent_workbench.runtime.docker", side_effect=docker):
            runtime.start(workspace, "mutable:tag", start_agent=False)
        self.assertFalse(report.exists())
