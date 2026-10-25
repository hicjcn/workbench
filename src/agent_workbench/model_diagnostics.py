"""Bounded public-endpoint diagnostics through the inherited proxy; never inference."""
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .runtime import model_config


def probe_url(url, fetch=urlopen):
    context = ssl.create_default_context()
    if os.environ.get("CODEX_PROXY_CERT"): context.load_verify_locations(os.environ["CODEX_PROXY_CERT"])
    result = {"url": url, "status": None, "classification": "CONNECTION_FAILED"}
    try:
        with fetch(Request(url, headers={"User-Agent": "agent-workbench-diagnostics"}), context=context, timeout=12) as response:
            result.update(status=response.status, classification="REACHABLE")
    except HTTPError as error:
        result.update(status=error.code, classification="HTTP_DENIED" if error.code in (403, 407) else "REACHABLE_HTTP_ERROR")
    except URLError as error:
        reason = error.reason
        # CONNECT denials are wrapped as URLError(OSError), not HTTPError.
        match = re.search(r"Tunnel connection failed:\s*(\d{3})\b", str(reason), re.I)
        if match: result.update(status=int(match[1]), classification="PROXY_DENIED" if match[1] in ("403", "407") else "PROXY_ERROR")
        elif isinstance(reason, ssl.SSLError): result["classification"] = "TLS_ERROR"
    except ssl.SSLError: result["classification"] = "TLS_ERROR"
    except OSError: pass
    return result


def diagnose(models, probe=probe_url):
    for model in models: model_config(model)
    urls = ["https://models.dev/api.json"]
    if any(model.startswith("opencode/") for model in models): urls.append("https://opencode.ai/zen/v1/models")
    if any(model.startswith("openrouter/") for model in models): urls.append("https://openrouter.ai/api/v1/models")
    checks = [probe(url) for url in urls]
    missing = any(model.startswith("openrouter/") for model in models) and not os.environ.get("OPENROUTER_API_KEY", "").strip()
    reachable = all(item["classification"] in ("REACHABLE", "REACHABLE_HTTP_ERROR") for item in checks)
    return {"schema": "workbench-model-readiness/v1", "checked_at": time.time(), "models": list(models), "checks": checks,
        "openrouter_credential_present": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "status": "MISSING_CREDENTIAL" if missing else "NETWORK_REACHABLE_NOT_INFERRED" if reachable else "BLOCKED_NETWORK",
        "can_attempt": reachable and not missing, "model_inference": False,
        "note": "公共端点可达不等于模型调用成功；未提交提示词，不保存响应正文、认证或原始错误。"}
