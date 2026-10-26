"""Local lifecycle supervisor. Control metadata never contains model credentials."""
import argparse
from contextlib import ExitStack
import faulthandler
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser

import httpx
import psutil

from . import locking
from .configuration import Configuration, ConfigurationError, config_path, launcher_directory
from .platform_support import IS_WINDOWS, docker_environment, is_link, retry_file_access
from .settings import Settings, storage_summary
from .workspace import WorkbenchError, write_json

TERMINAL = {"STOPPED", "FAILED", "NEEDS_ATTENTION"}


def control_dir(path):
    return launcher_directory(path)


def read_state(directory):
    path = directory / "instance.json"
    def read():
        if not path.exists(): return None
        if is_link(path) or path.stat().st_size > 65536: raise WorkbenchError("启动状态文件不可信")
        return path.read_text(encoding="utf-8")
    content = retry_file_access(read)
    if content is None: return None
    try:
        value = json.loads(content)
        if not re.fullmatch(r"[a-f0-9]{32}", value["instance"]) or not isinstance(value["pid"], int) or value["pid"] <= 0:
            raise ValueError
        if not 1 <= int(value["control_port"]) <= 65535 or len(value["control_token"]) < 32: raise ValueError
        return value
    except (KeyError, TypeError, ValueError):
        raise WorkbenchError("启动状态文件格式无效；未执行进程操作") from None


def process_info(pid):
    process = psutil.Process(pid)
    return {"pid": process.pid, "created_at": process.create_time()}


def owned_process(value, instance, role=None):
    try:
        process = psutil.Process(value["pid"])
        if abs(process.create_time() - value["created_at"]) > 0.001 or process.status() == psutil.STATUS_ZOMBIE: return False
        args = process.cmdline()
        if "agent_workbench.launcher" not in args or instance not in args: return False
        if role is None: return "_supervise" in args
        return "_child" in args and role in args
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


def descendant(pid, parent):
    if pid == parent: return True
    try: return parent in [process.pid for process in psutil.Process(pid).parents()]
    except psutil.Error: return False


def summary(state):
    if not state: return {"phase": "STOPPED", "managed": False}
    return {key: state[key] for key in ("phase", "instance", "pid", "api_url", "data_dir", "logs_dir", "roles", "error", "warning", "containers_stopped", "stop_timeout_seconds", "stopping_role") if key in state}


def rpc(state, method="GET", path="/status"):
    if not owned_process(state, state["instance"]): raise WorkbenchError("原启动器已退出或 PID 归属已变化")
    with httpx.Client(trust_env=False, timeout=5) as client:
        response = client.request(method, f"http://127.0.0.1:{state['control_port']}{path}", headers={"Authorization": "Bearer " + state["control_token"]})
        response.raise_for_status()
        return response.json()


def wait_supervisor_exit(state, timeout):
    deadline = time.monotonic() + timeout
    while owned_process(state, state["instance"]):
        if time.monotonic() >= deadline: return False
        time.sleep(0.1)
    return True


def marker(directory, instance, role):
    return directory / (instance + "." + role + ".stop")


def request_roles_stop(directory, instance):
    for role in ("runner", "api"):
        write_json(marker(directory, instance, role), {"stop": True})


def offline_stop(settings):
    from .maintenance import stop
    locks = settings.data_dir / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for name in ("docker-images.lock", "docker-command.lock"):
            handle = stack.enter_context((locks / name).open("a+b"))
            try: locking.flock(handle, locking.LOCK_EX | locking.LOCK_NB)
            except BlockingIOError: raise WorkbenchError("镜像命令仍在执行，停止尚未核实") from None
        report = stop(settings)
        from .docker_management import Images
        from .store import Store, docker_jobs
        store = Store(settings.database_url)
        try:
            Images(store).expire()
            if any(row["status"] in ("RUNNING", "UNKNOWN") for row in store.list(docker_jobs)):
                raise WorkbenchError("镜像任务需要人工核实；容器停止不代表镜像任务成功")
        finally: store.engine.dispose()
        return report


def status(directory):
    state = read_state(directory)
    if not state or state["phase"] in TERMINAL: return summary(state)
    if not owned_process(state, state["instance"]): return {**summary(state), "phase": "STALE"}
    try: return rpc(state)
    except httpx.HTTPError: return {**summary(state), "phase": "UNREACHABLE"}


