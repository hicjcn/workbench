"""Actual API / Runner / Docker and browser check; no model inference."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from playwright.sync_api import expect, sync_playwright
from agent_workbench import runtime, resources
from agent_workbench.settings import Settings
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.workspace import git, write_json


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new verification directory")
    root.mkdir(parents=True, mode=0o700)
    settings = replace(Settings.load(root / "data"), model="opencode/glm-5.3-flash-free")
    with socket.socket() as reserved: reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "AWB_MODEL": settings.model, "AWB_DATABASE_URL": settings.database_url}
    processes, handles, images = [], [], []
    checks = []; report = {"status": "RUNNING", "model_inference": False, "registry_pull_tested": False, "checks": checks}
    def check(name): checks.append(name); print(name, flush=True)
    def launch(module):
        handle = (root / (module.rsplit(".", 1)[-1] + ".log")).open("a"); handles.append(handle)
        extra = ["--port", str(port)] if module.endswith("server") else ["--api", base]
        processes.append(subprocess.Popen([sys.executable, "-m", module, "--data-dir", str(settings.data_dir), *extra], env=env, stdout=handle, stderr=subprocess.STDOUT))
    def terminate():
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    client = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=15)
    def get(path): response = client.get("/api/v1" + path); response.raise_for_status(); return response.json()
    def post(path, body): response = client.post("/api/v1" + path, json=body, headers={"Idempotency-Key": __import__("uuid").uuid4().hex}); response.raise_for_status(); return response.json()
    def image_wait(identity):
        for _ in range(1200):
            job = next(item for item in get("/docker")["operations"] if item["id"] == identity)
            if job["status"] not in ("QUEUED", "RUNNING"): return job
            time.sleep(.2)
        raise AssertionError("Image job timed out")
    def wait(identity):
        for _ in range(600):
            job = get("/jobs/" + identity)
            if job["status"] not in ("QUEUED", "RUNNING"): return job
            time.sleep(.2)
        raise AssertionError("Workspace job timed out")
    def image_operation(kind, ref, **options):
        job = post("/docker/operations", dict(kind=kind, reference=ref, actor="样例验收", reason="真实 Docker 管理验证", **options))
        return image_wait(job["id"])
    try:
        launch("agent_workbench.server")
        for _ in range(100):
            try:
                if client.get("/api/v1/session").status_code == 200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        launch("agent_workbench.runner")
        unique = root.name.lower().replace("_", "-")
        ref = "awb-user/" + unique + ":agent"; images.append(ref)
        recipe = post("/docker/recipes", dict(name="Docker 管理验收环境", reference=ref, dockerfile="FROM agent-workbench-m1:local\nLABEL awb.docker-management.fixture=true\n", actor="样例验收", reason="只继承现有工具镜像，不下载模型"))
        built = image_operation("build", ref, recipe_id=recipe["id"])
        assert built["status"] == "SUCCEEDED", built
        check("runner_really_builds_saved_dockerfile_version")
        assert get("/docker")["recipes"][0]["dockerfile"] == recipe["dockerfile"]
        check("versioned_recipe_and_result_are_persistent")
        source = root / "source"; source.mkdir(); (source / "main.txt").write_text("isolated fixture\n")
        for args in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"), ("add", "--all"), ("-c", "commit.gpgsign=false", "commit", "-m", "fixture")): git(source, *args)
        project = post("/projects", dict(name="Docker 管理样例", source_path=str(source), image=ref))
        req = post("/requirements", dict(project_id=project["id"], title="真实容器生命周期验证", description="只启动环境，不执行模型任务"))
        for action in ("prepare", "start"):
            result = wait(post(f"/requirements/{req['id']}/actions", {"action": action})["id"])
            assert result["status"] == "SUCCEEDED", result
        check("actual_requirement_environment_starts_from_built_image")
        for _ in range(50):
            status = get("/docker")
            if status["connected"] and status["containers"]: break
            time.sleep(.2)
        assert status["connected"] and status["containers"] and any(ref in item["tags"] for item in status["images"]), status
        assert all(settings.access_token not in json.dumps(status) and settings.runner_token not in json.dumps(status) for _ in [0])
        assert status["containers"][0]["verified"]
        check("runner_reports_actual_images_and_owned_containers_without_credentials")
        protected = image_operation("remove", ref, expected_image_id=built["result"]["image"]["id"])
        assert protected["status"] == "FAILED" and "引用" in protected["error"], protected
        check("actual_in_use_image_cannot_be_removed")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1000}); page.set_default_timeout(15000)
            errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle"); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token); page.get_by_role("button", name="进入工作台").click()
            page.get_by_role("button", name="Docker 镜像", exact=True).click()
            expect(page.get_by_role("heading", name="Docker 镜像", exact=True)).to_be_visible()
            expect(page.get_by_text(ref, exact=True).first).to_be_visible()
            page.get_by_role("button", name="容器管理", exact=True).click()
            expect(page.get_by_role("button", name="停止容器环境", exact=True)).to_be_enabled()
            page.screenshot(path=str(root / "containers-desktop.png"), full_page=True)
            page.get_by_role("button", name="停止容器环境", exact=True).click()
            expect(page.get_by_role("button", name="启动容器环境", exact=True)).to_be_enabled(timeout=60000)
            assert get(f"/requirements/{req['id']}")["workspace"]["environment"] == "STOPPED"
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "containers-mobile.png"), full_page=True)
            assert not errors, errors
            browser.close()
        check("browser_can_stop_actual_environment_and_mobile_layout_has_no_overflow")
        disposable = "awb-user/" + unique + ":scratch"; images.append(disposable)
        second = post("/docker/recipes", dict(name="可移除镜像样例", reference=disposable, dockerfile="FROM scratch\nLABEL awb.fixture.disposable=true\n", actor="样例验收", reason="验证未引用镜像的非强制移除"))
        image = image_operation("build", disposable, recipe_id=second["id"]); assert image["status"] == "SUCCEEDED", image
        removed = image_operation("remove", disposable, expected_image_id=image["result"]["image"]["id"])
        assert removed["status"] == "SUCCEEDED", removed
        check("unreferenced_fixture_tag_can_be_removed_without_force_or_volume_deletion")
        terminate(); stop(settings)
        saved = backup(settings); destination = root / "restored"; restore(Path(saved["file"]), destination)
        from agent_workbench.store import Store, docker_recipes, docker_jobs
        database = Store(f"sqlite:///{destination / 'workbench.db'}")
        try:
            assert len(database.list(docker_recipes)) == 2
            assert all(job["status"] not in ("QUEUED", "RUNNING", "UNKNOWN") for job in database.list(docker_jobs))
        finally: database.engine.dispose()
        check("schema_0009_offline_backup_restores_templates_and_history_without_replaying_jobs")
        report.update(status="PASSED", checks_passed=len(checks), backup_sha256=saved["sha256"])
    finally:
        terminate()
        for handle in handles: handle.close()
        client.close()
        try: resources.stop_all(settings.data_dir)
        finally:
            for ref in images: runtime.docker("image", "rm", ref, check=False)
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
