"""Real M6 onboarding, readonly preflight and sample commands; no model inference."""
import argparse
import hashlib
from dataclasses import replace
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

from agent_workbench import resources, runtime
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.settings import Settings
from agent_workbench.workspace import git, snapshot, write_json


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--m5-backup", type=Path); parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new dedicated directory")
    root.mkdir(parents=True, mode=0o700); data = root / "data"
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_DATABASE_URL": f"sqlite:///{data / 'workbench.db'}"}
    processes, handles = [], []
    report = {"status": "RUNNING", "model_inference": False, "sample_tests_executed": True, "user_business_project_tested": False, "checks": {}}
    checks = report["checks"]
    image = "agent-workbench-m6-sample:" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
    def launch(module):
        handle = (root / (module.rsplit(".", 1)[-1] + ".log")).open("a"); handles.append(handle)
        command = [sys.executable, "-m", module, "--data-dir", str(data)] + (["--port", str(port)] if module.endswith("server") else ["--api", base])
        process = subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT); processes.append(process)
    def terminate():
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    client = None
    try:
        runtime.docker("build", "--tag", image, str(Path(__file__).parent / "onboarding"))
        checks["custom_sample_dockerfile_builds_from_pinned_toolchain"] = "PASSED"
        launch("agent_workbench.server")
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/session", trust_env=False, timeout=1).status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        else: raise AssertionError("API did not start")
        settings = replace(Settings.load(data), database_url=env["AWB_DATABASE_URL"], model=env["AWB_MODEL"])
        client = httpx.Client(base_url=base + "/api/v1", headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=30)
        def post(path, value):
            response = client.post(path, json=value); response.raise_for_status(); return response.json()
        def wait_job(identity, expected="SUCCEEDED"):
            for _ in range(400):
                result = client.get("/jobs/" + identity).json()
                if result["status"] not in ("QUEUED", "RUNNING"):
                    assert result["status"] == expected, {"status": result["status"], "error": result.get("error")}; return result
                time.sleep(.2)
            raise AssertionError("Job did not finish")
        source = root / "source"; shutil.copytree(Path(__file__).parent / "sample-project", source)
        (source / "build.mjs").write_text("import fs from 'node:fs'; if (!fs.readFileSync('/run/workbench-config/settings.conf','utf8').includes('VALUE=one')) process.exit(1); console.log('Sample build configuration verified');\n")
        (source / "environment.test.mjs").write_text("import test from 'node:test'; import assert from 'node:assert/strict'; import fs from 'node:fs'; test('independent readonly configuration',()=>{ assert.match(fs.readFileSync('/run/workbench-config/settings.conf','utf8'),/VALUE=one/); assert.throws(()=>fs.writeFileSync('/run/workbench-config/settings.conf','forbidden')); });\n")
        git(source, "init", "-b", "main"); git(source, "config", "user.name", "M6 Sample"); git(source, "config", "user.email", "fixture@example.invalid"); git(source, "add", "--all"); git(source, "-c", "commit.gpgsign=false", "commit", "-m", "sample onboarding")
        original = snapshot(source)
        config_file = root / "settings.conf"; config_file.write_text("VALUE=one\n")
        project = post("/projects", {"name": "M6 样例项目", "source_path": str(source), "image": image})
        launch("agent_workbench.runner")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100}); errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token); page.get_by_role("button", name="进入工作台", exact=True).click()
            page.get_by_role("button", name="项目登记", exact=False).click()
            page.get_by_role("button", name="保存运行配置", exact=True).click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label("CPU 核数", exact=True).fill("0.5")
            dialog.get_by_label("构建命令（可留空）", exact=True).fill("node build.mjs")
            dialog.get_by_label("验证命令", exact=True).fill("node --test")
            dialog.get_by_role("button", name="添加配置文件", exact=True).click()
            dialog.get_by_label("配置源文件", exact=True).fill(str(config_file)); dialog.get_by_label("容器配置路径", exact=True).fill("/run/workbench-config/settings.conf")
            dialog.get_by_label("登记人", exact=True).fill("样例验收人"); dialog.get_by_label("配置原因", exact=True).fill("M6 环境接入实测")
            with page.expect_response(lambda response: response.url.endswith("/profiles") and response.request.method == "POST") as saved:
                dialog.get_by_role("button", name="保存新版本", exact=True).click()
            assert saved.value.status == 201, saved.value.text(); first_profile = saved.value.json()
            checks["browser_save_versioned_profile_without_file_contents"] = "PASSED"
            requirement = post("/requirements", {"project_id": project["id"], "title": "M6 样例接入需求"})
            endpoint = "/requirements/" + requirement["id"]
            page.get_by_role("button", name="需求工作区", exact=False).click()
            expect(page.locator(".requirement-card").filter(has_text="M6 样例接入需求")).to_be_visible(timeout=10000)
            page.locator(".requirement-card").filter(has_text="M6 样例接入需求").click()
            page.get_by_text("环境与手动工具", exact=True).click()
            def click_action(name, panel=None):
                target = panel or page
                expect(target.get_by_role("button", name=name, exact=True)).to_be_enabled(timeout=10000)
                with page.expect_response(lambda response: response.url.endswith(endpoint + "/actions") and response.request.method == "POST") as received:
                    target.get_by_role("button", name=name, exact=True).click()
                assert received.value.status == 202, received.value.text()
                return wait_job(received.value.json()["id"])
            click_action("创建工作区")
            panel = page.locator(".environment-panel")
            click_action("环境预检", panel)
            expect(panel.get_by_text("环境预检通过", exact=True)).to_be_visible(timeout=10000)
            detail = client.get(endpoint).json(); workspace = Path(detail["workspace"]["path"])
            preflight = json.loads((workspace / "preflight-report.json").read_text())
            assert all(item["passed"] for item in preflight["checks"])
            assert preflight["model_execution"] == "NOT_ATTEMPTED" and preflight["business_tests"] == "NOT_RUN"
            assert not resources.inventory(data)
            checks["real_readonly_preflight_tools_skills_mounts_and_cleanup"] = "PASSED"
            click_action("启动环境")
            inventory = resources.inventory(data); assert len(inventory) == 1
            inspected = json.loads(runtime.docker("inspect", inventory[0]["id"]).stdout)[0]
            assert inspected["HostConfig"]["NanoCpus"] == 500000000 and inspected["HostConfig"]["Memory"] == 1536 * 1024 * 1024
            assert not next(item for item in inspected["Mounts"] if item["Destination"] == "/run/workbench-config/settings.conf")["RW"]
            assert inspected["Image"] == preflight["image_id"]
            checks["runtime_uses_pinned_image_limits_and_readonly_config"] = "PASSED"
            click_action("执行项目构建", panel)
            tested = click_action("执行项目验证", panel)
            assert tested["result"]["command_output"]["exit_code"] == 0
            assert "independent readonly configuration" in tested["result"]["command_output"]["output_tail"]
            checks["real_container_build_and_three_sample_tests"] = "PASSED"
            config_file.write_text("VALUE=two\n")
            current = client.get(f"/projects/{project['id']}/profiles").json()["current"]
            updated = post(f"/projects/{project['id']}/profiles", {"expected_profile": current["id"], "actor": "样例验收人", "reason": "第二环境", "content": {**current["content"], "cpus": 1, "verification_command": "missing-m6-command"}})
            assert updated["number"] == 2
            click_action("执行项目验证", panel)
            assert client.get(endpoint).json()["profile"]["id"] == first_profile["id"]
            checks["profile_revision_and_host_change_leave_existing_workspace_independent"] = "PASSED"
            page.screenshot(path=str(root / "environment-desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844}); assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "environment-mobile.png"), full_page=True)
            assert not errors, errors; browser.close()
            checks["browser_mobile_without_errors_or_overflow"] = "PASSED"
        second = post("/requirements", {"project_id": project["id"], "title": "M6 第二配置验证"})
        second_endpoint = "/requirements/" + second["id"]
        wait_job(post(second_endpoint + "/actions", {"action": "prepare"})["id"])
        wait_job(post(second_endpoint + "/actions", {"action": "preflight"})["id"], "FAILED")
        second_detail = client.get(second_endpoint).json()
        assert second_detail["profile"]["number"] == 2 and second_detail["preflight"]["status"] == "FAILED"
        assert any(item["name"] == "command_entries" and not item["passed"] for item in second_detail["preflight"]["checks"])
        wait_job(post(second_endpoint + "/actions", {"action": "start"})["id"], "FAILED")
        checks["new_workspace_uses_new_profile_and_missing_command_blocks_start"] = "PASSED"
        assert snapshot(source) == original and config_file.read_text() == "VALUE=two\n"
        checks["original_source_and_host_config_not_modified_by_container"] = "PASSED"
        terminate(); stop(settings)
        backed_up = backup(settings); report["backup_sha256"] = backed_up["sha256"]
        target = root / "restored"; restore(Path(backed_up["file"]), target)
        assert not list(target.glob("workspaces/*/*/runtime-config/*"))
        checks["new_backup_restores_profile_metadata_without_config_contents"] = "PASSED"
        if args.m5_backup:
            result = restore(args.m5_backup, root / "restored-m5")
            assert result["workspace_count"] > 0
            checks["actual_m5_backup_restores_with_new_schema"] = "PASSED"
        report["image_id"] = preflight["image_id"]; report["opencode"] = preflight["tools"]["agent_cli"]["value"]
        report["status"] = "PASSED_M6_SAMPLE_ONBOARDING"
    finally:
        terminate()
        if client: client.close()
        for location in (data, root / "restored", root / "restored-m5"):
            if location.exists():
                try: resources.stop_all(location)
                except Exception: report.setdefault("cleanup_requires_inspection", []).append(str(location))
        for handle in handles: handle.close()
        runtime.docker("image", "rm", image, check=False)
        if report["status"] == "RUNNING": report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
