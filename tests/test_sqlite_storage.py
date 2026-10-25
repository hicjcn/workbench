import contextlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from agent_workbench.api import create_app
from agent_workbench.server import migrate
from agent_workbench.settings import Settings, storage_summary
from agent_workbench.store import Store
from agent_workbench.workspace import git


class SQLiteStorageTests(unittest.TestCase):
    def test_default_and_empty_environment_use_local_file_and_explicit_flag_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for configured in ("", "   "):
                with patch.dict(os.environ, {"AWB_DATABASE_URL": configured}):
                    settings = Settings.load(root)
                self.assertEqual(settings.database_url, f"sqlite:///{root / 'workbench.db'}")
            url = "postgresql+psycopg://fixture:secret@not-running.invalid/workbench"
            with patch.dict(os.environ, {"AWB_DATABASE_URL": url}):
                self.assertEqual(Settings.load(root).database_url, url)
                self.assertEqual(Settings.load(root, sqlite=True).database_url, f"sqlite:///{root / 'workbench.db'}")

    def test_sqlite_cli_initializes_without_external_driver_or_database_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {**os.environ, "AWB_DATABASE_URL": "postgresql+uninstalled_driver://fixture:never-print@not-running.invalid/workbench"}
            result = subprocess.run([sys.executable, "-m", "agent_workbench.server", "--sqlite", "--init-only", "--data-dir", str(root)], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("never-print", result.stdout + result.stderr)
            self.assertIn("SQLite 本地文件", result.stdout)
            with sqlite3.connect(root / "workbench.db") as database:
                self.assertEqual(database.execute("SELECT version_num FROM alembic_version").fetchone()[0], "0008")

    def test_persistence_and_runtime_storage_survive_api_restart_without_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); settings = Settings.load(root / "data", sqlite=True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()): migrate(settings)
            source = root / "source"; source.mkdir(); (source / "main.txt").write_text("fixture\n")
            for args in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"), ("add", "--all"), ("-c", "commit.gpgsign=false", "commit", "-m", "fixture")): git(source, *args)
            store = Store(settings.database_url)
            try:
                with patch("agent_workbench.runtime.docker", side_effect=AssertionError("SQLite API must not require Docker")), TestClient(create_app(settings, store)) as client:
                    client.headers["Authorization"] = "Bearer " + settings.access_token
                    project = client.post("/api/v1/projects", json={"name": "本地存储", "source_path": str(source)})
                    self.assertEqual(project.status_code, 201)
                    req = client.post("/api/v1/requirements", json={"project_id": project.json()["id"], "title": "重启后保留需求"})
                    self.assertEqual(req.status_code, 201)
                    recipe = client.post("/api/v1/docker/recipes", json={"name": "本地模板", "reference": "awb-user/local:v1", "dockerfile": "FROM scratch", "actor": "测试", "reason": "持久化验证"})
                    self.assertEqual(recipe.status_code, 201)
            finally: store.engine.dispose()
            reopened = Store(settings.database_url)
            try:
                with patch("agent_workbench.runtime.docker", side_effect=AssertionError("no service needed")), TestClient(create_app(settings, reopened)) as client:
                    client.headers["Authorization"] = "Bearer " + settings.access_token
                    self.assertEqual(client.get("/api/v1/requirements").json()[0]["title"], "重启后保留需求")
                    self.assertEqual(client.get("/api/v1/docker").json()["recipes"][0]["id"], recipe.json()["id"])
                    info = client.get("/api/v1/runtime").json()["storage"]
                    self.assertEqual(info, {"backend": "sqlite", "path": str(settings.data_dir / "workbench.db"), "mode": "file", "external_service_required": False})
                    client.headers.pop("Authorization")
                    self.assertEqual(client.get("/api/v1/runtime").status_code, 401)
            finally: reopened.engine.dispose()

    def test_storage_summary_never_exposes_external_database_credentials(self):
        summary = storage_summary("postgresql+psycopg://fixture:private-password@private-host/workbench?application_name=private-query")
        self.assertEqual(summary, {"backend": "postgresql", "path": None, "mode": "server", "external_service_required": True})
        self.assertEqual(storage_summary("sqlite:///:memory:")["mode"], "memory")
