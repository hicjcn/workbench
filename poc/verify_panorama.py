"""Real multi-repository visibility, isolation, cancellation and restore; no inference."""
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

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench import resources
from agent_workbench.checked_archive import file_hash, verify
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.settings import Settings
from agent_workbench.workspace import git, snapshot, write_json


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, required=True); parser.add_argument("--legacy-backup", type=Path)
    parser.add_argument("--chromium", default="/usr/bin/chromium"); args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new dedicated verification directory")
    root.mkdir(mode=0o700, parents=True); data = root / "data"; restored = root / "restored"
    with socket.socket() as reservation: reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"; handles, processes = [], []; client = None
    report = {"status": "RUNNING", "model_inference": False, "model_report_generated": False, "user_business_project_tested": False, "checks": {}}
    checks = report["checks"]
    def launch(module, location):
        handle = (root / (module.rsplit(".", 1)[-1] + ".log")).open("a"); handles.append(handle)
        env = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_MAX_CONTAINERS": "1", "AWB_DATABASE_URL": f"sqlite:///{location / 'workbench.db'}"}
        command = [sys.executable, "-m", module, "--data-dir", str(location)] + (["--port", str(port)] if module.endswith("server") else ["--api", base])
        processes.append(subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT))
    def terminate():
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    def ready():
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/session", trust_env=False, timeout=1).status_code == 200: return
            except httpx.HTTPError: pass
            time.sleep(.1)
        raise AssertionError("API did not become ready")
    def post(path, payload):
        response = client.post(path, json=payload); response.raise_for_status(); return response.json()
    def wait(job, expected="SUCCEEDED"):
        for _ in range(600):
            current = client.get("/jobs/" + job["id"]).json()
            if current["status"] not in ("QUEUED", "RUNNING"):
                assert current["status"] == expected, {"status": current["status"], "error": current.get("error")}; return current
            time.sleep(.2)
        raise AssertionError("Job timed out")
    def evidence(**extra): return {"actor": "样例验收人", "reason": "真实多仓库只读边界验证", "occurred_at": "2026-10-25T00:00:00Z", "evidence": "local-sample://panorama", **extra}
    try:
        sources, requirements, original = [], [], []
        launch("agent_workbench.server", data); ready()
        settings = replace(Settings.load(data), model="opencode/glm-5.3-flash-free", max_containers=1, database_url=f"sqlite:///{data / 'workbench.db'}")
        client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=30)
        launch("agent_workbench.runner", data)
        for index, name in enumerate(("订单服务", "库存服务", "候选通知服务")):
            source = root / f"source-{index}"; source.mkdir(); (source / "server.mjs").write_text(f"export const project = {index};\n// sample project {name}\n")
            (source / ".env").write_text("SAMPLE_EXCLUDED=private-fixture\n"); (source / "credentials").mkdir(); (source / "credentials/token").write_text("excluded-fixture\n")
            git(source, "init", "-b", "main"); git(source, "config", "user.name", "M9 Sample"); git(source, "config", "user.email", "sample@example.invalid")
            git(source, "add", "--all"); git(source, "-c", "commit.gpgsign=false", "commit", "-m", "Candidate sample")
            sources.append(source); original.append(snapshot(source))
            project = post("/projects", {"name": name, "source_path": str(source)})
            requirement = post("/requirements", {"project_id": project["id"], "title": "联合分析样例 - " + name})
            wait(post(f"/requirements/{requirement['id']}/actions", {"action": "prepare"})); requirements.append(requirement)
        group = post("/deliveries", {"title": "M9 订单与库存候选范围", "description": "样例需求：订单需要同步核实库存影响，通知服务作为额外候选。",
            "members": [{"requirement_id": req["id"], "required": index < 2, "skip_reason": "只作为候选检查" if index == 2 else ""} for index, req in enumerate(requirements)],
            "actor": "样例验收人", "reason": "固定全部候选项目", "expected_revision": None})
        group_id = group["id"]
        checks["three_explicit_candidates_include_optional_project_without_starting_development"] = "PASSED"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100}); errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token); page.get_by_role("button", name="进入工作台", exact=True).click()
            page.get_by_role("button", name="跨项目交付", exact=False).click()
            page.get_by_role("button", name="检查全景分析环境", exact=True).click()
            dialog = page.get_by_role("dialog"); dialog.get_by_label("登记人", exact=True).fill("样例验收人"); dialog.get_by_label("操作原因", exact=True).fill("真实只读挂载检查，不调用模型")
            with page.expect_response(lambda response: response.url.endswith("/panorama") and response.request.method == "POST") as receipt: dialog.get_by_role("button", name="确认提交", exact=True).click()
            assert receipt.value.status == 202; finished = wait(receipt.value.json())
            output = finished["result"]["panorama_output"]
            assert output["model_execution"] == "NOT_ATTEMPTED" and not output.get("report") and len(output["coverage"]) == 3
            assert all(item["readonly"] and item["readable"] and item["samples_match"] for item in output["coverage"])
            checks["browser_runs_one_real_readonly_container_over_all_three_repositories"] = "PASSED"
            state = client.get("/deliveries/" + group_id).json()
            assert not state["scope_ready"] and not state["scope_approved"] and state["panorama"]["analysis"] is None
            assert state["panorama"]["environment"]["code_current"]
            checks["environment_success_never_becomes_analysis_or_scope_approval"] = "PASSED"
            role_configs = list((data / "attempts" / finished["id"]).glob("*/*/agent-home/.config/opencode/opencode.json"))
            assert len(role_configs) == 1
            config = json.loads(role_configs[0].read_text()); permissions = config["permission"]
            expected_targets = {item["target"] + "/*" for item in output["coverage"]}
            assert set(permissions["external_directory"]) == expected_targets and permissions["*"] == "deny"
            for manifest in (data / "attempts" / finished["id"]).glob("*/*/workspace.json"):
                workspace = manifest.parent
                assert not (workspace / "repo/.env").exists() and not (workspace / "repo/credentials").exists()
                assert "runtime_profile" not in json.loads(manifest.read_text())
            assert not resources.inventory(data)
            checks["candidate_snapshots_exclude_credentials_and_have_scoped_external_directory_permissions"] = "PASSED"
            expect(page.locator(".panorama-panel")).to_contain_text("模型未调用", timeout=10000)
            page.screenshot(path=str(root / "panorama-desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844}); assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "panorama-mobile.png"), full_page=True); assert not errors, errors
            browser.close(); checks["desktop_and_mobile_show_environment_and_analysis_separately"] = "PASSED"
        state = client.get("/deliveries/" + group_id).json()
        job = post(f"/deliveries/{group_id}/panorama", evidence(kind="panorama_check", context_digest=state["panorama"]["context_digest"], model="opencode/glm-5.3-flash-free", prompt=""))
        for _ in range(300):
            current = client.get("/jobs/" + job["id"]).json()
            if current["status"] == "RUNNING" and resources.inventory(data): break
            time.sleep(.1)
        else: raise AssertionError("No running cancellation fixture")
        assert client.post(f"/requirements/{requirements[1]['id']}/actions", json={"action": "refresh"}).status_code == 409
        post("/jobs/" + job["id"] + "/cancel", {}); wait(job, "CANCELED")
        assert not resources.inventory(data)
        assert all(client.get("/requirements/" + req["id"]).json()["workspace"]["environment"] == "STOPPED" for req in requirements)
        assert len(client.get("/deliveries/" + group_id).json()["panorama"]["history"]) == 1
        checks["actual_running_cancel_stops_role_and_releases_all_candidate_reservations"] = "PASSED"
        assert all(snapshot(source) == before for source, before in zip(sources, original))
        checks["all_original_repositories_unchanged"] = "PASSED"
        terminate(); stop(settings); backed_up = backup(settings); report["backup_sha256"] = backed_up["sha256"]
        restore(Path(backed_up["file"]), restored); assert not resources.inventory(restored)
        launch("agent_workbench.server", restored); ready()
        client.close(); client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + (restored / "access-token").read_text()}, trust_env=False, timeout=30)
        recovered = client.get("/deliveries/" + group_id).json()
        assert recovered["panorama"]["environment"]["id"] == state["panorama"]["environment"]["id"] and not recovered["panorama"]["analysis"]
        assert not recovered["scope_approved"] and not resources.inventory(restored)
        checks["backup_restores_joint_environment_history_without_model_reports_or_auto_replay"] = "PASSED"
        if args.legacy_backup:
            old = verify(args.legacy_backup); info = restore(args.legacy_backup, root / "legacy-restored")
            assert info["sessions_replayed"] is False and not resources.inventory(root / "legacy-restored")
            report["previous_backup"] = {"format": old["schema"], "schema_revision": old["schema_revision"], "sha256": file_hash(args.legacy_backup)}
            checks["actual_m8_v2_archive_restores_without_automatic_containers"] = "PASSED"
        report["status"] = "PASSED_M9_MULTI_PROJECT_READONLY_ENVIRONMENT"
    finally:
        terminate()
        if client: client.close()
        for location in (data, restored, root / "legacy-restored"):
            if location.exists():
                try: resources.stop_all(location)
                except Exception: report.setdefault("cleanup_requires_inspection", []).append(str(location))
        for handle in handles: handle.close()
        if report["status"] == "RUNNING": report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
