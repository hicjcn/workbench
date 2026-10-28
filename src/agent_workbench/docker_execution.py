"""Runner-only Docker operations with explicitly selected local build contexts."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import time

import httpx
from . import locking
from . import runtime
from .platform_support import IS_WINDOWS, docker_argv, docker_environment
from .workspace import WorkbenchError
from . import logging_support as logs


def image_info(ref):
    result = runtime.docker("image", "inspect", ref, check=False)
    if result.returncode: return None
    item = json.loads(result.stdout)[0]
    return {"id": item["Id"], "tags": item.get("RepoTags") or [], "size": item.get("Size", 0), "created_at": item.get("Created", "")}


def inventory():
    identities = runtime.docker("image", "ls", "--no-trunc", "--quiet").stdout.split()
    if len(set(identities)) > 2000: raise WorkbenchError("镜像数量超过管理界面上限")
    return [item for identity in sorted(set(identities)) if (item := image_info(identity))]


def command(args, lock, source=None):
    env = docker_environment()
    started = time.monotonic()
    logs.event("images", "image.command", "正在执行镜像操作，等待 Docker 返回", operation=args[0])
    # Build output may contain application data or credentials. Never persist or expose it.
    with tempfile.TemporaryFile() as output:
        try:
            if IS_WINDOWS:
                from .docker_worker import run
                result = run(args, Path(lock.name).with_name("docker-command.lock"), source)
            else:
                result = subprocess.run(docker_argv(*args), stdin=source,
                    stdout=output, stderr=output, env=env, timeout=900, pass_fds=(lock.fileno(),))
        except (subprocess.TimeoutExpired, OSError):
            from .execution import Unknown
            raise Unknown("镜像操作超时或连接中断；请实际检查 Docker 后人工结束核实，禁止自动重试") from None
        logs.event("images", "image.command_returned", "镜像命令已返回", detailed=True, exit_code=result.returncode,
            duration_ms=round((time.monotonic()-started)*1000))
        if result.returncode: raise WorkbenchError(f"Docker 镜像操作失败（退出码 {result.returncode}）；请检查 Dockerfile、镜像引用和注册表访问；构建输出未写入工作台")


def execute(settings, job):
    from .execution import Unknown
    locks = settings.data_dir / "locks"; locks.mkdir(parents=True, exist_ok=True)
    with (locks / "docker-images.lock").open("a+b") as lock:
        try: locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
        except BlockingIOError: raise Unknown("旧镜像操作仍持有主机锁，禁止第二次执行") from None
        with (locks / "docker-command.lock").open("a+b") as command_lock:
            try: locking.flock(command_lock, locking.LOCK_EX | locking.LOCK_NB)
            except BlockingIOError: raise Unknown("旧 Docker 子进程仍在执行，禁止第二次操作") from None
        request = job["input"]; ref = request["reference"]
        build_context = None
        if job["kind"] == "build":
            if not ref.startswith("awb-user/"): raise WorkbenchError("界面构建目标必须使用 awb-user/ 前缀，避免覆盖其他工具镜像")
            directory = job.get("recipe", {}).get("context_dir", "")
            if directory:
                try:
                    raw = Path(directory).expanduser()
                    if not raw.is_absolute(): raise ValueError
                    build_context = raw.resolve(strict=True)
                    if not build_context.is_dir(): raise ValueError
                except (OSError, ValueError, RuntimeError):
                    raise WorkbenchError("构建上下文不是当前 Runner 可读取的本机绝对目录；请检查路径和访问权限") from None
            if image_info(ref): raise WorkbenchError("构建目标标签已经存在，请为新版本使用新标签")
            content = job["recipe"]["dockerfile"].encode()
            if len(content) > 65536: raise WorkbenchError("Dockerfile 超过 64 KiB")
            with tempfile.TemporaryFile() as source:
                if build_context:
                    source.write(content)
                else:
                    with tarfile.open(fileobj=source, mode="w") as archive:
                        entry = tarfile.TarInfo("Dockerfile"); entry.size = len(content); entry.mode = 0o600
                        archive.addfile(entry, io.BytesIO(content))
                source.seek(0)
                args = ["build", "--quiet", "--tag", ref]
                ca = os.environ.get("CODEX_PROXY_CERT")
                if ca: args += ["--secret", "id=proxy_ca,src=" + str(Path(ca).resolve(strict=True))]
                # Docker handles .dockerignore and context transfer. The saved
                # version is authoritative even if the directory has a Dockerfile.
                command([*args, "--file", "-", str(build_context)] if build_context else [*args, "-"], lock, source)
        elif job["kind"] == "pull": command(["pull", ref], lock)
        elif job["kind"] == "remove":
            with (locks / "runtime-admission.lock").open("a+b") as admission:
                locking.flock(admission, locking.LOCK_EX)
                current = image_info(ref)
                if not current or current["id"] != request["expected_image_id"]: raise WorkbenchError("镜像标签或 ID 已变化，请刷新后重新选择")
                for protected in job["protected_references"]:
                    protected_image = image_info(protected)
                    if protected == ref or (protected_image and protected_image["id"] == current["id"]): raise WorkbenchError("镜像被项目或历史运行配置引用，不能从界面移除")
                if runtime.docker("ps", "-a", "--filter", "ancestor=" + current["id"], "--format", "{{.ID}}").stdout.strip(): raise WorkbenchError("镜像仍被容器引用，不能移除")
                # No force, prune or volume deletion, even for unregistered containers.
                command(["image", "rm", ref], lock)
                return {"reference": ref, "removed_reference": True, "expected_image_id": current["id"]}
        else: raise WorkbenchError("镜像操作类型不支持")
        result = image_info(ref)
        if not result: raise Unknown("Docker 命令结束但镜像无法确认，请人工核实")
        return {"reference": ref, "image": result, "model_execution": "NOT_ATTEMPTED",
            **({"build_context": {"directory": str(build_context) if build_context else None, "dockerfile_source": "saved_recipe"}} if job["kind"] == "build" else {})}


def run_once(runner):
    received = runner.post("/internal/docker/claim", {"owner": runner.owner})
    job = received["job"]
    if not job: return False
    with logs.context(job_id=job['id'], action=job['kind']):
        return run_job(runner, job)


def run_job(runner, job):
    started = time.monotonic()
    logs.event("images", "image.claimed", "已领取镜像管理任务")
    base = {"owner": runner.owner, "epoch": job["epoch"]}
    stopped = threading.Event(); lost = threading.Event()
    def heartbeat():
        reported = time.monotonic()
        while not stopped.wait(3):
            try:
                runner.post(f"/internal/docker/{job['id']}/heartbeat", base)
                now = time.monotonic()
                if now-reported >= 15:
                    logs.event("images", "image.wait", "镜像操作仍在执行，租约已续期", detailed=True,
                        job_id=job['id'], action=job['kind'], elapsed_ms=round((now-started)*1000))
                    reported = now
            except httpx.HTTPError: lost.set(); return
    thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
    status, result, error = "SUCCEEDED", {}, None
    try: result = execute(runner.settings, job)
    except Exception as failure:
        from .execution import Unknown
        status = "UNKNOWN" if isinstance(failure, Unknown) else "FAILED"
        # Failure text is fixed by this module; do not forward Docker stdout/stderr or env.
        error = str(failure)[:1000] if isinstance(failure, WorkbenchError) else "镜像操作失败，请检查 Runner 和 Docker"
    finally: stopped.set(); thread.join(timeout=5)
    if lost.is_set():
        logs.event("images", "image.lease_lost", "镜像任务租约丢失，结果需要人工核实", level=30)
        return True  # The original request is retained until its lease expires; never replay.
    runner.post(f"/internal/docker/{job['id']}/finish", {**base, "status": status, "result": result, "error": error})
    logs.event("images", "image.finished", "镜像管理任务已结束", level=20 if status=='SUCCEEDED' else 30,
        status=status, duration_ms=round((time.monotonic()-started)*1000))
    return True
