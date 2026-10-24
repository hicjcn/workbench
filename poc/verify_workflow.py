"""Browser + real containers, with explicitly controlled reports instead of inference."""
import argparse
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

from agent_workbench import runtime
from agent_workbench.execution import Executor
from agent_workbench.runner import Runner
from agent_workbench.settings import Settings
from agent_workbench.workspace import git, snapshot, write_json


PURPOSE = "controlled M3 browser acceptance; no model inference"


class ControlledExecutor(Executor):
    def task(self, job, workspace, emit, canceled, **options):
        key = job["input"]["step_key"]
        state = json.loads((workspace / "runtime.json").read_text())
        if key == "development":
            runtime.docker("exec", state["container"], "node", "-e",
                "require('node:fs').appendFileSync('server.mjs','\\n// controlled M3 acceptance edit; not model generated\\n')")
            report = {"summary": "受控开发报告：仅追加验收注释，未调用模型", "verification_notes": "后续步骤执行真实 Node.js 测试"}
        else:
            details = json.loads(runtime.docker("inspect", state["container"]).stdout)[0]
            source = next(mount for mount in details["Mounts"] if mount["Destination"] == "/workspace/project")
            assert not source["RW"]
            write = runtime.docker("exec", state["container"], "node", "-e",
                "require('node:fs').writeFileSync('/workspace/project/forbidden.txt','must not succeed')", check=False)
            assert write.returncode != 0
            records_path = self.settings.data_dir / "controlled-roles.json"
            records = json.loads(records_path.read_text()) if records_path.exists() else []
            records.append({"step": key, "model": options["model"], "code_digest": snapshot(workspace / "repo")["tree_digest"],
                "workspace": str(workspace), "source_read_only": True, "write_denied": True})
            write_json(records_path, records)
            report = {"summary": "受控只读分析报告，未调用模型", "impacted_paths": ["server.mjs"], "plan": "在已登记样例中验证流程", "risks": []} if key == "analysis" else {
                "summary": "受控独立评审报告，未调用模型", "findings": ([{"severity": "MAJOR", "path": "server.mjs", "line": 1,
                    "description": "受控问题，仅用于验证问题处置界面", "recommendation": "记录这是验收样例"}] if key == "model_review_0" else [])}
        emit("fixture", {"text": PURPOSE})
        return {**self.observe(workspace), "agent_text": json.dumps(report, ensure_ascii=False)}