def start(configuration, directory, no_browser=False):
    state = read_state(directory)
    if state and owned_process(state, state["instance"]):
        result = rpc(state)
        if result["phase"] == "RUNNING": return {**result, "already_running": True}
        raise WorkbenchError("同一配置的服务正在启动或停止，请先查看 status")
    if state and state["phase"] not in TERMINAL:
        raise WorkbenchError("检测到未正常结束的启动记录，请先运行 stop 核实，禁止重复启动")
    instance = uuid.uuid4().hex
    configuration.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    with (configuration.logs_dir / "launcher.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, "-m", "agent_workbench.launcher", "_supervise", "--config", str(configuration.file) if configuration.file else "-", "--instance", instance],
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log, creationflags=flags, start_new_session=not IS_WINDOWS)
    deadline = time.monotonic() + configuration.startup_timeout_seconds
    while time.monotonic() < deadline:
        state = read_state(directory)
        if state and state["instance"] == instance:
            if state["phase"] in TERMINAL:
                wait_supervisor_exit(state, 5)
                raise WorkbenchError(state.get("error") or "启动失败，请查看本机日志")
            if state["phase"] == "RUNNING":
                result = rpc(state)
                if configuration.open_browser and not no_browser: webbrowser.open(result["api_url"])
                return result
        if child.poll() is not None: raise WorkbenchError("启动器已退出，请查看本机 launcher.log")
        time.sleep(0.15)
    state = read_state(directory)
    if state and state["instance"] == instance and owned_process(state, instance):
        try: rpc(state, "POST", "/stop")
        except (httpx.HTTPError, WorkbenchError): pass
    raise WorkbenchError("启动超时，已请求本实例退出；请用 status/stop 核实，未启动第二个实例")


def stop(configuration_file, directory):
    state = read_state(directory)
    if not state: return summary(state)
    if state["phase"] == "STOPPED":
        if wait_supervisor_exit(state, state["stop_timeout_seconds"]): return summary(state)
        return {**summary(state), "phase": "STOPPING"}
    if owned_process(state, state["instance"]):
        rpc(state, "POST", "/stop")
        deadline = time.monotonic() + state["stop_timeout_seconds"]
        while time.monotonic() < deadline:
            current = read_state(directory)
            if current["phase"] in TERMINAL:
                if wait_supervisor_exit(current, max(0.1, deadline - time.monotonic())): return summary(current)
                return {**summary(current), "phase": "STOPPING"}
            time.sleep(0.2)
        return {**summary(read_state(directory)), "phase": "STOPPING", "error": "停止仍在执行；未强杀活动 Runner，请稍后运行 status/stop"}
    # Recover a crashed supervisor without adopting any other process. Markers
    # belong to its random instance; maintenance also requires an exclusive gate.
    request_roles_stop(directory, state["instance"])
    configuration = Configuration.load(configuration_file)
    configuration.apply()
    local = state["storage"] == "sqlite"
    settings = Settings.load(Path(state["data_dir"]), sqlite=local, config_file=configuration_file)
    if not local and hashlib.sha256(settings.database_url.encode()).hexdigest() != state["database_fingerprint"]:
        raise WorkbenchError("外部数据库配置已变化，请还原原配置后核实旧实例")
    deadline = time.monotonic() + state["stop_timeout_seconds"]
    while any(owned_process(value, state["instance"], role) for role, value in state.get("roles", {}).items()):
        if time.monotonic() > deadline: return {**summary(state), "phase": "STOPPING", "error": "旧服务仍在退出；未操作其他 PID"}
        time.sleep(0.2)
    if state["stop_managed_environments"]: offline_stop(settings)
    state.update(phase="STOPPED", containers_stopped=state["stop_managed_environments"], error=None)
    write_json(directory / "instance.json", state)
    return summary(state)


