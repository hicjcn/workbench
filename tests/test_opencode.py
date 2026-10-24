import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from unittest.mock import Mock

from agent_workbench.opencode import OpenCodeClient
from agent_workbench.workspace import WorkbenchError


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def respond(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path == "/global/health":
            self.respond({"healthy": True, "version": "test"})
        elif self.path == "/doc":
            self.respond({"openapi": "3.1.0", "paths": {"/session": {"post": {}}, "/skill": {"get": {}}, "/session/{id}/abort": {"post": {}}}})
        elif self.path == "/config":
            self.respond({"plugin": ["/opt/superpowers"], "provider": {"private_fixture": "must-not-be-in-report"}})
        elif self.path == "/skill":
            self.respond([{"name": "brainstorming", "content": "must-not-be-in-report"}])
        else:
            self.send_error(404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.seen.append(("POST", self.path))
        self.respond({"id": "ses_fixture"} if self.path == "/session" else True)

    def do_DELETE(self):
        self.seen.append(("DELETE", self.path))
        self.respond(True)


class ClientTests(unittest.TestCase):
    def test_control_probe_does_not_leak_config_or_submit_a_prompt(self):
        Handler.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(f"http://127.0.0.1:{server.server_port}", "fixture-password")
            report = client.probe()
            self.assertTrue(report["superpowers_configured"])
            self.assertEqual(report["empty_session_create_abort"], "PASSED")
            self.assertEqual(report["skill_names"], ["brainstorming"])
            self.assertNotIn("must-not-be-in-report", json.dumps(report))
            self.assertNotIn("fixture-password", json.dumps(report))
            self.assertEqual(Handler.seen, [("POST", "/session"), ("POST", "/session/ses_fixture/abort"), ("DELETE", "/session/ses_fixture")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_remote_and_credential_in_url_endpoints_are_rejected(self):
        for url in ("https://example.com", "http://example.com:4096", "http://127.0.0.1",
                    "http://user:password@127.0.0.1:4096", "http://127.0.0.1:4096/other"):
            with self.assertRaises(WorkbenchError):
                OpenCodeClient(url, "fixture")

    def test_startup_connection_reset_is_a_retryable_control_error(self):
        client = OpenCodeClient("http://127.0.0.1:4096", "fixture-password")
        client.opener.open = Mock(side_effect=ConnectionResetError("startup window"))
        with self.assertRaisesRegex(WorkbenchError, "local endpoint is unavailable"):
            client.probe()


if __name__ == "__main__":
    unittest.main()

