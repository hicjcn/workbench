"""Real acceptance tooling checks; reports online acceptance separately as blocked."""
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

from agent_workbench.acceptance import Session, CONTRACT_TARGET
from agent_workbench import resources, runtime
from agent_workbench.checked_archive import file_hash
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.settings import Settings
from agent_workbench.workspace import write_json


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, required=True); parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new dedicated verification directory")
    root.mkdir(mode=0o700, parents=True); data = root / "data"; scenario = root / "scenario"; restored = root / "restored"
    with socket.socket() as reserved: reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"; handles, processes = [], []; session = None
    report = {"tooling_status": "RUNNING", "online_acceptance_status": "NOT_RUN", "model_inference": False, "supervisor_review_performed": False, "checks": {}}
    checks = report["checks"]
    def launch(module, location):
        handle = (root / (module.rsplit(".", 1)[-1] + ".log")).open("a"); handles.append(handle)
        env = {**os.environ, "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_DATABASE_URL": f"sqlite:///{location / 'workbench.db'}"}
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
        raise AssertionError("API did not start")
    def wait(identity):
        for _ in range(600):
            job = session.get("/jobs/" + identity)
            if job["status"] not in ("QUEUED", "RUNNING"): return job
            time.sleep(.2)
        raise AssertionError("Sample job timed out")
    try:
        launch("agent_workbench.server", data); ready(); launch("agent_workbench.runner", data)
        settings = replace(Settings.load(data), model="opencode/glm-5.3-flash-free", database_url=f"sqlite:///{data / 'workbench.db'}")
        session = Session(scenario, base, data / "access-token")
        setup = session.setup("agent-workbench-m1:local", ["opencode/glm-5.3-flash-free", "opencode/big-pickle"])
        original_contract = file_hash(scenario / "criteria/acceptance.test.mjs")
        original_source = session.state["source_tree_digest"]
        checks["durable_sample_setup_registers_fixed_models_profile_and_readonly_contract"] = "PASSED"
        for expected in ("prepare", "preflight", "start", "verify"):
            action = session.advance(); assert action["status"] == "SUBMITTED", action
            job = wait(action["id"])
            assert job["action"] == expected
            assert job["status"] == ("FAILED" if expected == "verify" else "SUCCEEDED"), {"action": expected, "error": job.get("error")}
        snapshot = session.snapshot(); assert snapshot[-1]
        checks["unchanged_baseline_really_fails_independent_function_and_http_acceptance_tests"] = "PASSED"
        current = session.get("/requirements/" + setup["resources"]["requirement_id"])
        workspace = Path(current["workspace"]["path"])
        state = json.loads((workspace / "runtime.json").read_text())
        inspected = json.loads(runtime.docker("inspect", state["container"]).stdout)[0]
        assert any(item["Destination"] == CONTRACT_TARGET and not item["RW"] for item in inspected["Mounts"])
        refused = runtime.docker("exec", state["container"], "node", "-e", "try{require('fs').appendFileSync(process.argv[1],'tamper');process.exit(1)}catch(e){process.exit(['EROFS','EACCES'].includes(e.code)?0:2)}", CONTRACT_TARGET, check=False)
        assert refused.returncode == 0 and file_hash(scenario / "criteria/acceptance.test.mjs") == original_contract
        checks["actual_container_cannot_modify_fixed_acceptance_file"] = "PASSED"
        restarted = session.advance(); assert restarted["status"] == "SUBMITTED"; assert wait(restarted["id"])["status"] == "SUCCEEDED"
        before = len(session.state["operations"])
        blocked = session.advance()
        assert blocked["status"] in ("BLOCKED_NETWORK", "MISSING_CREDENTIAL"), blocked
        assert len(session.state["operations"]) == before and not session.state["pending"]
        report["online_acceptance_status"] = blocked["status"]
        report["model_diagnostics"] = blocked["diagnostics"]
        checks["actual_proxy_denial_prevents_any_model_task_submission"] = "PASSED"
        audit = session.status()["audit"]
        assert audit["status"] == "INCOMPLETE" and any(item["category"] == "model" and item["status"] == "PENDING" for item in audit["checks"])
        checks["incomplete_records_never_display_full_online_acceptance"] = "PASSED"
        # Explicitly controlled correct implementation tests the test suite, not a model.
        fixed = root / "controlled-fixture"; fixed.mkdir()
        text = (scenario / "source/server.mjs").read_text().replace("export function greeting(name = 'world') { return { message: `hello ${name}` }; }",
            "export function greeting(name = 'world', lang = 'en') { const value = String(name).trim() || 'world'; return {message: lang === 'zh' ? `你好，${value}` : `hello ${value}`}; }")
        text = text.replace("greeting(url.searchParams.get('name') ?? 'world')", "greeting(url.searchParams.get('name') ?? 'world', url.searchParams.get('lang') ?? 'en')")
        (fixed / "server.mjs").write_text(text)
        tested = subprocess.run(["node", "--test", "--test-reporter=tap", str(scenario / "criteria/acceptance.test.mjs")], env={**os.environ, "AWB_ACCEPTANCE_TARGET": str(fixed / "server.mjs")}, capture_output=True, text=True, timeout=30)
        assert tested.returncode == 0 and "# pass 6" in tested.stdout, tested.stdout[-1000:]
        report["positive_implementation_is_controlled_fixture"] = True
        checks["controlled_correct_fixture_passes_all_six_independent_cases"] = "PASSED"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width":1500,"height":1100}); errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base); page.get_by_label("本机访问令牌", exact=True).fill(settings.access_token); page.get_by_role("button", name="进入工作台", exact=True).click()
            expect(page.locator(".acceptance-panel")).to_be_visible(timeout=10000)
            page.get_by_role("button", name="核对验收清单", exact=True).click()
            expect(page.locator(".acceptance-panel")).to_contain_text("仍有待补齐的执行证据")
            expect(page.locator(".acceptance-panel")).to_contain_text("待完成 · 至少两个不同模型")
            page.screenshot(path=str(root / "acceptance-desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844}); assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "acceptance-mobile.png"), full_page=True); assert not errors, errors
            browser.close(); checks["browser_and_mobile_distinguish_model_evidence_from_human_registration"] = "PASSED"
        session.check_contract(); assert session.state["source_tree_digest"] == original_source
        terminate(); stop(settings); backed_up = backup(settings); report["backup_sha256"] = backed_up["sha256"]
        restore(Path(backed_up["file"]), restored); assert not resources.inventory(restored)
        launch("agent_workbench.server", restored); ready()
        session.client.close(); session = Session(scenario, base, restored / "access-token")
        recovered = session.status()
        assert recovered["negative_control_confirmed"] and recovered["audit"]["status"] == "INCOMPLETE"
        assert not resources.inventory(restored)
        checks["offline_restore_preserves_negative_control_and_pending_acceptance_without_auto_replay"] = "PASSED"
        report["tooling_status"] = "PASSED_ACCEPTANCE_TOOLING"
    finally:
        terminate()
        if session: session.client.close()
        for location in (data, restored):
            if location.exists():
                try: resources.stop_all(location)
                except Exception: report.setdefault("cleanup_requires_inspection", []).append(str(location))
        for handle in handles: handle.close()
        if report["tooling_status"] == "RUNNING": report["tooling_status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
