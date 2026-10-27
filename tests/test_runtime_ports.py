import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_workbench import runtime_ports
from agent_workbench.workspace import WorkbenchError


class RuntimePortTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(port_range_start=23000, port_range_end=23002)

    def test_automatic_allocation_avoids_stopped_containers_host_ports_and_explicit_rows(self):
        def docker(*args):
            if args[0] == "ps": return SimpleNamespace(stdout="stopped\n")
            self.assertEqual(args, ("inspect", "stopped"))
            return SimpleNamespace(stdout=json.dumps([{"HostConfig": {"PortBindings": {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "23000"}]
            }}}]))
        with patch("agent_workbench.runtime_ports.runtime.docker", side_effect=docker), patch("agent_workbench.runtime_ports.available", return_value=True):
            ports = runtime_ports.allocate([
                {"container_port": 3000},
                {"container_port": 8080, "host_port": 23001},
            ], self.settings)
        self.assertEqual([row["host_port"] for row in ports], [23002, 23001])
        self.assertEqual(runtime_ports.publish_args(ports), ["--publish", "127.0.0.1:23002:3000/tcp", "--publish", "127.0.0.1:23001:8080/tcp"])

    def test_outside_range_duplicate_and_exhausted_requests_fail(self):
        with patch("agent_workbench.runtime_ports.occupied_ports", return_value=set()), patch("agent_workbench.runtime_ports.available", return_value=True):
            for requested in (22999, 23003, True):
                with self.assertRaisesRegex(WorkbenchError, "配置范围"):
                    runtime_ports.allocate([{"container_port": 3000, "host_port": requested}], self.settings)
            with self.assertRaisesRegex(WorkbenchError, "已占用"):
                runtime_ports.allocate([{"container_port": 3000, "host_port": 23001}, {"container_port": 8080, "host_port": 23001}], self.settings)
            with self.assertRaisesRegex(WorkbenchError, "没有可用"):
                runtime_ports.allocate([{"container_port": 3000}], self.settings, reserved=[(port, "tcp") for port in range(23000, 23003)])

    def test_host_socket_check_and_protocols(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            self.assertFalse(runtime_ports.available(port, "tcp"))
            self.assertTrue(runtime_ports.available(port, "udp"))

    def test_bindings_proof_rejects_extra_nonloopback_and_changed_publications(self):
        mapping = [{"container_port": 3000, "host_port": 23000, "protocol": "tcp"}]
        actual = {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23000"}]}
        self.assertTrue(runtime_ports.bindings_match(actual, mapping))
        self.assertFalse(runtime_ports.bindings_match(actual, []))
        self.assertFalse(runtime_ports.bindings_match({"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "23000"}]}, mapping))
        self.assertFalse(runtime_ports.bindings_match({"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23001"}]}, mapping))
        self.assertTrue(runtime_ports.bindings_match(None, []))

    def test_empty_mapping_does_not_contact_docker(self):
        with patch("agent_workbench.runtime_ports.runtime.docker") as docker:
            self.assertEqual(runtime_ports.allocate([], self.settings), [])
            docker.assert_not_called()
