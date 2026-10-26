"""Windows CMD/PowerShell + real fresh venv; no Docker or model tasks."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx

from agent_workbench.configuration import launcher_directory
from agent_workbench.launcher import owned_process, read_state
from agent_workbench.workspace import git


def free_port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0)); return reserved.getsockname()[1]


def main():
    if os.name != "nt": raise SystemExit("This verification requires native Windows.")
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path); args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = (args.root or repo / ".local-data" / ("windows launcher 测试 " + uuid.uuid4().hex[:8])).resolve()
    if root.exists(): raise ValueError("Use a new verification directory")
    root.mkdir(parents=True)
    file_port, port = free_port(), free_port()
    while port == file_port: port = free_port()
    config = root / "workbench.toml"
    config.write_text('[workbench]\nuse_sqlite=true\nport=' + str(file_port) + '\nmodel="opencode/glm-5.3-flash-free"\ndata_dir="data"\nweb_dir=' + json.dumps(str(repo / "web/dist")) +
        '\n[launcher]\nvenv_dir="venv"\nlogs_dir="logs"\nrequire_docker=false\nstop_managed_environments=false\nopen_browser=false\nstartup_timeout_seconds=60\nstop_timeout_seconds=60\n' +
        '[environment]\nAGENT_WINDOWS_TEST_TOKEN="fixture-file-value-not-a-real-key"\n[agent_cli]\nenabled=true\nname="Windows configured CLI"\nexecutable="opencode"\ncredential_env=["AGENT_WINDOWS_TEST_TOKEN"]\n', encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWB_")}
    environment.update(PYTHONUTF8="1", AWB_PORT=str(port), AWB_MODEL="opencode/big-pickle", AGENT_WINDOWS_TEST_TOKEN="fixture-env-wins-not-a-real-key")
    checks = []
    def run(argv, timeout=120):
        result = subprocess.run(argv, cwd=repo, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        for secret in ("fixture-file-value-not-a-real-key", "fixture-env-wins-not-a-real-key"):
            assert secret not in result.stdout + result.stderr
        if result.returncode:
            (root / "command-error.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
            print((result.stdout + result.stderr)[-4000:], file=sys.stderr)
            raise AssertionError("Windows command failed; inspect the private command-error.txt")
        return result
    def cmd(action, *extra, timeout=120):
        return run(["cmd.exe", "/d", "/c", str(repo / (action + ".cmd")), "--config", str(config), *extra], timeout)
    def powershell(action, *extra):
        return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(repo / "scripts" / (action + ".ps1")), "-ConfigPath", str(config), *extra])
    foreign = None
    try:
        cmd("setup", "--skip-image-build", timeout=600)
        assert (root / "venv/Scripts/python.exe").is_file()
        checks.append("cmd_setup_creates_real_venv_installs_server_and_builds_web")
        first = json.loads(cmd("start", "--no-browser").stdout)
        assert first["phase"] == "RUNNING" and first["api_url"] == f"http://127.0.0.1:{port}"
        checks.append("cmd_start_uses_real_venv_and_environment_port_override")
        second = json.loads(powershell("start", "-NoBrowser").stdout)
        assert second["already_running"] and second["instance"] == first["instance"]
        checks.append("windows_powershell_51_start_is_idempotent")
        status = json.loads(cmd("status").stdout); assert status["instance"] == first["instance"]
        token = (root / "data/access-token").read_text(encoding="utf-8")
        with httpx.Client(base_url=first["api_url"], trust_env=False, headers={"Authorization": "Bearer " + token}, timeout=20) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/v1/config").json()["model"] == "opencode/big-pickle"
            cli = client.get("/api/v1/agent-clis").json()
            assert any(item["name"] == "Windows configured CLI" and item["id"] == cli["default_id"] for item in cli["configs"])
            source = root / "source"; source.mkdir(); (source / "main.txt").write_bytes("中文源码\n".encode())
            for values in (("init", "-b", "main"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"), ("add", "--all"), ("-c", "commit.gpgsign=false", "commit", "-m", "fixture")): git(source, *values)
            def post(path, body):
                response = client.post("/api/v1" + path, json=body); response.raise_for_status(); return response.json()
            project = post("/projects", {"name": "Windows script fixture", "source_path": str(source)})
            req = post("/requirements", {"project_id": project["id"], "title": "prepare only, no model"})
            job = post(f"/requirements/{req['id']}/actions", {"action": "prepare"})
            deadline = time.monotonic() + 60
            while True:
                current = client.get("/api/v1/jobs/" + job["id"]).json()
                if current["status"] not in ("QUEUED", "RUNNING"): break
                if time.monotonic() >= deadline: raise AssertionError("prepare did not finish")
                time.sleep(0.2)
            assert current["status"] == "SUCCEEDED"
            assert client.get("/api/v1/requirements/" + req["id"]).json()["agent_cli"]["fixed"]
        checks.append("real_api_runner_sqlite_and_configured_cli_prepare_workspace_without_inference")
        foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], env=environment)
        config.write_text('[workbench]\nport="temporarily invalid edit"\n', encoding="utf-8")
        assert json.loads(powershell("status").stdout)["instance"] == first["instance"]
        stopped = json.loads(powershell("stop").stdout)
        assert stopped["phase"] == "STOPPED" and not stopped["containers_stopped"]
        assert foreign.poll() is None
        checks.append("powershell_stop_uses_recorded_instance_after_config_edit_and_preserves_other_python")
        assert json.loads(cmd("stop").stdout)["phase"] == "STOPPED"
        state = read_state(launcher_directory(config))
        assert not owned_process(state, state["instance"])
        assert (root / "data/workbench.db").is_file()
        for path in list((root / "logs").rglob("*.log")) + [launcher_directory(config) / "instance.json"]:
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "fixture-file-value-not-a-real-key" not in text and "fixture-env-wins-not-a-real-key" not in text
        checks.append("repeat_stop_keeps_sqlite_and_runtime_metadata_logs_omit_configured_credentials")
        report = {"status": "PASSED_WINDOWS_CONTROL_LAUNCHER", "checks_passed": len(checks), "checks": checks,
            "model_inference": False, "docker_execution": False, "real_venv": True, "native_cmd_and_windows_powershell": True}
        (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        try: powershell("stop")
        except Exception: pass
        if foreign and foreign.poll() is None: foreign.terminate(); foreign.wait(timeout=10)


if __name__ == "__main__": main()
