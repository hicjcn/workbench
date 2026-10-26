"""Real wrapper service and controlled task transport; never online inference."""
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
from agent_workbench.agent_clis import Content as AgentContent
from agent_workbench.execution import Executor
from agent_workbench.profiles import Content as Profile, inherit
from agent_workbench.settings import Settings
from agent_workbench.maintenance import backup, restore, stop
from agent_workbench.workspace import create_workspace, git, write_json


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(); root = args.root.resolve()
    if root.exists(): raise ValueError("Use a new verification directory")
    root.mkdir(parents=True, mode=0o700)
    settings = replace(Settings.load(root/"data", sqlite=True), model="opencode/glm-5.3-flash-free")
    with socket.socket() as reserved: reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
    base = f"http://127.0.0.1:{port}"; image = "awb-user/cli-wrapper:" + root.name.replace("_", "-")
    env = {**os.environ, "AWB_MODEL": settings.model, "AWB_DATABASE_URL": settings.database_url, "AGENT_FIXTURE_TOKEN": "private-wrapper-fixture-marker"}
    processes, handles, checks = [], [], []
    report = {"status": "RUNNING", "model_inference": False, "controlled_task_fixture": True, "user_specific_wrapper_verified": False, "checks": checks}
    client = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + settings.access_token}, trust_env=False, timeout=15)
    def check(name): checks.append(name); print(name, flush=True)
    def launch(module):
        handle = (root/(module.rsplit(".", 1)[-1]+".log")).open("a"); handles.append(handle)
        extra = ["--port", str(port), "--sqlite"] if module.endswith("server") else ["--api", base]
        processes.append(subprocess.Popen([sys.executable, "-m", module, "--data-dir", str(settings.data_dir), *extra], env=env, stdout=handle, stderr=subprocess.STDOUT))
    def terminate():
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        processes.clear()
    def get(path): response=client.get("/api/v1"+path); response.raise_for_status(); return response.json()
    def post(path, body):
        response=client.post("/api/v1"+path,json=body,headers={"Idempotency-Key":__import__("uuid").uuid4().hex}); response.raise_for_status(); return response.json()
    def wait(identity):
        for _ in range(600):
            job=get("/jobs/"+identity)
            if job["status"] not in ("QUEUED","RUNNING"): return job
            time.sleep(.2)
        raise AssertionError("job did not finish")
    try:
        runtime.docker("build", "-t", image, "poc/cli-wrapper", timeout=180)
        check("build_actual_delegating_opencode_wrapper_image")
        launch("agent_workbench.server")
        for _ in range(100):
            try:
                if client.get("/api/v1/session").status_code==200: break
            except httpx.HTTPError: pass
            time.sleep(.1)
        launch("agent_workbench.runner")
        content=AgentContent(serve_command="awb-opencode serve --hostname {host} --port {port}", run_command="awb-opencode fixture-run --attach {endpoint} --model {model} --format json", version_command="awb-opencode --version", expected_version="awb-opencode-fixture/1.0.0", credential_env=["AGENT_FIXTURE_TOKEN"]).model_dump()
        saved=post("/agent-clis",dict(name="受控 OpenCode 封装",content=content,actor="样例验证",reason="验证兼容接口和本地 fixture，非模型推理",set_default=True))
        source=root/"source";source.mkdir();(source/"main.txt").write_text("original\n")
        for args in (("init","-b","main"),("config","user.name","Fixture"),("config","user.email","fixture@example.invalid"),("add","--all"),("-c","commit.gpgsign=false","commit","-m","fixture")): git(source,*args)
        project=post("/projects",dict(name="封装CLI样例",source_path=str(source),image=image))
        profile=post(f"/projects/{project['id']}/profiles",dict(content=Profile(image=image).model_dump(),actor="样例验证",reason="固定镜像和预检"))
        req=post("/requirements",dict(project_id=project["id"],title="封装 CLI 传输验证",description="仅受控 fixture，不推理"))
        for action in ("prepare","preflight","start"):
            job=wait(post(f"/requirements/{req['id']}/actions",{"action":action})["id"])
            assert job["status"]=="SUCCEEDED", {"action":action,"error":job.get("error")}
        detail=get(f"/requirements/{req['id']}");workspace=Path(detail["workspace"]["path"])
        assert detail["agent_cli"]["id"]==saved["id"] and detail["agent_cli"]["fixed"]
        assert detail["preflight"]["tools"]["opencode"]["value"]=="awb-opencode-fixture/1.0.0"
        state=json.loads((workspace/"runtime.json").read_text()); inspected=json.loads(runtime.docker("inspect",state["container"]).stdout)[0]
        assert inspected["Config"]["Cmd"][0]=="awb-opencode"
        check("prepare_pin_and_preflight_really_use_wrapper_version_and_serve_command")
        assert runtime.probe(workspace)["health"]["healthy"]
        check("wrapper_exposes_real_opencode_authenticated_health_config_skills_and_session_api")
        post("/agent-clis/builtin-opencode/default",{})
        task=wait(post(f"/requirements/{req['id']}/actions",{"action":"task","prompt":"controlled fixture only; never call a model"})["id"])
        assert task["status"]=="SUCCEEDED",task
        assert task["result"]["agent_cli"]["id"]==saved["id"]
        assert (workspace/"repo/main.txt").read_text()=="controlled wrapper fixture change\n"
        assert (source/"main.txt").read_text()=="original\n"
        events=get(f"/jobs/{task['id']}/events")
        assert "private-wrapper-fixture-marker" not in json.dumps(events)
        assert any(event["kind"]=="model_completion" and event["payload"]["agent_cli_id"]==saved["id"] for event in events)
        check("controlled_stdin_json_task_uses_pinned_wrapper_after_default_switch_and_redacts_fixture_token")
        created=create_workspace(workspace/"repo",settings.data_dir/"attempts"/"cli-readonly",req["id"],project["id"],include_dirty=True,include_untracked=True)
        isolated=Path(created["workspace"]);inherit(workspace,isolated,settings.data_dir)
        with __import__("unittest.mock",fromlist=["patch"]).patch.dict(os.environ,{"AGENT_FIXTURE_TOKEN":env["AGENT_FIXTURE_TOKEN"]}):
            Executor(settings).start_runtime(isolated,image,read_only_source=True,opencode_model=settings.model)
        readonly=json.loads((isolated/"runtime.json").read_text())
        refused=runtime.docker("exec",readonly["container"],"node","-e","try{require('fs').writeFileSync('/workspace/project/main.txt','bad');process.exit(1)}catch(e){process.exit(['EROFS','EACCES'].includes(e.code)?0:2)}",check=False)
        assert refused.returncode==0
        assert readonly["agent_cli"]["id"]==saved["id"]
        runtime.stop(isolated)
        check("readonly_attempt_inherits_wrapper_and_kernel_refuses_source_write")
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox"])
            page=browser.new_page(viewport={"width":1440,"height":1000});errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto(base+"/#agent-clis",wait_until="networkidle");page.get_by_label("本机访问令牌",exact=True).fill(settings.access_token);page.get_by_role("button",name="进入工作台").click()
            expect(page.get_by_role("heading",name="Agent CLI 配置",exact=True)).to_be_visible()
            expect(page.get_by_role("heading",name="受控 OpenCode 封装",exact=True)).to_be_visible()
            page.get_by_role("button",name="容器管理",exact=True).click()
            expect(page.get_by_text("Agent CLI：受控 OpenCode 封装",exact=False)).to_be_visible()
            page.get_by_role("button",name="停止容器环境",exact=True).click()
            expect(page.get_by_role("button",name="启动容器环境",exact=True)).to_be_enabled(timeout=60000)
            assert not errors,errors;browser.close()
        check("real_browser_lists_wrapper_and_stops_its_owned_environment")
        terminate();stop(settings)
        archived=backup(settings);destination=root/"restored";restore(Path(archived["file"]),destination)
        from agent_workbench.store import Store
        from agent_workbench.agent_clis import selection, Configurations
        database=Store(f"sqlite:///{destination/'workbench.db'}")
        try:
            with database.engine.connect() as c: assert selection(c,req["id"])["id"]==saved["id"]
            assert Configurations(database).read()["default_id"]=="builtin-opencode"
        finally: database.engine.dispose()
        check("sqlite_0010_backup_restore_preserves_cli_history_and_pins_without_replaying_tasks")
        report.update(status="PASSED_COMPATIBLE_WRAPPER_TRANSPORT",checks_passed=len(checks),backup_sha256=archived["sha256"])
    finally:
        terminate();client.close()
        for handle in handles:handle.close()
        try:resources.stop_all(settings.data_dir)
        finally:runtime.docker("image","rm",image,check=False)
        write_json(root/"report.json",report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
