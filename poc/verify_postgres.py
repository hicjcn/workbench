"""Optional PostgreSQL migration, queue and event smoke test on an owned container."""
import argparse
from pathlib import Path
import json
import secrets
import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_workbench import runtime
from agent_workbench.api import create_app
from agent_workbench.server import migrate
from agent_workbench.settings import Settings
from agent_workbench.workspace import create_workspace, git, snapshot, write_json
from agent_workbench.workflow import Workflow
from agent_workbench.execution import Executor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists():
        raise ValueError("Use a new verification directory")
    root.mkdir(parents=True)
    password = secrets.token_urlsafe(24)
    container = "awb-pg-smoke-" + uuid.uuid4().hex[:10]
    report = {"status": "RUNNING"}
    try:
        runtime.docker("run", "--detach", "--name", container, "--label", "awb.m2-smoke=true",
            "--publish", "127.0.0.1::5432", "--memory", "512m", "--cpus", "1",
            "--env", "POSTGRES_PASSWORD", "postgres:17-bookworm", environment={"POSTGRES_PASSWORD": password})
        address = runtime.docker("port", container, "5432/tcp").stdout.strip()
        for _ in range(60):
            if runtime.docker("exec", container, "pg_isready", "--username", "postgres", check=False).returncode == 0:
                break
            time.sleep(0.5)
        source = root / "source"
        source.mkdir()
        (source / "file.txt").write_text("fixture\n")
        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "add", "--all")
        git(source, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        settings = Settings(root / "data", f"postgresql+psycopg://postgres:{password}@{address}/postgres",
            "fixture-user-token" * 2, "fixture-runner-token" * 2)
        settings.data_dir.mkdir()
        migrate(settings)
        app = create_app(settings)
        with TestClient(app, headers={"Authorization": "Bearer " + settings.access_token}) as client:
            project = client.post("/api/v1/projects", json={"name": "Postgres fixture", "source_path": str(source)}).json()
            requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "Postgres queue"}).json()
            path = f"/api/v1/requirements/{requirement['id']}/actions"
            first = client.post(path, json={"action": "prepare"}, headers={"Idempotency-Key": "one-intent"})
            assert first.status_code == 202, first.text
            repeated = client.post(path, json={"action": "prepare"}, headers={"Idempotency-Key": "one-intent"})
            assert repeated.json()["id"] == first.json()["id"]
            assert client.post(path, json={"action": "refresh"}).status_code == 409
            database = app.state.store
            job = database.claim("postgres_fixture")
            assert job["id"] == first.json()["id"]
            database.append_event(job["id"], "postgres_fixture", job["epoch"], 0, "output", {"text": "durable"})
            copied = create_workspace(source, settings.data_dir / "workspaces", requirement["id"], project["id"])
            observed = Executor(settings).observe(Path(copied["workspace"]), "STOPPED")
            database.finish(job["id"], "postgres_fixture", job["epoch"], "SUCCEEDED", observed, None)
            assert client.get(f"/api/v1/jobs/{job['id']}/events").json()[0]["payload"]["text"] == "durable"
            workflow = Workflow(database, settings.model)
            flow = workflow.start(requirement["id"], ["opencode/glm-5.3-flash-free", "opencode/big-pickle"], "node --test", "controlled PG workflow fixture", observed["snapshot"]["tree_digest"])
            workflow.enqueue(requirement["id"], flow["id"], "analysis", {"prompt": "", "decision": None, "reason": "", "dispositions": {}}, "pg-analysis")
            job = database.claim("postgres_fixture")
            database.finish(job["id"], "postgres_fixture", job["epoch"], "SUCCEEDED", {**observed,
                "workflow_output": {"summary": "controlled PG report; no inference", "impacted_paths": [], "plan": "test", "risks": []}}, None)
            assert workflow.read(requirement["id"])["current"]["next_step"] == "scope"
            before = workflow.read(requirement["id"])["revision"]
            workflow.revise(requirement["id"], before["id"], {"title": "PG revision", "description": "", "acceptance": ""}, "controlled change")
            assert workflow.read(requirement["id"])["current"]["state"] == "STALE"
            with database.engine.connect() as connection:
                report["postgres_version"] = connection.execute(text("SHOW server_version")).scalar_one()
                report["migration"] = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            database.engine.dispose()
        report.update(status="PASSED_POSTGRES_CONTROL_PLANE", controlled_model_report=True,
            checks=["migrations", "idempotency", "exclusive_job", "claim", "durable_event", "completion", "workflow_gate", "revision_invalidation"])
    finally:
        runtime.docker("rm", "--force", container, check=False)
        if report["status"] == "RUNNING":
            report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
