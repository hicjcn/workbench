"""Local OpenCode control API. No model provider or API key is hardcoded."""

from __future__ import annotations

import base64
from http.client import HTTPException
import json
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .workspace import WorkbenchError


class OpenCodeClient:
    def __init__(self, base_url: str, password: str):
        parsed = urlparse(base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise WorkbenchError("M1 accepts only the local runtime's loopback HTTP endpoint")
        self.base_url = base_url.rstrip("/")
        encoded = base64.b64encode(f"workbench:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}
        # Loopback control traffic is local; external requests retain their configured proxy.
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path: str, payload: dict | None = None, *, method: str = "GET"):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base_url + path, data=data, headers=self.headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise WorkbenchError("OpenCode response exceeds M1 limit")
                return json.loads(body) if body else None
        except HTTPError as error:
            raise WorkbenchError(f"OpenCode returned HTTP {error.code}") from None
        except (OSError, HTTPException) as error:
            raise WorkbenchError("OpenCode local endpoint is unavailable") from error

    def probe(self) -> dict:
        health = self.request("/global/health")
        spec = self.request("/doc")
        paths = spec.get("paths", {})
        configured = self.request("/config")
        plugins = configured.get("plugin", configured.get("plugins", []))
        report = {
            "health": {key: health.get(key) for key in ("healthy", "version")},
            "openapi_version": spec.get("openapi"),
            "capabilities": {
                "create_session": "post" in paths.get("/session", {}),
                "async_prompt": any("prompt_async" in path for path in paths),
                "abort": any(path.endswith("/abort") for path in paths),
                "events": "/event" in paths or "/global/event" in paths,
            },
            "superpowers_configured": any("superpowers" in str(plugin) for plugin in plugins),
        }
        if "/skill" in paths:
            skills = self.request("/skill")
            if isinstance(skills, list):
                report["skill_names"] = sorted(item["name"] for item in skills if isinstance(item, dict) and "name" in item)
        if report["capabilities"]["create_session"]:
            session = self.request("/session", {"title": "M1 control API verification"}, method="POST")
            session_id = session.get("id")
            if not isinstance(session_id, str) or not session_id.replace("_", "").isalnum():
                raise WorkbenchError("OpenCode returned an invalid session identifier")
            try:
                self.request(f"/session/{session_id}/abort", method="POST")
                report["empty_session_create_abort"] = "PASSED"
            finally:
                self.request(f"/session/{session_id}", method="DELETE")
        return report

