"""A separate, trusted single-host Runner which pulls typed jobs from the API."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback
from urllib.parse import urlparse
import uuid

import httpx

from . import locking
from .execution import Canceled, Executor, Unknown
from .settings import Settings
from .workspace import WorkbenchError
from . import logging_support as logs


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
        with logs.context(job_id=job["id"], requirement_id=job["requirement_id"], action=job["action"], operation=job.get("step_key")):
            return self.run_job(job, received["model"])

    def run_job(self, job, model):
        started = time.monotonic()
        current_step = "job.claimed"
        logs.event("runner", current_step, "已领取任务，开始执行")
        if model != self.settings.model:
            self.post(f"/internal/jobs/{job['id']}/finish", {"owner": self.owner, "epoch": job["epoch"],
                "status": "FAILED", "result": {}, "error": "API 与 Runner 的模型配置不一致"})
            logs.event("runner", "job.finished", "API 与 Runner 模型配置不一致，任务已拒绝", level=40, status="FAILED")
            return True
        base = {"owner": self.owner, "epoch": job["epoch"]}
        def resolve_sync(committed=None, *, rollback=False):
            from .profile_sync import resolve
            directory = self.settings.data_dir / "locks"; directory.mkdir(parents=True, exist_ok=True)
            with (directory / (job["requirement_id"] + ".lock")).open("a+b") as handle:
                locking.flock(handle, locking.LOCK_EX)
                resolve(Path(job["workspace"]["path"]), committed, rollback=rollback, expected_job_id=job["id"])
        stop_heartbeat = threading.Event()
        canceled = threading.Event()
        lost = threading.Event()
        def heartbeat():
            last = time.monotonic()
            reported = last
            while not stop_heartbeat.wait(0.2):
                if self.shutdown_event.is_set(): canceled.set()
                if time.monotonic() - last < 3: continue
                last = time.monotonic()
                try:
                    observed = self.post(f"/internal/jobs/{job['id']}/heartbeat", base)
                    if observed["cancel_requested"]:
                        canceled.set()
                    if last - reported >= 15:
                        logs.event("runner", current_step, "任务仍在执行，租约已续期", detailed=True,
                            job_id=job["id"], requirement_id=job["requirement_id"], action=job["action"], elapsed_ms=round((last-started)*1000))
                        reported = last
                except httpx.HTTPError:
                    logs.event("runner", "job.lease_lost", "无法续期任务，正在请求停止，结果需要核实", level=30,
                        job_id=job["id"], requirement_id=job["requirement_id"], action=job["action"])
                    lost.set()
                    canceled.set()
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        sequence = 0
        def emit(kind, payload):
            nonlocal sequence, current_step
            if payload.get("level") == "DEBUG" and self.settings.log_mode != "detailed": return
            sanitized = self.executor.redact_payload(payload, Path(job["workspace"]["path"]))
            if kind == "stage":
                current_step = sanitized.get("step", job["action"] + "." + kind)
                logs.event("runner", current_step, sanitized.get("text", "执行阶段"), detailed=sanitized.get("level") == "DEBUG")
            else:
                logs.event("runner", "job.output", "已收集执行事件", detailed=True, event_kind=kind,
                    bytes=len(json.dumps(sanitized, ensure_ascii=False).encode("utf-8")))
            self.post(f"/internal/jobs/{job['id']}/events", {**base, "seq": sequence, "kind": kind, "payload": sanitized})
            sequence += 1
        status, result, error = "SUCCEEDED", {}, None
        try:
            logs.progress(emit, "job.received", "执行器已接收操作")
            result = self.executor.execute(job, emit, canceled)
            logs.progress(emit, "job.collected", "操作完成，结果已收集")
        except Exception as failure:
            status = "CANCELED" if isinstance(failure, Canceled) else "UNKNOWN" if isinstance(failure, Unknown) else "FAILED"
            workspace = Path(job["workspace"]["path"])
            error = self.executor.redact(str(failure), workspace)[:4000]
            logs.event("runner", current_step, "任务中断，正在收集现场状态", level=30, status=status, error_type=type(failure).__name__)
            logs.event("runner", "job.failure_location", "失败位置", detailed=True,
                frames=";".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(failure.__traceback__)[-8:]))
            if job["action"] == "delivery":
                result = {"delivery_observations": getattr(failure, "observations", []), "delivery_stopped": getattr(failure, "stopped", False)}
            else:
                try:
                    environment = None if (job["action"] == "list_models" and status in ("FAILED", "CANCELED")) or (job["action"].startswith("app_") and status == "FAILED") else "STOPPED" if status == "CANCELED" else "ERROR"
                    if status == "FAILED":
                        from .runtime import describe_debug_runtime
                        if describe_debug_runtime(workspace): environment = "DEBUG_RUNNING"
                    result = self.executor.observe(workspace, environment)
                except Exception:
                    result = {"environment": "UNKNOWN" if status == "UNKNOWN" else "ERROR"}
                if hasattr(failure, 'command_output'): result['command_output'] = failure.command_output
        finally:
            stop_heartbeat.set()
            thread.join(timeout=5)
        if lost.is_set():
            status = "UNKNOWN"
            error = "执行期间与控制服务失联；结果需停止并核实"
        if job["action"] == "sync_profile" and status != "SUCCEEDED":
            try:
                resolve_sync(rollback=True)
            except Exception:
                status, error = "UNKNOWN", "配置同步回滚尚未核实，请停止并核实当前需求"
        try:
            logs.event("runner", "job.persist", "正在将执行结果写回控制服务", detailed=True, status=status)
            self.post(f"/internal/jobs/{job['id']}/finish", {**base, "status": status, "result": result, "error": error})
        except httpx.HTTPStatusError as failure:
            if failure.response.status_code == 409 and status == "SUCCEEDED":
                if job["action"] == "sync_profile":
                    resolve_sync(rollback=True)
                self.post(f"/internal/jobs/{job['id']}/finish", {**base, "status": "FAILED", "result": result,
                    "error": "代码版本或执行租约已变化，结果未通过确认"})
                status = "FAILED"
            else:
                raise
        if job["action"] == "sync_profile" and status == "SUCCEEDED":
            try: resolve_sync(job["id"])
            except (OSError, WorkbenchError): pass  # The acknowledged binding lets the next job finish cleanup.
        logs.event("runner", "job.finished", "任务已结束，结果已登记", level=20 if status == "SUCCEEDED" else 30,
            status=status, duration_ms=round((time.monotonic()-started)*1000))
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
        from .runtime import interruptible_inspection
        from .docker_execution import inventory
        with interruptible_inspection(self.shutdown_event):
            try:
                report = {"available": True, "controller_id": resources.controller_id(self.settings.data_dir), "containers": resources.inventory(self.settings.data_dir)}
            except (WorkbenchError, OSError, subprocess.TimeoutExpired):
                report = {"available": False, "controller_id": resources.controller_id(self.settings.data_dir), "containers": [], "error": "暂时无法核实 Docker 资源"}
            try: report.update(images=inventory(), images_available=True)
            except (WorkbenchError, OSError, ValueError, subprocess.TimeoutExpired): report.update(images=[], images_available=False)
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
    logs.configure(settings.log_mode, secrets=(settings.access_token, settings.runner_token))
    from .resources import controller_lock
    with controller_lock(settings.data_dir):
        stop_event = stop_event or threading.Event()
        runner = Runner(settings, args.api or configuration.api_url, shutdown_event=stop_event)
        logs.event("runner", "runner.ready", "Runner 已启动，等待本机控制服务分配任务", mode=settings.log_mode, pid=os.getpid())
        last_watch = 0.0
        try:
            while not stop_event.is_set():
                try:
                    worked = runner.run_once()
                    if not worked and not stop_event.is_set(): worked = runner.image_once()
                    if stop_event.is_set(): break
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
                    logs.event("runner", "runner.connection", "暂时无法更新执行状态，任务不会自动重放", level=30)
                    time.sleep(2)
        finally:
            runner.client.close()
            logs.event("runner", "runner.stopped", "Runner 已停止，当前任务处理已退出")


if __name__ == "__main__":
    main()
