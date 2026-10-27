"""Loopback-only port publication within the controller's configured range."""
import json
import socket

from . import runtime
from .workspace import WorkbenchError


def occupied_ports():
    """Include stopped containers: Docker still reserves their configured intent."""
    identifiers = runtime.docker("ps", "--all", "--quiet").stdout.split()
    result = set()
    for offset in range(0, len(identifiers), 100):
        details = json.loads(runtime.docker("inspect", *identifiers[offset:offset + 100]).stdout)
        for detail in details:
            for key, bindings in (detail.get("HostConfig", {}).get("PortBindings") or {}).items():
                protocol = key.rsplit("/", 1)[-1]
                for binding in bindings or []:
                    port = binding.get("HostPort", "")
                    if str(port).isdigit(): result.add((int(port), protocol))
    return result


def available(port, protocol):
    kind = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
    try:
        with socket.socket(socket.AF_INET, kind) as candidate:
            candidate.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def allocate(mappings, settings, *, reserved=()):
    """Called while holding runtime-admission.lock, through Docker creation.

    Docker performs the final atomic bind, so an unrelated host process racing
    this check fails the run; publication never falls back outside the range.
    ``reserved`` contains (host_port, protocol) pairs already selected by caller.
    """
    if not mappings: return []
    low, high = settings.port_range_start, settings.port_range_end
    if not 1024 <= low <= high <= 65535: raise WorkbenchError("宿主端口范围配置无效")
    result = [dict(item) for item in mappings]
    used = occupied_ports() | set(reserved)
    # Explicit requests claim ports first, even when preceded by automatic rows.
    for mapping in sorted(result, key=lambda item: item.get("host_port") is None):
        protocol = mapping.get("protocol", "tcp")
        if protocol not in ("tcp", "udp"): raise WorkbenchError("端口映射协议无效")
        requested = mapping.get("host_port")
        if requested is not None:
            if isinstance(requested, bool) or not isinstance(requested, int) or not low <= requested <= high:
                raise WorkbenchError(f"宿主端口必须位于配置范围 {low}–{high} 内")
            if (requested, protocol) in used or not available(requested, protocol):
                raise WorkbenchError(f"宿主端口 {requested}/{protocol} 已占用，请选择其他端口或自动分配")
        else:
            requested = next((port for port in range(low, high + 1) if (port, protocol) not in used and available(port, protocol)), None)
            if requested is None: raise WorkbenchError(f"配置范围 {low}–{high} 内没有可用的 {protocol} 宿主端口")
        mapping["host_port"] = requested
        mapping["protocol"] = protocol
        used.add((requested, protocol))
    return result


def publish_args(mappings):
    result = []
    for mapping in mappings:
        result += ["--publish", f"127.0.0.1:{mapping['host_port']}:{mapping['container_port']}/{mapping.get('protocol', 'tcp')}"]
    return result


def bindings_match(actual, mappings):
    expected = {}
    for mapping in mappings:
        key = f"{mapping['container_port']}/{mapping.get('protocol', 'tcp')}"
        expected.setdefault(key, []).append(("127.0.0.1", str(mapping["host_port"])))
    observed = {}
    if actual is not None and not isinstance(actual, dict): return False
    for key, bindings in (actual or {}).items():
        if not bindings: continue
        if not isinstance(bindings, list) or any(not isinstance(binding, dict) for binding in bindings): return False
        if any(not isinstance(binding.get("HostIp"), str) or not isinstance(binding.get("HostPort"), str) for binding in bindings): return False
        observed[key] = sorted((binding.get("HostIp"), binding.get("HostPort")) for binding in bindings)
    return observed == {key: sorted(bindings) for key, bindings in expected.items()}
