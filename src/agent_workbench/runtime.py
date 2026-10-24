"""A local Docker runtime with explicit workspace mounts and no host shell."""

from __future__ import annotations

import hashlib
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from .opencode import OpenCodeClient
from .workspace import WorkbenchError, load_workspace, safe_path, write_json


def exclusive_workspace(function):
    @wraps(function)
    def wrapped(workspace: Path, *args, **kwargs):
        workspace, _ = load_workspace(workspace)
        with (workspace / ".runtime.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkbenchError("Another runtime operation owns this workspace") from None
            return function(workspace, *args, **kwargs)
    return wrapped


def docker(*args: str, environment: dict | None = None, check: bool = True,
           timeout: float = 120) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    if environment:
        env.update(environment)
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    if check and result.returncode:
        # Do not include argv/env: credentials are passed by environment name.
        raise WorkbenchError(f"Docker operation failed (exit {result.returncode}): {result.stderr.strip()[:1500]}")
    return result


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


@exclusive_workspace
def start(workspace: Path, image: str, proxy_ca: Path | None = None, *,
          openrouter_model: str | None = None, opencode_model: str | None = None) -> dict:
    workspace, manifest = load_workspace(workspace)
    configuration = {"plugin": ["/opt/superpowers"], "permission": "allow"}
    provider_environment = {}
    if openrouter_model is not None and opencode_model is not None:
        raise WorkbenchError("Select only one verification provider")
    if opencode_model is not None:
        configuration = opencode_free_config(opencode_model)
    if openrouter_model is not None:
        configuration = openrouter_config(openrouter_model)
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise WorkbenchError("Configure the existing OPENROUTER_API_KEY secret before starting OpenRouter")
        provider_environment["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    suffix = hashlib.sha256(str(workspace).encode()).hexdigest()[:10]
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
        docker("network", "create", "--label", "awb.managed=true", network)
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
             "phase": "PREPARING", "image": image}
    write_json(workspace / "runtime.json", state, private=True)
    command = [
        "run", "--detach", "--name", container,
        "--label", "awb.managed=true", "--label", f"awb.workspace={suffix}",
        "--network", network, "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--cpus", "1", "--memory", "1536m", "--pids-limit", "256",
        "--publish", "127.0.0.1::4096", "--workdir", "/workspace/project",
        "--mount", f"type=bind,src={workspace / 'repo'},dst=/workspace/project",
        "--mount", f"type=bind,src={home},dst=/home/node",
        "--mount", f"type=bind,src={staging},dst=/workbench/out",
        "--env", "HOME=/home/node", "--env", "OPENCODE_SERVER_USERNAME=workbench",
        "--env", "OPENCODE_SERVER_PASSWORD",
    ]
    for name in provider_environment:
        command += ["--env", name]  # Pass the name, never the credential value, in argv.
    # Docker's configured proxy environment remains in effect. Mount session CA only at runtime.
    if proxy_ca:
        command += ["--mount", f"type=bind,src={proxy_ca.resolve(strict=True)},dst=/run/proxy-ca.pem,readonly",
                    "--env", "NODE_EXTRA_CA_CERTS=/run/proxy-ca.pem",
                    "--env", "SSL_CERT_FILE=/run/proxy-ca.pem",
                    "--env", "GIT_SSL_CAINFO=/run/proxy-ca.pem"]
    command += [image, "opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
    try:
        docker(*command, environment={"OPENCODE_SERVER_PASSWORD": password, **provider_environment})
        address = docker("port", container, "4096/tcp").stdout.strip()
        if not address.startswith("127.0.0.1:"):
            raise WorkbenchError("OpenCode port was not bound to loopback")
        state.update({"phase": "STARTED", "base_url": f"http://{address}"})
        write_json(workspace / "runtime.json", state, private=True)
        return {key: state[key] for key in ("container", "network", "phase", "base_url", "image")}
    except BaseException:
        # Retain the journal so a failed run is inspectable and can be explicitly stopped.
        state["phase"] = "NEEDS_RECONCILIATION"
        write_json(workspace / "runtime.json", state, private=True)
        raise


def probe(workspace: Path) -> dict:
    workspace, _ = load_workspace(workspace)
    state = json.loads((workspace / "runtime.json").read_text())
    if state.get("phase") != "STARTED":
        raise WorkbenchError("Runtime is not ready for capability probing")
    report = OpenCodeClient(state["base_url"], state["password"]).probe()
    details = json.loads(docker("inspect", state["container"]).stdout)[0]
    mounts = details["Mounts"]
    report["runtime"] = {
        "image_id": details["Image"], "user": details["Config"]["User"],
        "privileged": details["HostConfig"]["Privileged"],
        "readonly_rootfs": details["HostConfig"]["ReadonlyRootfs"],
        "docker_socket_mounted": any(mount["Destination"] == "/var/run/docker.sock" for mount in mounts),
        "network": state["network"],
        "egress_restriction_enforced": False,
    }
    report["model_execution"] = "NOT_ATTEMPTED"
    write_json(workspace / "capability-report.json", report)
    return report


@exclusive_workspace
def stop(workspace: Path) -> dict:
    workspace, _ = load_workspace(workspace)
    path = workspace / "runtime.json"
    state = json.loads(path.read_text())
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