def fixture_runner(root, base):
    if json.loads((root / "qa-purpose.json").read_text())["purpose"] != PURPOSE:
        raise ValueError("Fixture Runner only accepts its dedicated acceptance directory")
    settings = Settings.load(root)
    runner = Runner(settings, base, executor=ControlledExecutor(settings))
    while True:
        try:
            worked = runner.run_once()
            if not worked:
                runner.watch_once()
                time.sleep(0.5)
        except httpx.HTTPError:
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--fixture-runner", action="store_true")
    parser.add_argument("--api", default="http://127.0.0.1:8341")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.fixture_runner:
        return fixture_runner(root, args.api)
    if root.exists():
        raise ValueError("Use a new dedicated verification directory")
    root.mkdir(parents=True)
    write_json(root / "qa-purpose.json", {"purpose": PURPOSE})
    environment = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_DATABASE_URL": f"sqlite:///{root / 'workbench.db'}"}
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    processes = []
    handles = []
    report = {"status": "RUNNING", "model_inference": False, "controlled_reports": True, "checks": {}}
    def launch(module, extra=()):
        handle = (root / ("api.log" if module == "agent_workbench.server" else "runner.log")).open("a")
        handles.append(handle)
        command = [sys.executable, "-m", module, "--data-dir", str(root), "--port", str(port)] if module == "agent_workbench.server" else [sys.executable, __file__, "--fixture-runner", "--root", str(root), "--api", base]
        process = subprocess.Popen(command, env=environment, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    def ready():
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/session", trust_env=False, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise AssertionError("API did not start")
    client = None
    try:
        server = launch("agent_workbench.server"); ready(); launch("fixture")
        source = root / "source"
        shutil.copytree(Path(__file__).parent / "sample-project", source)
        git(source, "init", "-b", "main"); git(source, "config", "user.name", "M3 Fixture")
        git(source, "config", "user.email", "fixture@example.invalid"); git(source, "add", "--all")
        git(source, "-c", "commit.gpgsign=false", "commit", "-m", "controlled fixture")
        original = snapshot(source)
        client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + (root / "access-token").read_text().strip()}, trust_env=False)
        def post(path, data):
            response = client.post(path, json=data); response.raise_for_status(); return response.json()
        def wait_job(identity, expected_id=None):
            for _ in range(400):
                detail = client.get("/requirements/" + identity).json()
                job = detail["jobs"][0] if detail["jobs"] else None
                if job and (expected_id is None or job["id"] == expected_id) and job["status"] not in ("QUEUED", "RUNNING"):
                    assert job["status"] == "SUCCEEDED", job
                    return detail
                time.sleep(0.25)
            raise AssertionError("Job timed out")
        project = post("/projects", {"name": "M3 受控验收服务", "source_path": str(source)})
        requirements = []
        for title in ("M3 流程验收 · 受控报告", "M3 隔离对照需求"):
            requirement = post("/requirements", {"project_id": project["id"], "title": title, "description": PURPOSE, "acceptance": "流程与隔离验收"})
            requirements.append(requirement)
            post(f"/requirements/{requirement['id']}/actions", {"action": "prepare"}); wait_job(requirement["id"])
        identity = requirements[0]["id"]
        endpoint = "/requirements/" + identity
        post(endpoint + "/actions", {"action": "start"}); wait_job(identity)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100})
            errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base); page.get_by_label("本机访问令牌").fill((root / "access-token").read_text().strip())
            page.get_by_role("button", name="进入工作台", exact=True).click()
            page.locator(".requirement-card").filter(has_text=requirements[0]["title"]).click()
            panel = page.locator(".workflow-panel")
            panel.get_by_role("button", name="开启默认流程", exact=True).click()
            panel.get_by_label("验证命令", exact=True).fill("node --test server.test.mjs")
            panel.get_by_role("button", name="保存并开启本轮", exact=True).click()
            expect(panel.get_by_label("默认开发步骤")).to_be_visible()
            for index in range(8):
                state = client.get(endpoint + "/workflow").json()["current"]
                node = next(item for item in state["nodes"] if item["key"] == state["next_step"])
                expect(panel.get_by_role("heading", name="下一步：" + node["label"], exact=True)).to_be_visible(timeout=15000)
                if node["key"] in ("scope", "resolution", "self_review"):
                    if node["key"] == "resolution":
                        panel.get_by_label("问题处置 · model_review_0:0", exact=True).select_option("FALSE_POSITIVE")
                        panel.get_by_label("处置依据 · model_review_0:0", exact=True).fill("这是受控验收问题，已检查测试目的")
                    panel.get_by_label("确认依据", exact=True).fill("实际检查受控样例，确认本步")
                    button = panel.get_by_role("button", name="确认本步通过", exact=True)
                else:
                    button = panel.get_by_role("button", name="运行：" + node["label"], exact=True)
                with page.expect_response(lambda response: response.url.endswith(endpoint + "/workflow/steps") and response.request.method == "POST") as receipt:
                    button.click()
                assert receipt.value.status == 202
                wait_job(identity, receipt.value.json()["id"])
                for _ in range(100):
                    observed = client.get(endpoint + "/workflow").json()["current"]
                    if observed["next_step"] != node["key"]:
                        break
                    time.sleep(0.1)
                assert observed["next_step"] != node["key"]
            expect(panel.get_by_text("本轮分析到自审已完成，可进入后续概要设计阶段。", exact=True)).to_be_visible(timeout=15000)
            report["checks"]["browser_fixed_workflow_to_self_review"] = "PASSED"
            report["checks"]["browser_per_finding_human_disposition"] = "PASSED"
            records = json.loads((root / "controlled-roles.json").read_text())
            reviewers = [item for item in records if item["step"].startswith("model_review_")]
            assert len(reviewers) == 2 and reviewers[0]["model"] != reviewers[1]["model"]
            assert reviewers[0]["code_digest"] == reviewers[1]["code_digest"]
            assert reviewers[0]["workspace"] != reviewers[1]["workspace"]
            report["checks"]["real_docker_source_read_only_and_write_denied"] = "PASSED"
            report["checks"]["distinct_roles_same_digest_separate_homes"] = "PASSED"
            archived = client.get(endpoint + "/workflow/runs/" + observed["id"]).json()
            for attempt in archived["attempts"]:
                if attempt["step_key"].startswith("model_review_"):
                    assert not any(key.startswith("model_review_") for key in attempt["input"]["bundle"]["upstream"])
            report["checks"]["review_inputs_exclude_peer_model_reports"] = "PASSED"
            verification = next(item for item in observed["nodes"] if item["key"] == "verification")["output"]
            assert verification["exit_code"] == 0 and "# pass 2" in verification["output_tail"]
            report["checks"]["real_container_verification_command_two_node_tests"] = "PASSED"
            page.screenshot(path=str(root / "desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "mobile.png"), full_page=True)
            report["checks"]["mobile_layout_without_horizontal_overflow"] = "PASSED"
            panel.get_by_role("button", name="修订需求", exact=True).click()
            modal = page.get_by_role("dialog")
            modal.get_by_label("验收标准", exact=True).fill("更新后的受控验收标准")
            modal.get_by_label("变更原因", exact=True).fill("验证版本变化使旧确认失效")
            modal.get_by_role("button", name="保存新版本", exact=True).click()
            expect(modal).not_to_be_visible()
            expect(panel.get_by_text("需求 v2 · 已失效，需新一轮", exact=True)).to_be_visible(timeout=15000)
            report["checks"]["browser_revision_invalidates_old_flow"] = "PASSED"
            old_flow_id = observed["id"]
            panel.get_by_role("button", name="新建流程轮次", exact=True).click()
            panel.get_by_role("button", name="保存并开启本轮", exact=True).click()
            expect(panel.get_by_role("heading", name="下一步：只读全景分析", exact=True)).to_be_visible(timeout=15000)
            history = panel.locator("details").filter(has=page.get_by_text("流程历史 · 2", exact=True))
            history.locator("summary").click()
            history.get_by_role("button", name="查看本轮记录", exact=True).last.click()
            archive_dialog = page.get_by_role("dialog")
            expect(archive_dialog.get_by_role("heading", name="历史流程记录", exact=True)).to_be_visible()
            expect(archive_dialog.locator("pre").first).to_contain_text("受控只读分析报告")
            archive_dialog.get_by_role("button", name="关闭", exact=True).click()
            report["checks"]["browser_new_round_can_view_immutable_previous_reports"] = "PASSED"
            report["console_errors"] = errors
            assert not errors
            browser.close()
        current = client.get(endpoint).json()
        second = client.get("/requirements/" + requirements[1]["id"]).json()
        assert snapshot(source) == original
        assert snapshot(Path(second["workspace"]["path"]) / "repo")["tree_digest"] == original["tree_digest"]
        report["checks"]["source_and_other_requirement_unchanged"] = "PASSED"
        server.terminate(); server.wait(timeout=10); launch("agent_workbench.server"); ready()
        after = client.get(endpoint + "/workflow").json()
        previous = client.get(endpoint + "/workflow/runs/" + old_flow_id).json()
        assert after["revision"]["number"] == 2 and after["current"]["state"] == "ACTIVE" and len(after["history"]) == 2
        assert previous["state"] == "STALE" and len(previous["attempts"]) == 8
        report["checks"]["api_restart_preserves_revisions_reports_and_decisions"] = "PASSED"
        from sqlalchemy import text
        from agent_workbench.store import Store
        check_store = Store(f"sqlite:///{root / 'workbench.db'}")
        with check_store.engine.connect() as connection:
            migration = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        check_store.engine.dispose()
        report.update(status="PASSED_CONTROLLED_M3_WORKFLOW", migration=migration, roles=records, actual_model_inference="NOT_ATTEMPTED")
    finally:
        if client: client.close()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate(); process.wait(timeout=10)
        for handle in handles: handle.close()
        for path in [*root.glob("workspaces/*/*/runtime.json"), *root.glob("attempts/*/*/*/runtime.json")]:
            runtime.stop(path.parent)
        if report["status"] == "RUNNING": report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
