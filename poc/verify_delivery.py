"""M5 browser/Docker/restore acceptance, explicitly controlled workflow reports."""
import argparse
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from agent_workbench import resources, runtime
from agent_workbench.checked_archive import verify
from agent_workbench.delivery import Delivery
from agent_workbench.execution import Executor
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.materials import Materials
from agent_workbench.runner import Runner
from agent_workbench.settings import Settings
from agent_workbench.store import Store, jobs
from agent_workbench.workspace import create_workspace, git, snapshot, write_json
from material_fixtures import complete_code_fixture, evidence, sample_document


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new dedicated verification directory")
    root.mkdir(parents=True, mode=0o700)
    data = root / "data"; restored_root = root / "restored"
    env = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_DATABASE_URL": f"sqlite:///{data / 'workbench.db'}"}
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    processes, handles = [], []
    report = {"status": "RUNNING", "model_inference": False, "controlled_reports": True, "external_business_tests_executed": False, "checks": {}}
    checks = report["checks"]
    def launch(module, location):
        local_env = {**env, "AWB_DATABASE_URL": f"sqlite:///{location / 'workbench.db'}"}
        handle = (root / (module.rsplit(".", 1)[-1] + ".log")).open("a"); handles.append(handle)
        command = [sys.executable, "-m", module, "--data-dir", str(location)]
        command += ["--port", str(port)] if module.endswith("server") else ["--api", base]
        process = subprocess.Popen(command, env=local_env, stdout=handle, stderr=subprocess.STDOUT); processes.append(process)
        return process
    def ready():
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/session", timeout=1, trust_env=False).status_code == 200: return
            except httpx.HTTPError: pass
            time.sleep(.1)
        raise AssertionError("API did not start")
    def terminate():
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    store = None; client = None
    try:
        launch("agent_workbench.server", data); ready()
        settings = replace(Settings.load(data), model=env["AWB_MODEL"], database_url=env["AWB_DATABASE_URL"])
        client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=30)
        def post(path, payload):
            response = client.post(path, json=payload); response.raise_for_status(); return response.json()
        store = Store(settings.database_url); executor = Executor(settings); runner = Runner(settings, base)
        requirements, sources, originals = [], [], []
        for index in range(2):
            source = root / f"source-{index}"; shutil.copytree(Path(__file__).parent / "sample-project", source)
            git(source, "init", "-b", "main"); git(source, "config", "user.name", "M5 Controlled Fixture"); git(source, "config", "user.email", "fixture@example.invalid")
            git(source, "add", "--all"); git(source, "-c", "commit.gpgsign=false", "commit", "-m", "controlled source")
            sources.append(source); originals.append(snapshot(source))
            project = post("/projects", {"name": f"M5 样例服务 {index + 1}", "source_path": str(source)})
            req = post("/requirements", {"project_id": project["id"], "title": f"跨服务需求 {index + 1}", "description": "controlled M5 fixture", "acceptance": "受控交付验证"})
            post(f"/requirements/{req['id']}/actions", {"action": "prepare"}); assert runner.run_once()
            req = client.get(f"/requirements/{req['id']}").json(); workspace = Path(req["workspace"]["path"])
            (workspace / "repo/new-binary.bin").write_bytes(bytes(range(128)) * 2)
            (workspace / "repo/server.mjs").write_text((workspace / "repo/server.mjs").read_text() + "\n// controlled M5 delivery change\n")
            post(f"/requirements/{req['id']}/actions", {"action": "refresh"}); assert runner.run_once()
            flow = complete_code_fixture(store, settings, req["id"], workspace)
            materials = Materials(store); materials.start(req["id"], {"flow_id": flow["id"], "digest": flow["basis_digest"], "reason": "controlled M5 prerequisite"}, None)
            def step(key, payload=None):
                current = materials.read(req["id"])["current"]
                materials.enqueue(req["id"], {"round_id": current["id"], "context_digest": current["context_digest"], "step_key": key, "data": payload or {}, "prompt": ""}, None)
                job = store.claim("m5-controlled-materials")
                result = {**executor.observe(workspace), "material_output": sample_document(key)} if key in ("summary_design", "test_plan") else executor.execute(job, lambda *_: None, threading.Event())
                store.finish(job["id"], "m5-controlled-materials", job["epoch"], "SUCCEEDED", result, None)
                assert store.get(jobs, job["id"])["status"] == "SUCCEEDED"
            step("summary_design"); step("supervisor_review", evidence(decision="APPROVED")); step("performance_decision", evidence(decision="NOT_REQUIRED")); step("test_plan")
            state = materials.read(req["id"])["current"]
            step("test_execution", evidence(environment="受控登记环境", results=[{"version_id": state["cases"][0]["version_id"], "case_id": "F1", "status": "PASS", "actual": "受控测试记录，未执行真实业务联调", "evidence": "fixture://m5-case"}]))
            step("acceptance", evidence()); step("export")
            requirements.append(req)
            executor.start_runtime(workspace, project["image"])
        runner.client.close()
        assert len(resources.inventory(data)) == 2
        checks["two_real_member_containers"] = "PASSED"
        launch("agent_workbench.runner", data)
        def wait_job(identity):
            for _ in range(160):
                value = client.get("/jobs/" + identity).json()
                if value["status"] not in ("QUEUED", "RUNNING"):
                    assert value["status"] == "SUCCEEDED", value.get("error"); return
                time.sleep(.25)
            raise AssertionError("Delivery job timed out")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100}); errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token); page.get_by_role("button", name="进入工作台", exact=True).click()
            page.get_by_role("button", name="跨项目交付", exact=True).click(); page.get_by_role("button", name="＋ 创建交付", exact=True).click()
            dialog = page.get_by_role("dialog"); dialog.get_by_label("交付名称", exact=True).fill("M5 双服务受控交付")
            for index, req in enumerate(requirements):
                dialog.get_by_role("button", name="添加成员", exact=True).click(); dialog.get_by_label(f"第 {index + 1} 个需求", exact=True).select_option(req["id"])
            dialog.get_by_label("登记人", exact=True).fill("受控验收人"); dialog.get_by_label("操作原因", exact=True).fill("验证跨项目范围，非业务审批")
            with page.expect_response(lambda response: response.url.endswith("/deliveries") and response.request.method == "POST") as receipt:
                dialog.get_by_role("button", name="确认提交", exact=True).click()
            assert receipt.value.status == 201, receipt.value.text(); identity = receipt.value.json()["id"]; endpoint = "/deliveries/" + identity
            capture = page.get_by_role("button", name="采集交付包", exact=True)
            expect(capture).to_be_disabled(); checks["browser_create_ordered_cross_project_scope"] = "PASSED"
            def form_action(button, suffix, fill=None):
                page.get_by_role("button", name=button, exact=True).click(); form = page.get_by_role("dialog")
                if fill: fill(form)
                form.get_by_label("登记人", exact=True).fill("受控验收人"); form.get_by_label("证据或记录位置", exact=True).fill("fixture://m5-controlled-evidence")
                form.get_by_label("结论理由" if suffix == "integration" else "操作原因", exact=True).fill("受控验证登记，非实际主管结论或业务测试")
                with page.expect_response(lambda response: response.url.endswith(endpoint + "/" + suffix) and response.request.method == "POST") as received:
                    form.get_by_role("button", name="确认提交", exact=True).click()
                assert received.value.status in (200, 201, 202), received.value.text()
                if received.value.status == 202: wait_job(received.value.json()["id"])
            form_action("确认范围", "scope"); expect(capture).to_be_disabled()
            expect(page.get_by_role("button", name="登记联调", exact=True)).to_be_enabled()
            form_action("登记联调", "integration", lambda form: (form.get_by_label("实际环境", exact=True).fill("受控联调记录环境"), form.get_by_label("实际结果", exact=True).fill("仅验证登记门禁，未执行业务联调")))
            expect(capture).to_be_enabled(); checks["browser_explicit_scope_and_integration_gates"] = "PASSED"
            form_action("采集交付包", "operations")
            assert not resources.inventory(data)
            checks["capture_stops_both_real_containers"] = "PASSED"
            download = page.get_by_role("link", name="下载交付包 · 当前有效", exact=True).first
            expect(download).to_be_visible()
            with page.expect_download() as downloaded: download.click()
            downloaded.value.save_as(root / "browser-delivery.zip")
            exported = Delivery(store).read(identity)["exports"][0]
            archive = root / "browser-delivery.zip"; manifest = verify(archive, exported)
            with zipfile.ZipFile(archive) as zipped:
                for member, source in zip(requirements, sources):
                    prefix = "projects/" + member["project_id"]
                    patch_path = root / (member["project_id"] + ".patch"); patch_path.write_bytes(zipped.read(prefix + "/changes.patch"))
                    git(source, "apply", "--check", str(patch_path))
                    assert zipped.read(prefix + "/code/new-binary.bin") == bytes(range(128)) * 2
                    history = json.loads(zipped.read(prefix + "/history.json")); assert history["workflow_history"] and history["material_versions"] and history["material_records"]
            assert len(manifest["projects"]) == 2
            checks["browser_full_export_hashes_binary_patch_applies_and_history"] = "PASSED"
            form_action("归档交付", "operations")
            expect(page.get_by_role("button", name="重新打开", exact=True)).to_be_enabled()
            assert client.post(f"/requirements/{requirements[1]['id']}/actions", json={"action": "task", "prompt": "blocked"}).status_code == 409
            form_action("重新打开", "reopen"); expect(page.get_by_role("button", name="归档交付", exact=True)).to_be_enabled()
            checks["browser_archive_and_explicit_reopen"] = "PASSED"
            page.screenshot(path=str(root / "delivery-desktop.png"), full_page=True)
            page.get_by_role("button", name="资源与恢复", exact=True).click()
            expect(page.get_by_role("heading", name="离线维护", exact=True)).to_be_visible()
            expect(page.get_by_text("没有托管容器。", exact=True)).to_be_visible(timeout=15000)
            page.screenshot(path=str(root / "resources-desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "resources-mobile.png"), full_page=True)
            assert not errors, errors
            checks["browser_resources_and_mobile_without_overflow"] = "PASSED"
            browser.close()
        terminate()
        # Create an owned readonly attempt, remove only its fixture journal, and prove orphan cleanup.
        attempt = create_workspace(sources[0], data / "attempts" / ("job_" + "a" * 32), requirements[0]["id"], requirements[0]["project_id"])
        attempt_path = Path(attempt["workspace"])
        executor.start_runtime(attempt_path, requirements[0]["project"]["image"], read_only_source=True, opencode_model="opencode/glm-5.3-flash-free")
        (attempt_path / "runtime.json").unlink()
        assert resources.inventory(data)[0]["orphan"]
        external_network = "awb-m5-external-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
        runtime.docker("network", "create", external_network)
        try:
            runtime.docker("network", "connect", external_network, resources.inventory(data)[0]["id"])
            stopped = stop(settings); assert stopped["stopped_containers"] == 1 and not resources.inventory(data)
            assert runtime.docker("network", "inspect", external_network, check=False).returncode == 0
        finally: runtime.docker("network", "rm", external_network, check=False)
        checks["offline_owned_orphan_cleanup_preserves_source_and_evidence"] = "PASSED"
        checks["cleanup_preserves_attached_external_network"] = "PASSED"
        auth = attempt_path / "agent-home/.local/share/opencode/auth.json"; auth.parent.mkdir(parents=True, exist_ok=True); auth.write_text('"private controlled authentication fixture"')
        before_group = Delivery(store).read(identity)
        result = backup(settings); report["backup_sha256"] = result["sha256"]
        with zipfile.ZipFile(result["file"]) as zipped:
            assert not any("agent-home" in name or name.endswith("access-token") or name.endswith("runner-token") or name.endswith("runtime.json") for name in zipped.namelist())
        restore_info = restore(Path(result["file"]), restored_root)
        assert restore_info["credentials_regenerated"] and not restore_info["sessions_replayed"]
        assert (restored_root / "access-token").read_text() != settings.access_token
        checks["backup_restore_all_code_role_copies_exports_and_fresh_credentials"] = "PASSED"
        launch("agent_workbench.server", restored_root); ready()
        restored_settings = replace(Settings.load(restored_root), database_url=f"sqlite:///{restored_root / 'workbench.db'}")
        with httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + restored_settings.access_token}, trust_env=False) as restored_client:
            state = restored_client.get(endpoint).json()
            assert state["ready"] and len(state["exports"]) == len(before_group["exports"])
            assert restored_client.get(endpoint + "/exports/" + exported["id"]).content == archive.read_bytes()
            for req in requirements:
                detail = restored_client.get(f"/requirements/{req['id']}").json(); path = Path(detail["workspace"]["path"])
                assert path.is_relative_to(restored_root)
                assert snapshot(path / "repo")["tree_digest"] == detail["workspace"]["snapshot"]["tree_digest"]
                assert restored_client.get(f"/requirements/{req['id']}/materials").json()["current"]["accepted"]
            assert restored_client.get("/runtime").json()["jobs"] == {"QUEUED": 0, "RUNNING": 0, "UNKNOWN": 0}
            assert restored_client.get("/runtime").json()["restored"]["credentials_regenerated"]
        checks["fresh_api_reads_restored_gates_code_and_original_download_without_replay"] = "PASSED"
        assert all(snapshot(path) == original for path, original in zip(sources, originals))
        checks["external_original_repositories_unchanged"] = "PASSED"
        with store.engine.connect() as connection: report["migration"] = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        report["delivery_sha256"] = exported["sha256"]
        report["status"] = "PASSED_M5_CONTROLLED_DELIVERY_RESTORE"
    finally:
        terminate()
        if client: client.close()
        if store: store.engine.dispose()
        for location in (data, restored_root):
            if location.exists():
                try: resources.stop_all(location)
                except Exception: report.setdefault("cleanup_requires_inspection", []).append(str(location))
        for handle in handles: handle.close()
        if report["status"] == "RUNNING": report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
