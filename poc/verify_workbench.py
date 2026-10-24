"""Browser -> API -> database -> separate Runner -> real Docker acceptance."""
import argparse
import json
from pathlib import Path
import shutil
import time
import uuid

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench import runtime
from agent_workbench.workspace import git, snapshot, write_json


def verify_persistence(root, base_url, chromium):
    import sqlite3
    report = json.loads((root / "browser-verification.json").read_text())
    token = (root / "access-token").read_text().strip()
    with httpx.Client(base_url=base_url + "/api/v1", headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
        current = client.get("/requirements/" + report["requirements"][0]).json()
        assert current["workspace"]["environment"] == "STOPPED"
        assert current["workspace"]["approval"] == "STALE"
        assert len(current["reviews"]) == 1
        assert current["jobs"] and all(job["status"] in ("SUCCEEDED", "CANCELED") for job in current["jobs"])
        with sqlite3.connect(root / "workbench.db") as connection:
            report["migration"] = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert report["migration"] == "0004"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, args=["--no-sandbox"], headless=True)
        page = browser.new_page(viewport={"width":1500,"height":1100})
        page.goto(base_url)
        page.get_by_label("本机访问令牌").fill(token)
        page.get_by_role("button",name="进入工作台",exact=True).click()
        page.locator(".requirement-card").filter(has_text=report["titles"][0]).click()
        expect(page.get_by_text("需重新确认",exact=True)).to_be_visible()
        expect(page.locator(".log-row").first).to_be_visible()
        page.screenshot(path=str(root / "screenshots/desktop.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(root / "screenshots/mobile.png"),full_page=True)
        browser.close()
    report["checks"]["api_restart_and_migration_preserve_reviews_and_events"] = "PASSED"
    write_json(root / "browser-verification.json",report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


def verify_recovery(root, base_url, chromium):
    from sqlalchemy import insert
    from agent_workbench.store import Store, identifier, jobs
    report = json.loads((root / "browser-verification.json").read_text())
    identity = report["requirements"][0]
    database = Store(f"sqlite:///{root / 'workbench.db'}")
    unknown_id = identifier("job")
    with database.engine.begin() as connection:
        connection.execute(insert(jobs).values(id=unknown_id, requirement_id=identity, action="refresh",
            input_json="{}", status="UNKNOWN", cancel_requested=False, epoch=0,
            error="受控界面恢复用例", created_at=time.time(), updated_at=time.time()))
    database.engine.dispose()
    token = (root / "access-token").read_text().strip()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium,args=["--no-sandbox"],headless=True)
        page = browser.new_page(viewport={"width":1500,"height":1100})
        page.goto(base_url)
        page.get_by_label("本机访问令牌").fill(token)
        page.get_by_role("button",name="进入工作台",exact=True).click()
        page.locator(".requirement-card").filter(has_text=report["titles"][0]).click()
        recover = page.get_by_role("button",name="停止并核实",exact=True)
        expect(recover).to_be_enabled()
        recover.click()
        expect(page.get_by_role("button",name="停止环境",exact=True)).to_be_disabled(timeout=15000)
        browser.close()
    with httpx.Client(base_url=base_url + "/api/v1",headers={"Authorization":"Bearer " + token},trust_env=False) as client:
        current = client.get("/requirements/" + identity).json()
        assert next(job for job in current["jobs"] if job["id"]==unknown_id)["status"]=="CANCELED"
        assert current["jobs"][0]["action"]=="reconcile" and current["jobs"][0]["status"]=="SUCCEEDED"
    report["checks"]["browser_can_reconcile_unknown_with_stopped_environment"]="PASSED"
    report["recovery_case"]="controlled UNKNOWN record for UI recovery; no model task was fabricated"
    write_json(root / "browser-verification.json",report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8340")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--check-existing", action="store_true")
    parser.add_argument("--check-recovery", action="store_true")
    args = parser.parse_args()
    root = args.data_dir.resolve()
    if args.check_recovery:
        return verify_recovery(root,args.api,args.chromium)
    if args.check_existing:
        return verify_persistence(root, args.api, args.chromium)
    suffix = uuid.uuid4().hex[:6]
    source = root / "sources" / ("sample-" + suffix)
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).parent / "sample-project", source)
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "M2 Browser Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "add", "--all")
    git(source, "-c", "commit.gpgsign=false", "commit", "-m", "Browser acceptance fixture")
    original = snapshot(source)
    client = httpx.Client(base_url=args.api + "/api/v1", headers={"Authorization": "Bearer " + (root / "access-token").read_text().strip()}, trust_env=False)
    def detail(identity):
        response = client.get("/requirements/" + identity)
        response.raise_for_status()
        return response.json()
    def wait_job(identity, action):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = detail(identity)
            match = next((job for job in current["jobs"] if job["action"] == action), None)
            if match and match["status"] not in ("QUEUED", "RUNNING"):
                assert match["status"] == "SUCCEEDED", {"action":action,"status":match["status"],"error":match["error"]}
                return current
            time.sleep(0.3)
        raise AssertionError("Job did not finish: " + action)
    checks = {}
    requirement_ids = []
    report = {"model_inference_repeated": False, "checks": checks}
    screenshot_dir = root / "screenshots"
    screenshot_dir.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, args=["--no-sandbox"], headless=True)
        page = browser.new_page(viewport={"width":1500,"height":1100}, device_scale_factor=1)
        console_errors = []
        page.on("pageerror", lambda failure: console_errors.append(str(failure)))
        try:
            page.goto(args.api)
            page.get_by_label("本机访问令牌").fill((root / "access-token").read_text().strip())
            page.get_by_role("button", name="进入工作台", exact=True).click()
            expect(page.get_by_role("heading",name="每个需求，都有自己的工作区。")).to_be_visible()
            page.get_by_role("button",name="＋ 登记项目",exact=True).click()
            dialog = page.get_by_role("dialog")
            project_name = "验收示例服务 · " + suffix
            dialog.get_by_label("项目名称").fill(project_name)
            dialog.get_by_label("仓库绝对路径").fill(str(source))
            dialog.get_by_role("button",name="登记项目",exact=True).click()
            expect(dialog).not_to_be_visible()
            checks["browser_project_registration"] = "PASSED"
            titles = ["示例需求 A · 参数处理 · " + suffix, "示例需求 B · 独立回归 · " + suffix]
            for title in titles:
                page.get_by_role("button",name="＋ 新建需求",exact=True).click()
                dialog = page.get_by_role("dialog")
                dialog.get_by_label("需求名称").fill(title)
                dialog.get_by_label("关联项目").select_option(label=project_name)
                dialog.get_by_label("需求背景").fill("验证独立工作区、执行日志与人工确认闭环。")
                dialog.get_by_label("验收标准").fill("源码不互相覆盖；确认绑定代码快照；修改后重新确认。")
                dialog.get_by_role("button",name="创建需求",exact=True).click()
                expect(dialog).not_to_be_visible()
                expect(page.get_by_role("heading",name=title,exact=True)).to_be_visible()
                records = client.get("/requirements").json()
                identity = next(item["id"] for item in records if item["title"] == title)
                requirement_ids.append(identity)
                page.get_by_role("button",name="创建工作区",exact=True).click()
                wait_job(identity,"prepare")
            checks["browser_two_durable_workspaces"] = "PASSED"
            page.locator(".requirement-card").filter(has_text=titles[0]).click()
            expect(page.get_by_role("heading",name=titles[0],exact=True)).to_be_visible()
            page.get_by_role("button",name="启动环境",exact=True).click()
            first = wait_job(requirement_ids[0],"start")
            workspace = Path(first["workspace"]["path"])
            state = json.loads((workspace / "runtime.json").read_text())
            checks["browser_api_runner_real_opencode_container"] = "PASSED"
            # This is a deliberate human-style edit for review invalidation, not model output.
            runtime.docker("exec",state["container"],"node","-e","require('node:fs').appendFileSync('server.mjs', '\\n// M2 manual review fixture\\n')")
            page.get_by_role("button",name="刷新差异",exact=True).click()
            first = wait_job(requirement_ids[0],"refresh")
            expect(page.get_by_label("代码差异")).to_contain_text("M2 manual review fixture")
            page.get_by_label("审查意见").fill("已检查此验收样例的当前代码快照。")
            page.get_by_role("button",name="自审通过",exact=True).click()
            wait_job(requirement_ids[0],"review")
            expect(page.locator(".section-title").filter(has=page.get_by_role("heading",name="代码差异与自审")).get_by_text("自审通过",exact=True)).to_be_visible()
            checks["browser_diff_and_snapshot_bound_review"] = "PASSED"
            runtime.docker("exec",state["container"],"node","-e","require('node:fs').appendFileSync('server.mjs', '\\n// changed after approval\\n')")
            expect(page.get_by_text("需重新确认",exact=True)).to_be_visible(timeout=15000)
            checks["idle_code_change_invalidates_approval"] = "PASSED"
            second = detail(requirement_ids[1])
            assert snapshot(Path(second["workspace"]["path"]) / "repo")["tree_digest"] == original["tree_digest"]
            assert snapshot(source) == original
            checks["other_requirement_and_source_unchanged"] = "PASSED"
            expect(page.locator(".log-row").first).to_be_visible()
            checks["browser_persistent_event_display"] = "PASSED"
            page.screenshot(path=str(screenshot_dir / "desktop.png"),full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile viewport overflows"
            page.screenshot(path=str(screenshot_dir / "mobile.png"),full_page=True)
            checks["mobile_layout_without_horizontal_overflow"] = "PASSED"
            assert not console_errors, console_errors
            report.update({"status":"PASSED_BROWSER_CONTROLLER_FLOW","requirements":requirement_ids,
                "titles":titles,"source":str(source),"console_errors":console_errors})
        finally:
            for identity in requirement_ids:
                current = detail(identity)
                workspace = Path(current["workspace"]["path"])
                if (workspace / "runtime.json").exists():
                    response = client.post(f"/requirements/{identity}/actions",json={"action":"stop"})
                    response.raise_for_status()
                    wait_job(identity,"stop")
            browser.close()
            write_json(root / "browser-verification.json",report)
            client.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
