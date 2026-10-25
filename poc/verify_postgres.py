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
            from material_fixtures import complete_code_fixture, evidence, sample_document
            from agent_workbench.materials import Materials
            flow = complete_code_fixture(database, settings, requirement["id"], Path(copied["workspace"]))
            materials = Materials(database)
            material_round = materials.start(requirement["id"], {"flow_id": flow["id"], "digest": flow["basis_digest"], "reason": "controlled PG material fixture"}, "pg-material-start")
            def material_step(step, data=None):
                state = materials.read(requirement["id"])["current"]
                materials.enqueue(requirement["id"], {"round_id": material_round["id"], "step_key": step, "context_digest": state["context_digest"], "prompt": "", "data": data or {}}, None)
                claimed = database.claim("pg-material-fixture")
                result = {**observed, "material_output": sample_document(step)} if step in ("summary_design", "test_plan") else Executor(settings).execute(claimed, lambda *_: None, __import__("threading").Event())
                database.finish(claimed["id"], "pg-material-fixture", claimed["epoch"], "SUCCEEDED", result, None)
                assert database.get(__import__("agent_workbench.store", fromlist=["jobs"]).jobs, claimed["id"])["status"] == "SUCCEEDED"
            material_step("summary_design")
            assert materials.read(requirement["id"])["current"]["stage"] == "SUPERVISOR_REVIEW"
            from concurrent.futures import ThreadPoolExecutor
            state = materials.read(requirement["id"])["current"]
            intent = {"round_id": material_round["id"], "step_key": "supervisor_review", "context_digest": state["context_digest"], "prompt": "", "data": evidence(decision="APPROVED")}
            with ThreadPoolExecutor(max_workers=4) as pool:
                concurrent = list(pool.map(lambda _: materials.enqueue(requirement["id"], intent, "pg-concurrent-intent"), range(4)))
            assert len({item["id"] for item in concurrent}) == 1
            claimed = database.claim("pg-concurrent-worker")
            result = Executor(settings).execute(claimed, lambda *_: None, __import__("threading").Event())
            database.finish(claimed["id"], "pg-concurrent-worker", claimed["epoch"], "SUCCEEDED", result, None)
            material_step("supervisor_review", evidence(decision="APPROVED"))
            material_step("performance_decision", evidence(decision="NOT_REQUIRED"))
            material_step("test_plan")
            state = materials.read(requirement["id"])["current"]
            assert state["test_status"] == "NOT_RUN"
            material_step("test_execution", evidence(environment="controlled PG environment", results=[{"version_id": state["cases"][0]["version_id"], "case_id": "F1", "status": "PASS", "actual": "controlled registration; no business test executed", "evidence": "fixture://pg-result"}]))
            material_step("acceptance", evidence())
            material_step("export")
            state = materials.read(requirement["id"])["current"]
            assert state["accepted"]
            exported = client.get(f"/api/v1/requirements/{requirement['id']}/materials/exports/{state['exports'][0]['id']}")
            assert exported.status_code == 200 and exported.content.startswith(b"PK")
            from agent_workbench.delivery import Delivery
            from agent_workbench.store import jobs, reservations
            from sqlalchemy import update
            delivery = Delivery(database)
            other_project = client.post("/api/v1/projects", json={"name": "PG optional service", "source_path": str(source)}).json()
            optional = client.post("/api/v1/requirements", json={"project_id": other_project["id"], "title": "Explicitly skipped PG service"}).json()
            group = delivery.save({"title": "PG cross-project fixture", "description": "Controlled", "actor": "Fixture", "reason": "controlled PG", "expected_revision": None,
                "members": [{"requirement_id": requirement["id"], "required": True, "skip_reason": ""}, {"requirement_id": optional["id"], "required": False, "skip_reason": "not affected by controlled change"}]})["id"]
            delivery.decide(group, evidence(expected_scope=delivery.read(group)["scope_digest"], decision="APPROVED"))
            delivery.integrate(group, evidence(expected_baseline=delivery.read(group)["baseline_digest"], result="NOT_REQUIRED", environment="controlled PG", actual="no business integration test executed"))
            request = evidence(kind="capture", context_digest=delivery.read(group)["context_digest"])
            with ThreadPoolExecutor(max_workers=4) as pool:
                captures = list(pool.map(lambda _: delivery.enqueue(group, request, "pg-delivery-same-intent"), range(4)))
            assert len({item["id"] for item in captures}) == 1 and len(database.list(reservations)) == 2
            assert client.post(f"/api/v1/requirements/{optional['id']}/actions", json={"action": "prepare"}).status_code == 409
            claimed = database.claim("pg-delivery-worker")
            result = Executor(settings).execute(claimed, lambda *_: None, __import__("threading").Event())
            database.finish(claimed["id"], "pg-delivery-worker", claimed["epoch"], "SUCCEEDED", result, None)
            assert delivery.read(group)["exports"][0]["validity"] == "CURRENT" and not database.list(reservations)
            from agent_workbench.store import Conflict
            from threading import Barrier
            barrier = Barrier(2)
            def competing_intent(kind):
                barrier.wait()
                try:
                    return delivery.enqueue(group, evidence(kind="capture", context_digest=delivery.read(group)["context_digest"]), None) if kind == "group" else database.enqueue(optional["id"], "prepare", {"prompt": ""}, None)
                except Conflict: return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                race = list(pool.map(competing_intent, ["group", "legacy"]))
            winners = [item for item in race if item]
            assert len(winners) == 1
            database.cancel(winners[0]["id"])
            assert not database.list(reservations)
            unknown = delivery.enqueue(group, evidence(kind="capture", context_digest=delivery.read(group)["context_digest"]), None)
            database.claim("pg-lost-worker")
            with database.engine.begin() as connection: connection.execute(update(jobs).where(jobs.c.id == unknown["id"]).values(lease_until=0))
            database.expire(); assert len(database.list(reservations)) == 2
            delivery.enqueue(group, evidence(kind="reconcile", context_digest=delivery.read(group)["context_digest"]), None)
            claimed = database.claim("pg-reconcile-worker")
            result = Executor(settings).execute(claimed, lambda *_: None, __import__("threading").Event())
            database.finish(claimed["id"], "pg-reconcile-worker", claimed["epoch"], "SUCCEEDED", result, None)
            assert database.get(jobs, unknown["id"])["status"] == "CANCELED" and not database.list(reservations)
            queued = []
            for _ in range(3):
                extra = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "PG concurrent admission"}).json()
                queued.append(client.post(f"/api/v1/requirements/{extra['id']}/actions", json={"action": "prepare"}).json()["id"])
            with ThreadPoolExecutor(max_workers=6) as pool:
                claims = list(pool.map(lambda index: database.claim(f"pg-limit-{index}"), range(6)))
            claimed_jobs = [item for item in claims if item]
            assert len(claimed_jobs) == 2 and len({item["id"] for item in claimed_jobs}) == 2
            for item in claimed_jobs: database.finish(item["id"], item["owner"], item["epoch"], "FAILED", {}, "controlled admission completion")
            for identity in queued:
                if database.get(jobs, identity)["status"] == "QUEUED": database.cancel(identity)
            old = state["documents"]["summary_design"]
            material_step("edit_document", {"kind": "summary_design", "expected_version": old["id"], "content": {**old["content"], "markdown": "人工更新 PG 材料"}, "reason": "controlled update"})
            assert materials.read(requirement["id"])["current"]["stage"] == "SUPERVISOR_REVIEW"
            with database.engine.connect() as connection:
                report["postgres_version"] = connection.execute(text("SHOW server_version")).scalar_one()
                report["migration"] = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            from agent_workbench.maintenance import backup, restore
            from agent_workbench.store import Store
            from agent_workbench.profiles import Profiles, Content
            profile_service = Profiles(database, settings.data_dir)
            def concurrent_profile(_):
                try: return profile_service.save(project["id"], {"expected_profile": None, "content": Content().model_dump(), "actor": "PG fixture", "reason": "concurrent profile"})
                except Conflict: return None
            with ThreadPoolExecutor(max_workers=4) as pool:
                saved_profiles = [item for item in pool.map(concurrent_profile, range(4)) if item]
            assert len(saved_profiles) == 1
            profile_requirement = client.post("/api/v1/requirements", json={"project_id": project["id"], "title": "PG pinned profile"}).json()
            queued_profile = client.post(f"/api/v1/requirements/{profile_requirement['id']}/actions", json={"action": "prepare"}).json()
            database.cancel(queued_profile["id"])
            profile_service.save(project["id"], {"expected_profile": saved_profiles[0]["id"], "content": Content(cpus=2, dependencies=[{"kind": "postgres"}]).model_dump(), "actor": "PG fixture", "reason": "next version"})
            assert client.get(f"/api/v1/requirements/{profile_requirement['id']}").json()["profile"]["id"] == saved_profiles[0]["id"]
            assert client.get(f"/api/v1/requirements/{requirement['id']}").json()["profile"] is None
            backed_up = backup(settings)
            restore(Path(backed_up["file"]), root / "restored-sqlite")
            restored = Store(f"sqlite:///{root / 'restored-sqlite/workbench.db'}")
            try:
                assert Delivery(restored).read(group)["exports"][0]["sha256"] == delivery.read(group)["exports"][0]["sha256"]
                assert len(restored.list(jobs)) == len(database.list(jobs))
                assert len(Profiles(restored, root / "restored-sqlite").read(project["id"])["history"]) == 2
                assert Profiles(restored, root / "restored-sqlite").read(project["id"])["current"]["content"]["dependencies"][0]["kind"] == "postgres"
            finally: restored.engine.dispose()
            database.engine.dispose()
        report.update(status="PASSED_POSTGRES_CONTROL_PLANE", controlled_model_report=True,
            checks=["migrations", "idempotency", "exclusive_job", "claim", "durable_event", "completion", "workflow_gate", "revision_invalidation", "material_versions", "supervisor_gate", "test_definition_execution_separation", "material_acceptance", "material_export_download", "material_revision_invalidation", "concurrent_idempotent_material_intent", "delivery_scope_integration_gate", "concurrent_idempotent_delivery_intent", "all_member_reservations", "delivery_package", "group_unknown_reconcile", "concurrent_global_job_limit", "postgres_backup_to_new_sqlite_restore", "group_vs_legacy_enqueue_mutual_exclusion", "concurrent_profile_version_creation", "workspace_profile_pin_and_legacy_compatibility", "profile_versions_restore", "dependency_profile_json_preserved"])
    finally:
        runtime.docker("rm", "--force", container, check=False)
        if report["status"] == "RUNNING":
            report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