def supervise(configuration, directory, instance):
    configuration.apply()
    settings = Settings.load(config_file=configuration.file)
    environment = os.environ.copy()
    environment.update(AWB_CONFIG_FILE="-", AWB_LAUNCHER_INSTANCE=instance, PYTHONUTF8="1")
    quit_requested = threading.Event()
    state = {**process_info(os.getpid()), "phase": "STARTING", "instance": instance, "control_token": secrets.token_urlsafe(32), "roles": {},
        "python_executable": sys.executable,
        "api_url": configuration.api_url, "data_dir": str(settings.data_dir), "logs_dir": str(configuration.logs_dir / instance),
        "stop_timeout_seconds": configuration.stop_timeout_seconds, "stop_managed_environments": configuration.stop_managed_environments,
        "storage": storage_summary(settings.database_url)["backend"], "database_fingerprint": hashlib.sha256(settings.database_url.encode()).hexdigest(), "error": None}
    children, logs = {}, []
    logs_dir = configuration.logs_dir / instance
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    def save(): write_json(directory / "instance.json", state)
    class Control(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self):
            if self.headers.get("Authorization") != "Bearer " + state["control_token"]:
                self.send_error(401); return
            if self.path not in ("/status", "/stop"):
                self.send_error(404); return
            if self.command == "POST" and self.path == "/stop": quit_requested.set()
            if self.command == "GET" and self.path != "/status": self.send_error(405); return
            data = json.dumps(summary(state), ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        do_GET = respond
        do_POST = respond
    control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    control.daemon_threads = True
    state["control_port"] = control.server_port
    with (directory / "run.lock").open("a+b") as run_lock:
        locking.flock(run_lock, locking.LOCK_EX | locking.LOCK_NB)
        save()
        serving = threading.Thread(target=control.serve_forever, daemon=True); serving.start()
        try:
            from .resources import controller_lock
            with controller_lock(settings.data_dir, exclusive=True): pass
            if not (configuration.web_dir / "index.html").is_file() or not (configuration.web_dir / "assets").is_dir(): raise WorkbenchError("前端尚未完整构建，请先运行 setup")
            if configuration.require_docker:
                from .runtime import docker
                if docker("info", "--format", "{{.OSType}}").stdout.strip() != "linux": raise WorkbenchError("请启动 Docker Desktop 的 Linux engine")
            def launch(role):
                handle = (logs_dir / (role + ".log")).open("ab"); logs.append(handle)
                process = subprocess.Popen([sys.executable, "-m", "agent_workbench.launcher", "_child", "--role", role, "--instance", instance,
                    "--stop-file", str(marker(directory, instance, role))], env=environment, cwd=Path(__file__).resolve().parents[2], stdout=handle, stderr=handle)
                children[role] = process
                state["roles"][role] = process_info(process.pid); save()
            launch("api")
            deadline = time.monotonic() + configuration.startup_timeout_seconds
            with httpx.Client(base_url=configuration.api_url, headers={"Authorization": "Bearer " + settings.runner_token}, trust_env=False, timeout=2) as client:
                ready = None
                while not quit_requested.is_set():
                    if any(child.poll() is not None for child in children.values()): raise WorkbenchError("服务进程提前退出；请检查日志、端口和配置")
                    try:
                        response = client.get("/internal/ready")
                        if response.status_code == 200:
                            observed = response.json()
                            if observed["instance"] == instance and descendant(observed["pid"], children["api"].pid): ready = observed
                    except (httpx.HTTPError, ValueError, KeyError): pass
                    if ready:
                        state["warning"] = ready.get("preset_warning")
                        state["roles"]["api"] = process_info(ready["pid"])
                        if "runner" not in children: launch("runner")
                        runner = next((item for item in ready["runners"] if item["instance"] == instance and descendant(item["pid"], children["runner"].pid)), None)
                        if runner:
                            state["roles"]["runner"] = process_info(runner["pid"]); break
                    if time.monotonic() > deadline: raise WorkbenchError("API 或 Runner 未在限定时间内就绪；请查看本机日志")
                    ready = None; time.sleep(0.15)
            if not quit_requested.is_set(): state["phase"] = "RUNNING"; save()
            while not quit_requested.wait(0.3):
                if any(child.poll() is not None for child in children.values()):
                    raise WorkbenchError("服务进程意外退出，已停止本实例其余服务，请核实执行记录")
        except Exception as error:
            state["error"] = str(error) if isinstance(error, WorkbenchError) else "启动或运行失败，请检查本机日志和配置；未输出凭据"
        finally:
            state["phase"] = "STOPPING"; save()
            # Keep the API alive until the Runner has canceled/completed its
            # current operation and recorded its result. Image commands drain.
            for role in ("runner", "api"):
                if role in children:
                    state["stopping_role"] = role; save()
                    write_json(marker(directory, instance, role), {"stop": True})
                    while children[role].poll() is None:
                        time.sleep(0.2)
            try:
                if configuration.stop_managed_environments and "runner" in children:
                    offline_stop(settings)
                    state["containers_stopped"] = True
                else: state["containers_stopped"] = False
                state["phase"] = "FAILED" if state["error"] else "STOPPED"
            except Exception:
                state.update(phase="NEEDS_ATTENTION", error="服务已退出，但资源或镜像任务尚未核实；请查看 stop/维护记录")
            state.pop("stopping_role", None)
            save()
            control.shutdown(); control.server_close(); serving.join(timeout=2)
            for handle in logs: handle.close()


def child(role, stop_file):
    event = threading.Event()
    stop_lock = threading.Lock()
    def stop():
        with stop_lock:
            if not event.is_set():
                faulthandler.dump_traceback_later(10)
                event.set()
    def watch():
        while not event.wait(0.2):
            try:
                if stop_file.exists(): stop()
            except OSError: continue  # A transient Windows sharing error must not lose stop delivery.
    threading.Thread(target=watch, daemon=True).start()
    sys.argv = [role, "--config", "-"]
    from .server import main as serve
    from .runner import main as run
    (serve if role == "api" else run)(stop_event=event)


def build_image(configuration, rebuild=False):
    configuration.apply()
    from .resources import controller_lock
    settings = Settings.load(config_file=configuration.file)
    with controller_lock(settings.data_dir, exclusive=True):
        return _build_image(configuration, rebuild)


def _build_image(configuration, rebuild):
    from .runtime import docker
    if docker("info", "--format", "{{.OSType}}").stdout.strip() != "linux": raise WorkbenchError("镜像构建需要本机 Linux Docker engine")
    if not rebuild and docker("image", "inspect", configuration.tool_image, check=False).returncode == 0:
        return {"image": configuration.tool_image, "already_exists": True}
    args = ["build", "--tag", configuration.tool_image]
    if configuration.proxy_ca: args += ["--secret", "id=proxy_ca,src=" + str(configuration.proxy_ca)]
    try: docker(*args, str(configuration.tool_context), timeout=configuration.image_build_timeout_seconds)
    except Exception: raise WorkbenchError("工具镜像构建失败，请检查 Dockerfile、代理及网络；构建输出未写入启动器日志") from None
    return {"image": configuration.tool_image, "built": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status", "build-image", "_supervise", "_child"])
    parser.add_argument("--config")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--instance", help=argparse.SUPPRESS)
    parser.add_argument("--role", choices=["api", "runner"], help=argparse.SUPPRESS)
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.action == "_child": child(args.role, args.stop_file); return 0
        path = config_path(args.config)
        directory = control_dir(path); directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.action == "_supervise": supervise(Configuration.load(args.config), directory, args.instance); return 0
        if args.action == "status": result = status(directory)
        else:
            with (directory / "operation.lock").open("a+b") as operation:
                try: locking.flock(operation, locking.LOCK_EX | locking.LOCK_NB)
                except BlockingIOError: raise WorkbenchError("另一个启动或停止命令正在执行，请先查看 status") from None
                result = stop(args.config, directory) if args.action == "stop" else build_image(Configuration.load(args.config), args.rebuild) if args.action == "build-image" else start(Configuration.load(args.config), directory, args.no_browser)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("phase") not in ("FAILED", "NEEDS_ATTENTION", "STALE", "UNREACHABLE", "STOPPING") else 2
    except (ConfigurationError, WorkbenchError) as error:
        print(str(error), file=sys.stderr); return 1
    except OSError as error:
        code = getattr(error, "winerror", None) or error.errno
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(f"启动管理操作失败（系统错误 {code}，{Path(frame.filename).name}:{frame.lineno}）；请检查本机日志和进程状态，未输出凭据", file=sys.stderr); return 1
    except Exception:
        print("启动管理操作失败；请检查配置、本机日志及进程状态，未输出凭据", file=sys.stderr); return 1


if __name__ == "__main__":
    sys.exit(main())
