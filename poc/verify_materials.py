"""M4 browser acceptance: real API/Runner/Docker, explicitly controlled reports."""
import argparse
import hashlib
import io
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

from agent_workbench import runtime
from agent_workbench.execution import Executor
from agent_workbench.runner import Runner
from agent_workbench.settings import Settings
from agent_workbench.store import Store
from agent_workbench.workspace import git, snapshot, write_json
from material_fixtures import ControlledMaterialExecutor, complete_code_fixture


PURPOSE = "controlled M4 material acceptance; no online model inference"


def fixture_runner(root, base):
    if json.loads((root / "qa-purpose.json").read_text())["purpose"] != PURPOSE:
        raise ValueError("Fixture Runner requires its dedicated acceptance directory")
    settings = Settings.load(root)
    runner = Runner(settings, base, executor=ControlledMaterialExecutor(settings))
    while True:
        try:
            worked = runner.run_once()
            if not worked:
                runner.watch_once(); time.sleep(0.4)
        except httpx.HTTPError:
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--fixture-runner", action="store_true")
    parser.add_argument("--api")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args(); root = args.root.resolve()
    if args.fixture_runner:
        return fixture_runner(root, args.api)
    if root.exists():
        raise ValueError("Use a new dedicated verification directory")
    root.mkdir(parents=True, mode=0o700)
    write_json(root / "qa-purpose.json", {"purpose": PURPOSE})
    env = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_DATABASE_URL": f"sqlite:///{root / 'workbench.db'}"}
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    processes, handles = [], []
    report = {"status": "RUNNING", "model_inference": False, "controlled_reports": True,
        "m3_prerequisite": "seeded controlled workflow reports; not an online model run", "checks": {}}
    client = None
    def launch(server=False):
        handle = (root / ("api.log" if server else "runner.log")).open("a"); handles.append(handle)
        cmd = [sys.executable, "-m", "agent_workbench.server", "--data-dir", str(root), "--port", str(port)] if server else [sys.executable, __file__, "--fixture-runner", "--root", str(root), "--api", base]
        process = subprocess.Popen(cmd, env=env, stdout=handle, stderr=subprocess.STDOUT); processes.append(process)
        return process
    def ready():
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/session", trust_env=False, timeout=1).status_code == 200: return
            except httpx.HTTPError: pass
            time.sleep(0.1)
        raise AssertionError("API did not start")
    try:
        server = launch(True); ready()
        source = root / "source"; shutil.copytree(Path(__file__).parent / "sample-project", source)
        git(source, "init", "-b", "main"); git(source, "config", "user.name", "M4 Controlled Fixture")
        git(source, "config", "user.email", "fixture@example.invalid"); git(source, "add", "--all")
        git(source, "-c", "commit.gpgsign=false", "commit", "-m", "controlled source fixture")
        original = snapshot(source)
        settings = Settings.load(root)
        # The parent process uses explicit free profiles for fixture preparation only.
        from dataclasses import replace
        settings = replace(settings, model=env["AWB_MODEL"], database_url=env["AWB_DATABASE_URL"])
        client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False)
        def post(path, data):
            response = client.post(path, json=data); response.raise_for_status(); return response.json()
        project = post("/projects", {"name": "M4 材料验收样例", "source_path": str(source)})
        preparation = Runner(settings, base)
        requirements = []
        for title in ("M4 材料闭环 · 受控验收", "M4 隔离对照需求"):
            requirement = post("/requirements", {"project_id": project["id"], "title": title, "description": PURPOSE, "acceptance": "验证材料与人工登记边界"})
            requirements.append(requirement)
            post(f"/requirements/{requirement['id']}/actions", {"action": "prepare"}); assert preparation.run_once()
        preparation.client.close()
        identity = requirements[0]["id"]; endpoint = "/requirements/" + identity; materials = endpoint + "/materials"
        detail = client.get(endpoint).json(); workspace = Path(detail["workspace"]["path"])
        store = Store(settings.database_url)
        original_flow = complete_code_fixture(store, settings, identity, workspace)
        store.engine.dispose(); launch()
        def state(): return client.get(materials).json()["current"]
        def wait_job(job_id):
            for _ in range(480):
                job = client.get("/jobs/" + job_id).json()
                if job["status"] not in ("QUEUED", "RUNNING"):
                    assert job["status"] == "SUCCEEDED", {"status": job["status"], "error": job["error"]}
                    return
                time.sleep(0.25)
            raise AssertionError("Material job timed out")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100})
            errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(base); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token)
                page.get_by_role("button", name="进入工作台", exact=True).click()
                page.locator(".requirement-card").filter(has_text=requirements[0]["title"]).click()
                panel = page.locator(".material-panel")
                panel.get_by_role("button", name="开始材料阶段", exact=True).click()
                expect(panel.get_by_role("button", name="生成概要设计", exact=True)).to_be_visible(timeout=15000)
                def submit_button(button):
                    with page.expect_response(lambda response: response.url.endswith(materials + "/steps") and response.request.method == "POST") as receipt:
                        button.click()
                    assert receipt.value.status == 202, receipt.value.text()
                    wait_job(receipt.value.json()["id"])
                def generate(name):
                    submit_button(panel.get_by_role("button", name=name, exact=True))
                def wait_stage(label):
                    expect(panel.locator(".section-title").first.locator(".small-label")).to_have_text(label, timeout=15000)
                def record(name, decision=None, cases=(), feedback=False):
                    panel.get_by_role("button", name=name, exact=True).click()
                    dialog = page.get_by_role("dialog")
                    if name in ("登记主管评审", "补充主管记录"):
                        dialog.get_by_label("评审人", exact=True).fill("受控主管姓名")
                        dialog.get_by_label("主管结论", exact=True).select_option(decision or "APPROVED")
                    else:
                        dialog.get_by_label("登记人员", exact=True).fill("受控登记人")
                    if name in ("登记性能测试判断", "调整性能判断"):
                        dialog.get_by_label("是否需要性能测试", exact=True).select_option(decision)
                    if cases:
                        dialog.get_by_label("实际测试环境", exact=True).fill("受控验收环境，非业务性能测试")
                        for case_id in cases:
                            dialog.get_by_label("登记 " + case_id, exact=True).check()
                            dialog.get_by_label("结果 · " + case_id, exact=True).select_option("PASS")
                            dialog.get_by_label("实际结果 · " + case_id, exact=True).fill("受控登记通过，仅验证记录链路")
                            dialog.get_by_label("用例证据 · " + case_id, exact=True).fill("fixture://controlled-case")
                    if feedback:
                        dialog.get_by_label("反馈类型", exact=True).select_option("BUG")
                        dialog.get_by_label("修复沿用原已确认范围与方案，直接进入开发", exact=True).check()
                    dialog.get_by_label("意见与说明", exact=True).fill("受控验收意见，非实际业务审批或测试证据")
                    dialog.get_by_label("证据引用", exact=True).fill("fixture://controlled-reference")
                    submit_button(dialog.get_by_role("button", name="保存记录", exact=True))
                    expect(dialog).not_to_be_visible()
                def edit_summary(marker):
                    panel.get_by_role("button", name="编辑概要设计", exact=True).click()
                    dialog = page.get_by_role("dialog")
                    dialog.get_by_label("设计正文（Markdown）", exact=True).fill("## 人工修订\n" + marker)
                    dialog.get_by_label("修订原因", exact=True).fill("验证不可变文档版本")
                    submit_button(dialog.get_by_role("button", name="保存材料新版本", exact=True))
                    expect(dialog).not_to_be_visible()
                generate("生成概要设计"); wait_stage("等待主管评审")
                report["checks"]["browser_generate_summary_without_implicit_approval"] = "PASSED"
                edit_summary("受控修订 v2")
                record("登记主管评审"); wait_stage("性能判断待明确")
                record("登记性能测试判断", "UNDECIDED"); wait_stage("性能判断待明确")
                expect(panel.get_by_role("button", name="生成功能测试单", exact=True)).not_to_be_visible()
                report["checks"]["browser_undecided_performance_cannot_skip"] = "PASSED"
                record("登记性能测试判断", "NOT_REQUIRED"); wait_stage("等待功能测试单")
                generate("生成功能测试单"); wait_stage("等待测试执行记录")
                assert state()["test_status"] == "NOT_RUN"
                expect(panel.get_by_role("button", name="确认材料阶段验收", exact=True)).not_to_be_visible()
                report["checks"]["generated_test_definition_has_no_execution_result"] = "PASSED"
                record("登记测试执行", cases=("F1",)); wait_stage("等待材料阶段验收")
                record("确认材料阶段验收"); wait_stage("材料阶段已验收")
                report["checks"]["browser_records_evidence_and_material_acceptance"] = "PASSED"
                generate("导出当前材料包")
                link = panel.get_by_role("link", name="下载材料包", exact=False)
                expect(link).to_be_visible(timeout=15000)
                with page.expect_download() as downloaded: link.click()
                download_path = root / "downloaded-materials.zip"; downloaded.value.save_as(download_path)
                package = download_path.read_bytes(); original_hash = hashlib.sha256(package).hexdigest()
                with zipfile.ZipFile(io.BytesIO(package)) as zipped:
                    manifest = json.loads(zipped.read("manifest.json"))
                    assert manifest["status_at_export"] == "ACCEPTED" and manifest["performance_decision"] == "NOT_REQUIRED" and manifest["performance_skip_reason"]
                    for entry in manifest["entries"]: assert hashlib.sha256(zipped.read(entry["path"])).hexdigest() == entry["sha256"]
                original_export = state()["exports"][0]
                report["checks"]["browser_download_zip_hashes_and_reasoned_performance_skip"] = "PASSED"
                edit_summary("受控修订 v3，使旧评审与测试失效"); wait_stage("等待主管评审")
                assert not state()["accepted"] and state()["test_status"] == "NOT_RUN"
                assert client.get(materials + "/exports/" + original_export["id"]).content == package
                report["checks"]["manual_document_revision_invalidates_records_preserving_export"] = "PASSED"
                record("登记主管评审"); record("登记性能测试判断", "REQUIRED")
                generate("生成功能测试单"); wait_stage("等待性能测试单")
                generate("生成性能测试单"); wait_stage("等待测试执行记录")
                record("登记测试执行", cases=("F1",)); wait_stage("等待测试执行记录")
                assert state()["test_status"] == "NOT_RUN"
                record("登记测试执行", cases=("P1",)); wait_stage("等待材料阶段验收")
                record("确认材料阶段验收"); wait_stage("材料阶段已验收")
                report["checks"]["required_performance_case_blocks_until_registered"] = "PASSED"
                page.screenshot(path=str(root / "desktop.png"), full_page=True)
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(root / "mobile.png"), full_page=True)
                report["checks"]["mobile_layout_without_horizontal_overflow"] = "PASSED"
                old_material_id = state()["id"]
                old_versions = [item["id"] for item in state()["version_history"]]
                # Explicitly open a second materials round to exercise historical export.
                post(materials, {"flow_id": original_flow["id"], "digest": original_flow["basis_digest"], "reason": "受控检查历史轮次与下载"})
                expect(panel.get_by_role("button", name="生成概要设计", exact=True)).to_be_enabled(timeout=15000)
                history = panel.locator("details").filter(has=page.get_by_text("材料轮次历史 · 2", exact=True))
                history.locator("summary").click()
                history.get_by_role("button", name="查看材料轮次", exact=True).last.click()
                archive_dialog = page.get_by_role("dialog")
                expect(archive_dialog.get_by_role("heading", name="历史材料轮次", exact=True)).to_be_visible()
                previous_downloads = archive_dialog.get_by_role("link", name="下载历史材料包", exact=False).count()
                submit_button(archive_dialog.get_by_role("button", name="导出此历史轮次", exact=True))
                expect(archive_dialog.get_by_role("link", name="下载历史材料包", exact=False)).to_have_count(previous_downloads + 1, timeout=15000)
                historical_link = archive_dialog.get_by_role("link", name="下载历史材料包", exact=False).last
                expect(historical_link).to_be_visible(timeout=15000)
                with page.expect_download() as historic_download: historical_link.click()
                historic_path = root / "downloaded-historical.zip"; historic_download.value.save_as(historic_path)
                with zipfile.ZipFile(historic_path) as zipped:
                    assert json.loads(zipped.read("manifest.json"))["status_at_export"] == "STALE"
                archive_dialog.get_by_role("button", name="关闭", exact=True).click()
                report["checks"]["browser_historical_round_export_remains_downloadable"] = "PASSED"
                record("登记反馈", feedback=True); wait_stage("需重新确认")
                flow = client.get(endpoint + "/workflow").json()["current"]
                assert flow["id"] != original_flow["id"] and flow["next_step"] == "development"
                report["checks"]["browser_bug_feedback_opens_explicit_scoped_fix_round"] = "PASSED"
                report["console_errors"] = errors; assert not errors
            except Exception:
                page.screenshot(path=str(root / "failure.png"), full_page=True)
                raise
            finally:
                browser.close()
        roles = json.loads((root / "controlled-material-roles.json").read_text())
        assert len({item["workspace"] for item in roles}) == len(roles) and all(item["write_denied"] for item in roles)
        assert all(item["code_digest"] == original["tree_digest"] for item in roles)
        report["checks"]["real_docker_document_roles_are_separate_read_only_snapshots"] = "PASSED"
        second = client.get("/requirements/" + requirements[1]["id"]).json()
        assert snapshot(source) == original and snapshot(Path(second["workspace"]["path"]) / "repo")["tree_digest"] == original["tree_digest"]
        report["checks"]["source_and_other_requirement_unchanged"] = "PASSED"
        before = state(); server.terminate(); server.wait(timeout=10); launch(True); ready()
        after = state()
        assert after["stage"] == "STALE" and len(after["records"]) == len(before["records"]) and len(after["version_history"]) == len(before["version_history"])
        assert hashlib.sha256(client.get(materials + "/exports/" + original_export["id"]).content).hexdigest() == original_hash
        historical = client.get(materials + "?round_id=" + old_material_id).json()["current"]
        assert [item["id"] for item in historical["version_history"]] == old_versions
        report["checks"]["api_restart_preserves_versions_records_and_download"] = "PASSED"
        report.update(status="PASSED_CONTROLLED_M4_MATERIALS", migration="0004", roles=roles, downloaded_package_sha256=original_hash,
            actual_online_model_inference="NOT_ATTEMPTED", actual_business_or_performance_test="NOT_EXECUTED; test results are controlled registrations")
    finally:
        if client: client.close()
        for process in reversed(processes):
            if process.poll() is None: process.terminate(); process.wait(timeout=10)
        for handle in handles: handle.close()
        for path in [*root.glob("workspaces/*/*/runtime.json"), *root.glob("attempts/*/*/*/runtime.json")]: runtime.stop(path.parent)
        if report["status"] == "RUNNING": report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
