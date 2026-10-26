"""A separate, trusted single-host Runner which pulls typed jobs from the API."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
import uuid

import httpx

from . import locking
from .execution import Canceled, Executor, Unknown
from .settings import Settings
from .workspace import WorkbenchError


class Runner:
    def __init__(self, settings, base_url="http://127.0.0.1:8340", executor=None, transport=None, shutdown_event=None):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "testserver") or parsed.username:
            raise ValueError("Runner 仅连接本机控制 API")
        self.settings = settings
        self.executor = executor or Executor(settings)
        self.owner = "runner_" + uuid.uuid4().hex
        self.shutdown_event = shutdown_event or threading.Event()
        self.client = httpx.Client(base_url=base_url, headers={"Authorization": "Bearer " + settings.runner_token},
            trust_env=False, timeout=15, transport=transport)

    def post(self, path, payload):
        response = self.client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def run_once(self):
        received = self.post("/internal/claim", {"owner": self.owner})
        job = received["job"]
        if not job:
            return False
        if received["model"] != self.settings.model:
            self.post(f"/internal/jobs/{job['id']}/finish", {"owner": self.owner, "epoch": job["epoch"],
                "status": "FAILED", "result": {}, "error": "API 与 Runner 的模型配置不一致"})
            return True
        base = {"owner": self.owner, "epoch": job["epoch"]}
        stop_heartbeat = threading.Event()
        canceled = threading.Event()
        lost = threading.Event()
        def heartbeat():
            last = time.monotonic()
            while not stop_heartbeat.wait(0.2):
                if self.shutdown_event.is_set(): canceled.set()
                if time.monotonic() - last < 3: continue
                last = time.monotonic()
                try:
                    observed = self.post(f"/internal/jobs/{job['id']}/heartbeat", base)
                    if observed["cancel_requested"]:
                        canceled.set()
                except httpx.HTTPError:
                    lost.set()
                    canceled.set()
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        sequence = 0
        def emit(kind, payload):
            nonlocal sequence
            sanitized = json.loads(self.executor.redact(json.dumps(payload, ensure_ascii=False), Path(job["workspace"]["path"])))
            self.post(f"/internal/jobs/{job['id']}/events", {**base, "seq": sequence, "kind": kind, "payload": sanitized})
            sequence += 1
        status, result, error = "SUCCEEDED", {}, None
        try:
            emit("stage", {"text": "执行器已接收操作"})
            result = self.executor.execute(job, emit, canceled)
            emit("stage", {"text": "操作完成，结果已收集"})
        except Exception as failure:
            status = "CANCELED" if isinstance(failure, Canceled) else "UNKNOWN" if isinstance(failure, Unknown) else "FAILED"
            workspace = Path(job["workspace"]["path"])
            error = self.executor.redact(str(failure), workspace)[:4000]
            if job["action"] == "delivery":
                result = {"delivery_observations": getattr(failure, "observations", []), "delivery_stopped": getattr(failure, "stopped", False)}
            else:
                try:
                    environment = None if job["action"].startswith("app_") and status == "FAILED" else "STOPPED" if status == "CANCELED" else "ERROR"
                    result = self.executor.observe(workspace, environment)
                except Exception:
                    result = {"environment": "UNKNOWN" if status == "UNKNOWN" else "ERROR"}
        finally:
            stop_heartbeat.set()
            thread.join(timeout=5)
        if lost.is_set():
            status = "UNKNOWN"
            error = "执行期间与控制服务失联；结果需停止并核实"
        try:
            self.post(f"/internal/jobs/{job['id']}/finish", {**base, "status": status, "result": result, "error": error})
        except httpx.HTTPStatusError as failure:
            if failure.response.status_code == 409 and status == "SUCCEEDED":
                self.post(f"/internal/jobs/{job['id']}/finish", {**base, "status": "FAILED", "result": result,
                    "error": "代码版本或执行租约已变化，结果未通过确认"})
            else:
                raise
        return True

    def watch_once(self):
        response = self.client.get("/internal/watch")
        response.raise_for_status()
        locks = self.settings.data_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        for workspace in response.json():
            with (locks / (workspace["requirement_id"] + ".lock")).open("a+b") as lock:
                try:
                    locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    observed = self.executor.observe(Path(workspace["path"]))
                    payload = {"snapshot": observed["snapshot"], "diff": observed["diff"], "diff_truncated": observed["diff_truncated"]}
                except (WorkbenchError, OSError, ValueError):
                    payload = {"unreadable": True}
                result = self.client.post(f"/internal/workspaces/{workspace['requirement_id']}/observation", json=payload)
                if result.status_code != 409:
                    result.raise_for_status()


    def report_once(self):
        from . import resources
        try:
            report = {"available": True, "controller_id": resources.controller_id(self.settings.data_dir), "containers": resources.inventory(self.settings.data_dir)}
        except (WorkbenchError, OSError):
            report = {"available": False, "controller_id": resources.controller_id(self.settings.data_dir), "containers": [], "error": "暂时无法核实 Docker 资源"}
        from .docker_execution import inventory
        try: report.update(images=inventory(), images_available=True)
        except (WorkbenchError, OSError, ValueError): report.update(images=[], images_available=False)
        report.update(pid=os.getpid(), launcher_instance=os.environ.get("AWB_LAUNCHER_INSTANCE"))
        self.post("/internal/runtime", {"owner": self.owner, "report": report})

    def image_once(self):
        from .docker_execution import run_once
        return run_once(self)


def main(stop_event=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config", help="统一 TOML 配置文件；- 表示只使用环境变量")
    parser.add_argument("--api")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from .configuration import configured
    configuration = configured(args)
    settings = Settings.load(args.data_dir, config_file=args.config)
    from .resources import controller_lock
    with controller_lock(settings.data_dir):
        stop_event = stop_event or threading.Event()
        runner = Runner(settings, args.api or configuration.api_url, shutdown_event=stop_event)
        print("Runner 已启动，等待本机控制服务分配任务", flush=True)
        last_watch = 0.0
        try:
            while not stop_event.is_set():
                try:
                    worked = runner.run_once()
                    if not worked: worked = runner.image_once()
                    if time.monotonic() - last_watch > 2:
                        runner.watch_once()
                        last_watch = time.monotonic()
                        runner.report_once()
                    if args.once:
                        return
                    if not worked:
                        time.sleep(0.5)
                except httpx.HTTPError:
                    if args.once:
                        raise
                    print("暂时无法更新执行状态，任务不会自动重放", flush=True)
                    time.sleep(2)
        finally:
            runner.client.close()


if __name__ == "__main__":
    main()
