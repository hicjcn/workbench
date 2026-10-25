"""Real PostgreSQL isolation, lifecycle and data restore; no model inference."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import zipfile

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench import application, dependencies, resources, runtime
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.settings import Settings
from agent_workbench.workspace import git, snapshot, write_json


QUERY = """const {connect}=require('./dataset.cjs'); (async()=>{const c=await connect();try{if(process.argv[1]==='put')await c.query('INSERT INTO items VALUES ($1,$2) ON CONFLICT(name) DO UPDATE SET value=excluded.value',[process.argv[2],process.argv[3]]);const r=await c.query('SELECT name,value FROM items ORDER BY name');console.log(JSON.stringify(r.rows));}finally{await c.end();}})().catch(()=>{console.error('Database sample failed');process.exit(1);});"""


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, required=True); parser.add_argument("--legacy-backup", type=Path)
    parser.add_argument("--application", action="store_true", help="Also run the M8 application lifecycle acceptance")
    parser.add_argument("--chromium", default="/usr/bin/chromium"); args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new dedicated verification directory")
    root.mkdir(parents=True, mode=0o700); data = root / "data"; restored = root / "restored"
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    image = "agent-workbench-m7-sample:" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
    processes, handles = [], []; client = None
    report = {"status": "RUNNING", "model_inference": False, "real_postgres_tests": True, "user_business_project_tested": False, "checks": {}}
    checks = report["checks"]
    def terminate():
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    def launch(module, directory):
        handle = (root / (module.rsplit(".", 1)[-1] + ".log")).open("a"); handles.append(handle)
        env = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_DATABASE_URL": f"sqlite:///{directory / 'workbench.db'}"}
        if args.application: env["AWB_MAX_CONTAINERS"] = "6"
        command = [sys.executable, "-m", module, "--data-dir", str(directory)] + (["--port", str(port)] if module.endswith("server") else ["--api", base])
        processes.append(subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT))
    def ready():
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/session", trust_env=False, timeout=1).status_code == 200: return
            except httpx.HTTPError: pass
            time.sleep(.1)
        raise AssertionError("API did not start")
    try:
        build = ["build", "--tag", image]
        if os.environ.get("CODEX_PROXY_CERT"): build += ["--secret", "id=proxy_ca,src=" + os.environ["CODEX_PROXY_CERT"]]
        runtime.docker(*build, str(Path(__file__).parent / "postgres-sample"), timeout=300)
        checks["sample_postgres_client_image_builds_with_tls"] = "PASSED"
        source = root / "source"; shutil.copytree(Path(__file__).parent / "postgres-sample/source", source)
        git(source, "init", "-b", "main"); git(source, "config", "user.name", "M7 Sample"); git(source, "config", "user.email", "sample@example.invalid"); git(source, "add", "--all"); git(source, "-c", "commit.gpgsign=false", "commit", "-m", "PostgreSQL sample")
        original = snapshot(source)
        launch("agent_workbench.server", data); ready()
        settings = replace(Settings.load(data), model="opencode/glm-5.3-flash-free", database_url=f"sqlite:///{data / 'workbench.db'}")
        client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=30)
        def post(path, payload):
            response = client.post(path, json=payload); response.raise_for_status(); return response.json()
        def action(req, kind, expected="SUCCEEDED"):
            job = post(f"/requirements/{req['id']}/actions", {"action": kind})
            for _ in range(600):
                current = client.get("/jobs/" + job["id"]).json()
                if current["status"] not in ("QUEUED", "RUNNING"):
                    assert current["status"] == expected, {"status": current["status"], "error": current.get("error")}; return current
                time.sleep(.2)
            raise AssertionError("Job timed out")
        def detail(req): return client.get("/requirements/" + req["id"]).json()
        def query(workspace, *values):
            state = json.loads((workspace / "runtime.json").read_text())
            return json.loads(runtime.docker("exec", state["container"], "node", "-e", QUERY, *values).stdout)
        project = post("/projects", {"name": "M7 PostgreSQL 样例", "source_path": str(source), "image": image})
        launch("agent_workbench.runner", data)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100}); errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token); page.get_by_role("button", name="进入工作台", exact=True).click()
            page.get_by_role("button", name="项目登记", exact=False).click(); page.get_by_role("button", name="保存运行配置", exact=True).click()
            form = page.get_by_role("dialog"); form.get_by_label("构建命令（可留空）", exact=True).fill("node --check dataset.cjs"); form.get_by_label("验证命令", exact=True).fill("node --test dataset.test.cjs")
            form.get_by_label("为每个需求启用独立 PostgreSQL 17", exact=True).check(); form.get_by_label("登记人", exact=True).fill("样例验收人"); form.get_by_label("配置原因", exact=True).fill("独立业务数据实测")
            if args.application: form.get_by_label("启用应用服务与 HTTP 检查", exact=True).check()
            with page.expect_response(lambda response: response.url.endswith("/profiles") and response.request.method == "POST") as receipt:
                form.get_by_role("button", name="保存新版本", exact=True).click()
            assert receipt.value.status == 201, receipt.value.text()
            checks["browser_register_per_requirement_postgres"] = "PASSED"
            reqs = [post("/requirements", {"project_id": project["id"], "title": title}) for title in ("M7 独立需求 A", "M7 独立需求 B")]
            paths = []
            for req in reqs:
                action(req, "prepare"); action(req, "preflight")
                current = detail(req); assert current["dependency"] is None
                assert any(item["name"] == "no_database_access" and item["passed"] for item in current["preflight"]["checks"])
                action(req, "start"); paths.append(Path(detail(req)["workspace"]["path"]))
            checks["readonly_preflight_never_connects_or_provisions_database"] = "PASSED"
            a, b = paths
            query(a, "put", "only-a", "A"); assert not query(b, "get")
            query(b, "put", "only-b", "B"); assert all(row["name"] != "only-b" for row in query(a, "get"))
            assert len(resources.inventory(data)) == 4
            db_a = json.loads(runtime.docker("inspect", dependencies.names(a)["container"]).stdout)[0]
            db_b = json.loads(runtime.docker("inspect", dependencies.names(b)["container"]).stdout)[0]
            assert not db_a["HostConfig"].get("PortBindings") and not db_b["HostConfig"].get("PortBindings")
            assert dependencies.names(a)["volume"] != dependencies.names(b)["volume"]
            assert json.loads((a / dependencies.PRIVATE).read_text())["password"] != json.loads((b / dependencies.PRIVATE).read_text())["password"]
            private_a = json.loads((a / dependencies.PRIVATE).read_text())["password"]
            checks["two_live_requirements_have_independent_data_volumes_credentials_and_no_host_ports"] = "PASSED"
            b_ip = db_b["NetworkSettings"]["Networks"][dependencies.names(b)["network"]]["IPAddress"]
            blocked = "const net=require('node:net');const c=net.connect({host:process.argv[1],port:5432});c.setTimeout(1500);c.on('connect',()=>process.exit(1));c.on('error',()=>process.exit(0));c.on('timeout',()=>process.exit(0));"
            state_a = json.loads((a / "runtime.json").read_text())
            assert runtime.docker("exec", state_a["container"], "node", "-e", blocked, b_ip, check=False).returncode == 0
            checks["cross_requirement_database_ip_access_is_blocked"] = "PASSED"
            tested = action(reqs[0], "verify")
            assert tested["result"]["command_output"]["exit_code"] == 0 and "# pass 1" in tested["result"]["command_output"]["output_tail"]
            checks["real_postgres_write_read_test_runs_inside_development_container"] = "PASSED"
            if args.application:
                for req in reqs: action(req, "app_start")
                assert len(resources.inventory(data)) == 6
                for workspace in (a, b):
                    app = application.checked_container(data, workspace)
                    assert app["State"]["Running"] and not app["HostConfig"].get("PortBindings")
                    assert app["HostConfig"]["ReadonlyRootfs"] and app["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
                    assert any(item["Destination"] == "/workspace/project" and not item["RW"] for item in app["Mounts"])
                    assert not any(value.startswith(("OPENROUTER_API_KEY=", "OPENCODE_SERVER_PASSWORD=")) for value in app["Config"].get("Env", []))
                checks["two_same_port_applications_run_in_six_limited_containers_without_host_ports_or_model_credentials"] = "PASSED"
                query(a, "put", "only-a-extra", "A-extra")
                def health(workspace):
                    script = "fetch('http://127.0.0.1:3000/health').then(r=>r.json()).then(v=>console.log(JSON.stringify(v)))"
                    return json.loads(runtime.docker("exec", application.name(workspace), "node", "-e", script).stdout)
                assert health(a)["items"] == 3 and health(b)["items"] == 1
                for req in reqs: action(req, "app_check")
                checks["http_handlers_query_their_own_real_postgres_dataset"] = "PASSED"
                logged = action(reqs[0], "app_logs")["result"]["application_logs"]
                assert "Sample application started" in logged and private_a not in logged
                action(reqs[0], "app_stop")
                assert not application.checked_container(data, a) and health(b)["healthy"]
                assert query(a, "get") and dependencies.checked_container(data, a, dependencies.read(a, dependencies.STATE))["State"]["Running"]
                action(reqs[0], "app_start")
                checks["service_logs_and_individual_stop_preserve_database_and_other_requirement"] = "PASSED"
                (a / "repo/m8-note.txt").write_text("changed source baseline\n")
                action(reqs[0], "app_check", expected="FAILED")
                assert detail(reqs[0])["application"]["phase"] == "STALE" and detail(reqs[0])["workspace"]["environment"] == "RUNNING"
                action(reqs[0], "app_stop"); action(reqs[0], "app_start")
                action(reqs[1], "app_stop")
                server_file = b / "repo/server.cjs"; original_server = server_file.read_text(); server_file.write_text("process.exit(23);\n" + original_server)
                action(reqs[1], "app_start", expected="FAILED")
                assert detail(reqs[1])["application"]["last_check"]["phase"] == "EXITED"
                action(reqs[1], "app_stop"); server_file.write_text(original_server); action(reqs[1], "app_start")
                checks["changed_code_and_exited_process_fail_checks_without_replay_or_database_loss"] = "PASSED"
                # Lose the app journal as well: cleanup must still identify and stop this writer.
                (a / application.JOURNAL).unlink()
            # Test owned orphan cleanup: stop the writer before collecting its database snapshot.
            (a / "runtime.json").unlink(); (a / dependencies.JOURNAL).unlink(); action(reqs[0], "stop")
            assert detail(reqs[0])["dependency"]["phase"] == "STOPPED" and dependencies.snapshot_info(a, True)
            assert dependencies.inspect("volume", dependencies.names(a)["volume"])
            action(reqs[0], "start"); assert {row["name"]: row["value"] for row in query(a, "get")}["only-a"] == "A"
            if args.application:
                assert not application.checked_container(data, a)
                action(reqs[0], "app_start"); assert health(a)["items"] == 3
                checks["orphan_application_is_stopped_before_checkpoint_and_restart_uses_retained_data"] = "PASSED"
            checks["orphan_writer_stop_checkpoints_data_and_restart_preserves_it"] = "PASSED"
            runtime.docker("stop", dependencies.names(b)["container"])
            action(reqs[1], "reconcile")
            assert detail(reqs[1])["dependency"]["phase"] == "STOPPED"
            action(reqs[1], "start"); assert query(b, "get") == [{"name": "only-b", "value": "B"}]
            if args.application: action(reqs[1], "app_start")
            checks["explicit_reconciliation_recovers_stopped_database_without_losing_data"] = "PASSED"
            page.get_by_role("button", name="需求工作区", exact=False).click()
            expect(page.locator(".requirement-card").filter(has_text="M7 独立需求 A")).to_be_visible(timeout=10000)
            page.locator(".requirement-card").filter(has_text="M7 独立需求 A").click()
            expect(page.locator(".dependency-status")).to_contain_text("独立 PostgreSQL 17")
            if args.application:
                expect(page.locator(".application-panel")).to_contain_text("上次 HTTP 检查通过")
                with page.expect_response(lambda response: response.url.endswith("/actions") and response.request.method == "POST") as receipt:
                    page.get_by_role("button", name="检查 HTTP 响应", exact=True).click()
                assert receipt.value.status == 202
                browser_job = receipt.value.json()
                for _ in range(100):
                    completed = client.get("/jobs/" + browser_job["id"]).json()
                    if completed["status"] not in ("QUEUED", "RUNNING"): break
                    time.sleep(.1)
                assert completed["status"] == "SUCCEEDED"
                checks["browser_saves_application_config_and_runs_real_http_check"] = "PASSED"
            page.screenshot(path=str(root / "dependencies-desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844}); assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "dependencies-mobile.png"), full_page=True); assert not errors, errors
            browser.close(); checks["browser_dependency_status_and_mobile_layout"] = "PASSED"
        action(reqs[0], "stop"); action(reqs[1], "stop"); terminate(); stop(settings)
        backed_up = backup(settings); report["backup_sha256"] = backed_up["sha256"]
        with zipfile.ZipFile(backed_up["file"]) as zipped:
            manifest = json.loads(zipped.read("manifest.json")); assert manifest["schema"] == "workbench-backup/v2"
            assert len([name for name in zipped.namelist() if name.endswith("postgres.dump")]) == 2
            assert not any(name.endswith(dependencies.PRIVATE) or name.endswith(dependencies.JOURNAL) for name in zipped.namelist())
            if args.application:
                assert len([name for name in zipped.namelist() if name.endswith(application.REPORT)]) == 2
                assert not any(name.endswith(application.JOURNAL) for name in zipped.namelist())
        restore(Path(backed_up["file"]), restored); assert not resources.inventory(restored)
        checks["v2_backup_contains_two_data_dumps_without_authentication_or_auto_replay"] = "PASSED"
        launch("agent_workbench.server", restored); ready(); launch("agent_workbench.runner", restored)
        client.close(); client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + (restored / "access-token").read_text()}, trust_env=False, timeout=30)
        restored_paths = []
        for req in reqs:
            if args.application:
                assert detail(req)["application"]["phase"] == "STOPPED"
            action(req, "start"); restored_paths.append(Path(detail(req)["workspace"]["path"]))
        restored_a, restored_b = restored_paths
        assert {row["name"]: row["value"] for row in query(restored_a, "get")}["only-a"] == "A"
        assert {row["name"]: row["value"] for row in query(restored_b, "get")} == {"only-b": "B"}
        assert json.loads((restored_a / dependencies.PRIVATE).read_text())["password"] != private_a
        assert dependencies.names(restored_a)["volume"] != dependencies.names(a)["volume"]
        assert detail(reqs[0])["dependency"]["restored_from"]
        checks["fresh_directory_restores_both_datasets_to_new_volumes_and_new_credentials"] = "PASSED"
        if args.application:
            for req in reqs: action(req, "app_start"); action(req, "app_check")
            assert health(restored_a)["items"] == 3 and health(restored_b)["items"] == 1
            checks["restored_app_history_stays_stopped_until_explicit_start_with_new_database_credentials"] = "PASSED"
        assert snapshot(source) == original
        checks["original_code_unchanged"] = "PASSED"
        if args.legacy_backup:
            restore(args.legacy_backup, root / "legacy-restored")
            assert not resources.inventory(root / "legacy-restored")
            with zipfile.ZipFile(args.legacy_backup) as previous:
                previous_manifest = json.loads(previous.read("manifest.json"))
            report["previous_backup"] = {"format": previous_manifest["schema"], "schema_revision": previous_manifest["schema_revision"],
                "sha256": hashlib.sha256(args.legacy_backup.read_bytes()).hexdigest()}
            checks["previous_backup_restores_without_auto_replay"] = "PASSED"
        report["status"] = "PASSED_M8_APPLICATION_POSTGRES_RESTORE" if args.application else "PASSED_M7_POSTGRES_ISOLATION_RESTORE"
    finally:
        terminate()
        if client: client.close()
        for location in (data, restored, root / "legacy-restored"):
            if location.exists():
                try:
                    resources.stop_all(location)
                    for workspace in resources.known_workspaces(location):
                        state = dependencies.read(workspace, dependencies.STATE)
                        if not state: continue
                        volume = dependencies.inspect("volume", dependencies.names(workspace)["volume"])
                        if volume and dependencies.labels_match(volume, dependencies.owner_labels(location, workspace, state["data_id"]), True):
                            runtime.docker("volume", "rm", dependencies.names(workspace)["volume"])
                except Exception: report.setdefault("cleanup_requires_inspection", []).append(str(location))
        for handle in handles: handle.close()
        runtime.docker("image", "rm", image, check=False)
        if report["status"] == "RUNNING": report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
