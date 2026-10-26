"""Actual browser -> API -> Runner -> Docker COPY; no model calls or pulls."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import time
import uuid

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench import runtime
from agent_workbench.workspace import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new verification directory")
    root.mkdir(parents=True, mode=0o700)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    config = root / "workbench.toml"
    config.write_text('[workbench]\nuse_sqlite=true\nport=' + str(port) + '\nmodel="opencode/big-pickle"\ndata_dir="data"\nweb_dir=' + json.dumps(str(repo / "web/dist")) +
        '\n[launcher]\nvenv_dir=' + json.dumps(str(Path(sys.prefix))) + '\nlogs_dir="logs"\nrequire_docker=true\nstop_managed_environments=true\nopen_browser=false\n', encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWB_")}
    environment.update(PYTHONUTF8="1", PYTHONPATH=str(repo / "src"))
    checks, images, containers = [], [], []
    report = {"status": "RUNNING", "model_inference": False, "registry_pull_tested": False, "checks": checks}
    def check(name): checks.append(name); print(name, flush=True)
    def control(action):
        result = subprocess.run([sys.executable, str(repo / "scripts/control.py"), action, "--config", str(config), "--no-browser"], cwd=repo, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=150)
        if result.returncode: raise AssertionError("Control command failed: " + result.stderr)
        return json.loads(result.stdout)
    def context(name, value):
        path = root / name; path.mkdir()
        (path / "app.txt").write_text(value, encoding="utf-8")
        (path / "ignored.txt").write_text("fixture-must-not-enter-image", encoding="utf-8")
        (path / "Dockerfile").write_text("THIS IS NOT THE SAVED DOCKERFILE", encoding="utf-8")
        (path / ".dockerignore").write_text("ignored.txt\nDockerfile\n.dockerignore\n", encoding="utf-8")
        return path
    first_context, second_context = context("build context 中文", "first version"), context("second context 中文", "second version")
    unique = uuid.uuid4().hex[:12]
    first_ref, second_ref = "awb-user/context-" + unique + ":v1", "awb-user/context-" + unique + ":v2"
    images.extend([first_ref, second_ref])
    client = None
    try:
        state = control("start")
        token = (root / "data/access-token").read_text(encoding="utf-8")
        client = httpx.Client(base_url=state["api_url"], headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=20)
        def read():
            response = client.get("/api/v1/docker"); response.raise_for_status(); return response.json()
        def wait_for(reference):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                job = next((item for item in read()["operations"] if item["input"]["reference"] == reference), None)
                if job and job["status"] not in ("QUEUED", "RUNNING"):
                    assert job["status"] == "SUCCEEDED", job
                    return job
                time.sleep(.2)
            raise AssertionError("Build did not finish")
        def verify_image(reference, value):
            container = runtime.docker("create", reference, "/never-executed").stdout.strip(); containers.append(container)
            archive = root / (reference.rsplit(":", 1)[1] + ".tar")
            with archive.open("wb") as output: runtime.docker_stream("export", container, destination=output)
            with tarfile.open(archive) as source:
                assert source.extractfile("payload/app.txt").read().decode() == value
                assert not any(name in source.getnames() for name in ("payload/ignored.txt", "payload/Dockerfile"))
            runtime.docker("rm", container); containers.remove(container)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1000}); page.set_default_timeout(15000)
            errors = []; page.on("pageerror", lambda failure: errors.append(str(failure)))
            page.goto(state["api_url"], wait_until="networkidle")
            page.get_by_label("本机访问令牌", exact=True).fill(token); page.get_by_role("button", name="进入工作台").click()
            page.get_by_role("button", name="Docker 镜像", exact=True).click()
            page.get_by_role("button", name="＋ 新建 Dockerfile", exact=True).click()
            page.get_by_label("模板名称").fill("Context browser fixture")
            page.get_by_label("构建镜像标签").fill(first_ref)
            page.get_by_label("构建上下文目录（可选）", exact=True).fill(str(first_context))
            page.get_by_label("Dockerfile", exact=True).fill("FROM scratch\nCOPY . /payload/\n")
            page.get_by_label("登记人", exact=True).fill("fixture")
            page.get_by_label("版本原因", exact=True).fill("First directory")
            page.get_by_role("button", name="保存 Dockerfile 版本", exact=True).click()
            expect(page.get_by_role("button", name="编辑新版", exact=True)).to_be_visible()
            first = read()["recipes"][0]
            assert first["context_dir"] == str(first_context)
            check("browser_saves_unicode_space_context_directory_on_recipe")
            page.get_by_role("button", name="编辑新版", exact=True).click()
            expect(page.get_by_label("构建上下文目录（可选）", exact=True)).to_have_value(str(first_context))
            page.get_by_label("构建上下文目录（可选）", exact=True).fill(str(second_context))
            page.get_by_label("构建镜像标签").fill(second_ref)
            page.get_by_label("登记人", exact=True).fill("fixture")
            page.get_by_label("版本原因", exact=True).fill("Second directory")
            page.get_by_role("button", name="保存 Dockerfile 版本", exact=True).click()
            expect(page.get_by_role("button", name="编辑新版", exact=True)).to_be_visible()
            second = next(item for item in read()["recipes"] if item["current"])
            assert second["number"] == 2 and second["context_dir"] == str(second_context)
            check("editor_preserves_context_and_changes_create_immutable_versions")
            response = client.post("/api/v1/docker/operations", json={"kind": "build", "reference": first_ref, "recipe_id": first["id"], "actor": "fixture", "reason": "Build pinned old context"}, headers={"Idempotency-Key": uuid.uuid4().hex})
            response.raise_for_status(); first_job = wait_for(first_ref)
            assert first_job["result"]["build_context"]["directory"] == str(first_context)
            verify_image(first_ref, "first version")
            check("old_recipe_build_uses_its_original_directory_and_honors_dockerignore")
            page.get_by_role("button", name="构建此版本", exact=True).click()
            expect(page.get_by_text(str(second_context), exact=True).last).to_be_visible()
            page.get_by_label("操作人", exact=True).fill("fixture")
            page.get_by_label("操作原因", exact=True).fill("Build directory from browser")
            page.get_by_role("button", name="提交构建镜像", exact=True).click()
            second_job = wait_for(second_ref)
            assert second_job["result"]["build_context"]["directory"] == str(second_context)
            verify_image(second_ref, "second version")
            expect(page.get_by_role("button", name="构建此版本", exact=True)).to_be_enabled(timeout=30000)
            check("browser_build_runs_real_copy_using_saved_dockerfile_instead_of_directory_dockerfile")
            page.get_by_role("button", name="版本历史", exact=True).click()
            expect(page.get_by_text(str(first_context), exact=True)).to_be_visible()
            page.get_by_role("button", name="关闭", exact=True).click()
            page.screenshot(path=str(root / "context-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "context-mobile.png"), full_page=True)
            assert not errors, errors
            browser.close()
            check("history_shows_context_and_mobile_has_no_horizontal_overflow_or_browser_errors")
        assert control("stop")["phase"] == "STOPPED"
        assert control("start")["phase"] == "RUNNING"
        assert len(read()["recipes"]) == 2 and next(item for item in read()["recipes"] if item["id"] == first["id"])["context_dir"] == str(first_context)
        check("sqlite_restart_preserves_both_context_versions")
        report.update(status="PASSED_DOCKER_BUILD_CONTEXT", checks_passed=len(checks))
    finally:
        if client: client.close()
        try: control("stop")
        finally:
            for container in containers: runtime.docker("rm", "--force", container, check=False)
            for reference in images: runtime.docker("image", "rm", reference, check=False)
            write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
