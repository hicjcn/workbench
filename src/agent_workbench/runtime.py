"""A local Docker runtime with explicit workspace mounts and no host shell."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import time

from . import locking
from . import logging_support as logs
from .opencode import OpenCodeClient
from .platform_support import bind_mount, container_ids, container_user, docker_argv, docker_environment
from .container_shell import git_safe_environment, shell_argv
from .workspace import WorkbenchError, load_workspace, safe_path, write_json

_inspection_stop = ContextVar("docker_inspection_stop", default=None)


@contextmanager
def interruptible_inspection(stop_event):
    """Only read-only discovery may be interrupted during controller shutdown."""
    token = _inspection_stop.set(stop_event)
    try: yield
    finally: _inspection_stop.reset(token)


def inspection_command(args, environment, timeout, stop_event):
    if not (args[0] in ("ps", "inspect") or args[:2] in (("image", "ls"), ("image", "inspect"), ("network", "ls"), ("network", "inspect"), ("volume", "inspect"))):
        raise WorkbenchError("资源查询上下文不允许修改 Docker 资源")
    if stop_event.is_set(): raise WorkbenchError("资源状态查询已停止")
    argv = docker_argv(*args)
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=environment) as process:
        deadline = time.monotonic() + timeout
        try:
            while True:
                if stop_event.is_set(): raise WorkbenchError("资源状态查询已停止")
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    output, error = process.communicate(timeout=min(0.2, remaining))
                    return subprocess.CompletedProcess(argv, process.returncode, output, error)
                except subprocess.TimeoutExpired: pass
        finally:
            if process.poll() is None:
                # This is our own read-only CLI query, never a build/run command.
                process.kill()
                process.communicate()


def exclusive_workspace(function):
    @wraps(function)
    def wrapped(workspace: Path, *args, **kwargs):
        workspace, _ = load_workspace(workspace)
        with (workspace / ".runtime.lock").open("a+b") as lock:
            try:
                locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
            except BlockingIOError:
                raise WorkbenchError("Another runtime operation owns this workspace") from None
            return function(workspace, *args, **kwargs)
    return wrapped


def docker(*args: str, environment: dict | None = None, check: bool = True,
           timeout: float = 120) -> subprocess.CompletedProcess:
    env = docker_environment(environment)
    started = time.monotonic()
    operation = args[0] if args else "unknown"
    if operation in ("network", "image", "volume") and len(args) > 1: operation += "." + args[1]
    logs.event("docker", "docker.command", "正在执行 Docker 操作", detailed=True, operation=operation)
    stop_event = _inspection_stop.get()
    result = inspection_command(args, env, timeout, stop_event) if stop_event is not None else subprocess.run(
        docker_argv(*args),
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout,
    )
    logs.event("docker", "docker.result", "Docker 操作已返回", detailed=True, operation=operation,
        exit_code=result.returncode, duration_ms=round((time.monotonic()-started)*1000))
    if check and result.returncode:
        # Do not include argv/env: credentials are passed by environment name.
        raise WorkbenchError(f"Docker operation failed (exit {result.returncode}): {result.stderr.strip()[:1500]}")
    return result


def docker_stream(*args: str, source=None, destination=None, timeout=120):
    """Stream private binary data without loading it into logs or command arguments."""
    env = docker_environment()
    result = subprocess.run(docker_argv(*args), stdin=source,
        stdout=destination if destination is not None else subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=timeout)
    if result.returncode: raise WorkbenchError(f"数据库流式导出或导入失败（退出码 {result.returncode}）；保留现场，不记录数据内容")


def doctor() -> dict:
    result = docker("info", "--format", "{{json .ServerVersion}}", check=False)
    return {"docker_available": result.returncode == 0,
            "docker_version": json.loads(result.stdout) if result.returncode == 0 else None,
            "diagnostic": None if result.returncode == 0 else result.stderr.strip()[:1000]}


def openrouter_config(model: str) -> dict:
    if not re.fullmatch(r"openrouter/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+:free", model):
        raise WorkbenchError("OpenRouter verification requires an explicit openrouter/vendor/model:free ID")
    return {
        "plugin": ["/opt/superpowers"], "permission": "allow",
        "enabled_providers": ["openrouter"], "model": model, "small_model": model,
        "provider": {"openrouter": {
            "whitelist": [model.removeprefix("openrouter/")],
            "options": {"apiKey": "{env:OPENROUTER_API_KEY}"},
        }},
    }


def opencode_free_config(model: str) -> dict:
    if not re.fullmatch(r"opencode/(?:[A-Za-z0-9._-]+-free|big-pickle)", model):
        raise WorkbenchError("OpenCode verification requires an explicit free model ID")
    return {
        "plugin": ["/opt/superpowers"], "permission": "allow",
        "enabled_providers": ["opencode"], "model": model, "small_model": model,
        "provider": {"opencode": {"whitelist": [model.removeprefix("opencode/")]}},
    }


def model_config(model: str, read_only: bool = False, context_paths: list | None = None) -> dict:
    config = openrouter_config(model) if model.startswith("openrouter/") else opencode_free_config(model)
    if read_only:
        config["permission"] = {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow"}
        if context_paths:
            if any(not re.fullmatch(r"/workspace/candidates/prj_[a-f0-9]{32}", path) for path in context_paths):
                raise WorkbenchError("只读上下文路径不正确")
            config["permission"]["external_directory"] = {path + "/*": "allow" for path in context_paths}
    elif context_paths: raise WorkbenchError("多项目上下文仅用于只读角色")
    return config


def matches_model_config(actual, expected):
    if not isinstance(actual, dict): return False
    actual = dict(actual)
    # OpenCode 1.19.4 writes its editor schema on first use; it does not change permissions.
    if actual.get("$schema") == "https://opencode.ai/config.json": actual.pop("$schema")
    return actual == expected


@exclusive_workspace
def start(workspace: Path, image: str, proxy_ca: Path | None = None, *,
          openrouter_model: str | None = None, opencode_model: str | None = None, read_only_source: bool = False, controller: str | None = None,
          limits: dict | None = None, config_mounts: list | None = None,
          dependency_network: str | None = None, dependency_environment: dict | None = None, context_mounts: list | None = None, agent_cli: dict | None = None,
          volume_mounts: list | None = None, profile_environment: dict | None = None, port_mappings: list | None = None,
          run_as_root: bool = False) -> dict:
    workspace, manifest = load_workspace(workspace)
    fixed_cli = manifest.get("agent_cli")
    if agent_cli is None:
        agent_cli = fixed_cli
    elif fixed_cli and (agent_cli.get("id"), agent_cli.get("content_digest")) != (fixed_cli.get("id"), fixed_cli.get("content_digest")):
        raise WorkbenchError("运行时 Agent CLI 与工作区固定版本不一致")
    configuration = {"plugin": ["/opt/superpowers"], "permission": "allow"}
    provider_environment = {}
    if agent_cli:
        from .agent_clis import checked, command as agent_command
        cli_content = checked(agent_cli)
        serve_argv = agent_command(agent_cli, "serve")
    else:
        # Keep the standalone M1 runtime usable without controller dependencies.
        cli_content = {"credential_env": []}
        serve_argv = ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
    for name in cli_content["credential_env"]:
        if name in os.environ: provider_environment[name] = os.environ[name]
    if openrouter_model is not None and opencode_model is not None:
        raise WorkbenchError("Select only one verification provider")
    if opencode_model is not None:
        configuration = opencode_free_config(opencode_model)
    if openrouter_model is not None:
        configuration = openrouter_config(openrouter_model)
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise WorkbenchError("Configure the existing OPENROUTER_API_KEY secret before starting OpenRouter")
        provider_environment["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    if read_only_source:
        if not (openrouter_model or opencode_model):
            raise WorkbenchError("Read-only roles require an explicit model")
        configuration = model_config(openrouter_model or opencode_model, True, [item["target"] for item in context_mounts or []])
    elif context_mounts: raise WorkbenchError("多项目上下文仅用于只读角色")
    suffix = hashlib.sha256(str(workspace).encode()).hexdigest()[:10]
    limits = limits or {"cpus": 1, "memory_mib": 1536, "pids_limit": 256}
    requirement_suffix = hashlib.sha256(str(workspace.parent).encode()).hexdigest()[:10]
    container = f"awb-{manifest['requirement'][:20]}-{manifest['project'][:20]}-{suffix}"
    network = f"awb-req-{requirement_suffix}"
    if (workspace / "runtime.json").exists():
        raise WorkbenchError("Runtime state already exists; stop the recorded runtime before starting another")
    # Persisted Agent state is untrusted input when interpreted on the host.
    home = safe_path(workspace, "agent-home")
    config = safe_path(workspace, "agent-home/.config/opencode")
    config_file = safe_path(workspace, "agent-home/.config/opencode/opencode.json")
    staging = safe_path(workspace, "staging")
    found = docker("network", "inspect", network, check=False)
    if found.returncode:
        docker("network", "create", "--label", "awb.managed=true", *(["--label", f"awb.controller={controller}"] if controller else []), network)
    else:
        labels = json.loads(found.stdout)[0].get("Labels") or {}
        if labels.get("awb.managed") != "true":
            raise WorkbenchError("Network name belongs to an unmanaged resource")
    config.mkdir(parents=True, exist_ok=True)
    if not config_file.exists() or openrouter_model is not None or opencode_model is not None:
        # Reapply the selected model restrictions even when Agent state was persisted.
        write_json(config_file, configuration)
    staging.mkdir(exist_ok=True)
    password = secrets.token_urlsafe(32)
    state = {"container": container, "network": network, "password": password,
             "phase": "PREPARING", "image": image, "run_as_root": run_as_root, "serve_argv": serve_argv}
    if port_mappings: state["port_mappings"] = port_mappings
    if agent_cli: state["agent_cli"] = agent_cli
    if context_mounts: state["context_paths"] = [item["target"] for item in context_mounts]
    write_json(workspace / "runtime.json", state, private=True)
    command = [
        "run", "--detach", "--name", container,
        "--entrypoint=/bin/bash",
        "--label", "awb.managed=true", "--label", f"awb.workspace={suffix}",
        "--label", "awb.kind=agent",
        "--network", network, "--user", f"0:{container_ids()[1]}" if run_as_root else container_user(),
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--cpus", str(limits["cpus"]), "--memory", str(limits["memory_mib"]) + "m", "--pids-limit", str(limits["pids_limit"]),
        "--publish", "127.0.0.1::4096", "--workdir", "/workspace/project",
        "--mount", bind_mount(workspace / "repo", "/workspace/project", readonly=read_only_source),
        "--mount", bind_mount(home, "/home/node"),
        "--mount", bind_mount(staging, "/workbench/out"),
        "--env", "HOME=/home/node", "--env", "OPENCODE_SERVER_USERNAME=workbench",
        "--env", "OPENCODE_SERVER_PASSWORD",
    ]
    if run_as_root:
        command += ["--cap-add", "DAC_OVERRIDE"]
        for key, value in git_safe_environment(workspace, context_mounts or []).items():
            command += ["--env", key + "=" + value]
    for name in provider_environment:
        command += ["--env", name]  # Pass the name, never the credential value, in argv.
    for name in dependency_environment or {}: command += ["--env", name]
    for name in profile_environment or {}: command += ["--env", name]
    if port_mappings:
        from .runtime_ports import publish_args
        command += publish_args(port_mappings)
    if controller: command += ["--label", f"awb.controller={controller}"]
    for mount in config_mounts or []:
        command += ["--mount", bind_mount(mount["source"], mount["target"], readonly=True)]
    for mount in volume_mounts or []:
        command += ["--mount", bind_mount(mount["source"], mount["target"], readonly=read_only_source or mount.get("read_only", True))]
    for mount in context_mounts or []:
        command += ["--mount", bind_mount(mount["source"], mount["target"], readonly=True)]
    # Docker's configured proxy environment remains in effect. Mount session CA only at runtime.
    if proxy_ca:
        command += ["--mount", bind_mount(proxy_ca.resolve(strict=True), "/run/proxy-ca.pem", readonly=True),
                    "--env", "NODE_EXTRA_CA_CERTS=/run/proxy-ca.pem",
                    "--env", "SSL_CERT_FILE=/run/proxy-ca.pem",
                    "--env", "GIT_SSL_CAINFO=/run/proxy-ca.pem"]
    command += [image, *shell_argv(serve_argv, root=run_as_root)[1:]]
    try:
        try:
            docker(*command, environment={**(profile_environment or {}), "OPENCODE_SERVER_PASSWORD": password, **provider_environment, **(dependency_environment or {})})
        except WorkbenchError as error:
            hint = ""
            if "executable file not found" in str(error) or "no such file or directory" in str(error):
                hint = " 请检查镜像是否提供 /bin/bash、初始化文件是否可读，以及程序是否安装在初始化后的 PATH 中，或使用容器内绝对路径。/root/.nvm 等 root 安装需要启用 root 运行模式；命令参数不会展开 Shell 变量，独立 Agent HOME 仍位于 /home/node。"
            raise WorkbenchError(f"Agent CLI「{agent_cli['name'] if agent_cli else 'OpenCode（内置）'}」启动失败；程序：{serve_argv[0]}；镜像：{image}；实际命令：{shlex.join(serve_argv)}。{error}{hint}") from None
        if dependency_network: docker("network", "connect", dependency_network, container)
        address = docker("port", container, "4096/tcp").stdout.strip()
        if not address.startswith("127.0.0.1:"):
            raise WorkbenchError("OpenCode port was not bound to loopback")
        state.update({"phase": "STARTED", "base_url": f"http://{address}"})
        write_json(workspace / "runtime.json", state, private=True)
        return {**{key: state[key] for key in ("container", "network", "phase", "base_url", "image")}, "port_mappings": state.get("port_mappings", [])}
    except BaseException:
        # Retain the journal so a failed run is inspectable and can be explicitly stopped.
        state["phase"] = "NEEDS_RECONCILIATION"
        write_json(workspace / "runtime.json", state, private=True)
        raise


def probe(workspace: Path) -> dict:
    workspace, _ = load_workspace(workspace)
    state = json.loads((workspace / "runtime.json").read_text(encoding="utf-8"))
    if state.get("phase") != "STARTED":
        raise WorkbenchError("Runtime is not ready for capability probing")
    report = OpenCodeClient(state["base_url"], state["password"]).probe()
    if state.get("agent_cli"):
        report["agent_cli"] = {key: state["agent_cli"][key] for key in ("id", "name", "number", "content_digest")}
    details = json.loads(docker("inspect", state["container"]).stdout)[0]
    mounts = details["Mounts"]
    report["runtime"] = {
        "image_id": details["Image"], "user": details["Config"]["User"],
        "privileged": details["HostConfig"]["Privileged"],
        "readonly_rootfs": details["HostConfig"]["ReadonlyRootfs"],
        "docker_socket_mounted": any(mount["Destination"] == "/var/run/docker.sock" for mount in mounts),
        "source_read_only": any(mount["Destination"] == "/workspace/project" and not mount["RW"] for mount in mounts),
        "network": state["network"],
        "egress_restriction_enforced": False,
    }
    report["model_execution"] = "NOT_ATTEMPTED"
    write_json(workspace / "capability-report.json", report)
    return report


def published_ports(workspace: Path) -> list:
    """Expose only resolved public bindings, never the private runtime journal."""
    path = workspace / "runtime.json"
    try:
        from .platform_support import is_link
        if is_link(path): return []
        state = json.loads(path.read_text(encoding="utf-8"))
        result = []
        for item in state.get("port_mappings", []):
            if (isinstance(item, dict) and type(item.get("container_port")) is int and type(item.get("host_port")) is int and
                1 <= item["container_port"] <= 65535 and 1024 <= item["host_port"] <= 65535 and item.get("protocol") in ("tcp", "udp")):
                result.append({"container_port": item["container_port"], "host_port": item["host_port"], "protocol": item["protocol"], "service": "agent"})
        return result
    except (OSError, ValueError, TypeError, AttributeError): return []


@exclusive_workspace
def stop(workspace: Path) -> dict:
    workspace, _ = load_workspace(workspace)
    path = workspace / "runtime.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    inspected = docker("inspect", state["container"], check=False)
    if inspected.returncode == 0:
        details = json.loads(inspected.stdout)[0]
        expected = hashlib.sha256(str(workspace).encode()).hexdigest()[:10]
        if details["Config"]["Labels"].get("awb.workspace") != expected:
            raise WorkbenchError("Refusing to stop a container owned by a different workspace")
        docker("rm", "--force", state["container"])
    elif "No such" not in inspected.stderr:
        raise WorkbenchError("Cannot verify runtime existence; retaining state")
    # Removing a shared network fails harmlessly if another project still uses it.
    docker("network", "rm", state["network"], check=False)
    path.unlink()
    return {"stopped": True, "code_and_agent_state_preserved": True}

