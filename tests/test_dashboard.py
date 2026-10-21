import json
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import platform_runtime as runtime
import service_config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConfigurationTests(unittest.TestCase):
    def test_validation_and_atomic_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            svc = {"id": "api", "name": "API", "group": "Projects", "type": "process", "cwd": folder,
                   "cmd": "python server.py", "port": 32100, "url": "http://localhost:32100"}
            valid = service_config.validate(svc, [])
            path = Path(folder) / "services.json"
            service_config.save(path, {"services": [valid]})
            self.assertEqual(service_config.load(path)["services"][0], valid)
            for change in ({"id": "../escape"}, {"id": "x\" onclick=\"alert(1)"}, {"port": True},
                           {"port": 0}, {"url": "javascript:alert(1)"}, {"cwd": str(Path(folder) / "missing")},
                           {"startTimeout": 601}, {"cmd": ""}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    service_config.validate({**svc, **change}, [])
            with self.assertRaises(ValueError):
                service_config.validate(svc, [valid])
            with self.assertRaises(ValueError):
                service_config.validate({**svc, "id": "another"}, [valid])

    def test_shell_adapters(self):
        with patch.object(runtime, "SYSTEM", "Windows"), patch.dict(os.environ, {"COMSPEC": "cmd.exe"}):
            self.assertEqual(runtime.shell_command("npm run dev"), '"cmd.exe" /d /s /c "npm run dev"')
        with patch.object(runtime, "SYSTEM", "Linux"), patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            self.assertEqual(runtime.shell_command("echo hello"), ["/bin/bash", "-c", "echo hello"])

    def test_zombie_children_do_not_make_stop_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = runtime.ProcessManager(folder)
            parent, zombie = MagicMock(), MagicMock()
            parent.children.return_value = [zombie]
            zombie.is_running.return_value = True
            zombie.status.return_value = runtime.psutil.STATUS_ZOMBIE
            with patch.object(manager, "process", return_value=parent), patch.object(runtime.psutil, "wait_procs", side_effect=[([], [zombie]), ([], [])]):
                manager.stop({"id": "test", "port": 32100})
            zombie.kill.assert_not_called()
            self.assertEqual(manager.records, {})

    def test_reused_pid_is_never_owned(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = runtime.ProcessManager(folder)
            manager.records["fake"] = {"pid": os.getpid(), "created": 1}
            self.assertIsNone(manager.process("fake"))


class DashboardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev services ")
        self.port = free_port()
        self.service_port = free_port()
        while self.service_port == self.port:
            self.service_port = free_port()
        self.env = {**os.environ, "DASHBOARD_DATA_DIR": self.temp.name, "DASHBOARD_PORT": str(self.port)}
        self.server = None
        self.start_server()

    def start_server(self):
        self.server = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--no-browser"], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(1200):
            if self.server.poll() is not None:
                self.fail(self.server.stderr.read().decode())
            try:
                self.request("/api/health")
                return
            except OSError:
                time.sleep(0.1)
        self.fail("Server did not start")

    def request(self, path, body=None, headers=None, raw=None):
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=payload,
                      headers=headers if headers is not None else {"Content-Type": "application/json", "X-Dashboard": "1"})
        with urlopen(req, timeout=10) as response:
            data = response.read()
            return json.loads(data) if response.headers.get_content_type() == "application/json" else data.decode()

    def wait_row(self, condition):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            rows = self.request("/api/status")["services"]
            if rows and condition(rows[0]):
                return rows[0]
            time.sleep(0.1)
        self.fail(f"Service did not reach expected state: {rows}")

    def assert_status(self, code, path, body=None, **kwargs):
        with self.assertRaises(HTTPError) as error:
            self.request(path, body, **kwargs)
        self.assertEqual(error.exception.code, code)
        error.exception.close()

    def add_service(self):
        # Quote the interpreter path for either shell, including project paths containing spaces.
        executable = f'"{sys.executable}"'
        service = {"id": "test-api", "name": "Test API", "group": "Projects", "type": "process", "cwd": self.temp.name,
                   "cmd": f"{executable} -u -m http.server {self.service_port} --bind 127.0.0.1", "port": self.service_port,
                   "startTimeout": 90}
        self.request("/api/services/save", {"service": service})
        return service

    def tearDown(self):
        # Clean only the disposable process recorded in this test's private data directory.
        try:
            manager = runtime.ProcessManager(self.temp.name)
            manager.stop({"id": "test-api", "port": self.service_port})
        finally:
            self.stop_server()
            self.temp.cleanup()

    def stop_server(self):
        if self.server:
            if self.server.poll() is None:
                self.server.terminate()
                self.server.wait(timeout=10)
            self.server.stderr.close()
            self.server = None

    def test_crud_process_lifecycle_and_recovery(self):
        self.assertEqual(self.request("/api/status")["services"], [])
        self.assertIn("Add service", self.request("/"))
        self.assertIn("platform", self.request("/api/setup"))
        service = self.add_service()
        self.request("/api/action", {"id": service["id"], "action": "start"})
        self.wait_row(lambda row: row["running"] and not row.get("busy"))
        self.assert_status(409, "/api/services/delete", {"id": service["id"]})
        self.assert_status(409, "/api/services/save", {"originalId": service["id"], "service": service})
        # Backend restart must not lose ownership or stop the managed service.
        self.stop_server()
        self.start_server()
        self.wait_row(lambda row: row["running"] and row["managed"])
        self.request("/api/action", {"id": service["id"], "action": "restart"})
        self.wait_row(lambda row: row["running"] and not row.get("busy"))
        self.request("/api/action", {"id": service["id"], "action": "stop"})
        self.wait_row(lambda row: not row["running"] and not row.get("managed") and not row.get("busy"))
        self.assertTrue((Path(self.temp.name) / "logs/test-api.log").exists())
        self.request("/api/logs/clear", {"id": service["id"]})
        self.request("/api/services/save", {"originalId": service["id"], "service": {**service, "name": "Updated API"}})
        self.assertEqual(self.request("/api/services")["services"][0]["name"], "Updated API")
        self.request("/api/services/delete", {"id": service["id"]})
        self.assertEqual(self.request("/api/status")["services"], [])

    def test_failed_start_can_be_stopped_with_stop_all(self):
        service = self.add_service()
        service["cmd"] = f'"{sys.executable}" -c "import time; time.sleep(60)"'
        service["startTimeout"] = 1
        self.request("/api/services/save", {"originalId": service["id"], "service": service})
        self.request("/api/action", {"id": service["id"], "action": "start"})
        row = self.wait_row(lambda row: row.get("busy", {}).get("done"))
        self.assertTrue(row["managed"])
        self.assertIn("did not open", row["busy"]["error"])
        self.request("/api/stop-all", {})
        self.wait_row(lambda row: not row.get("managed") and not row.get("busy"))

    def test_request_protection_and_validation(self):
        self.assert_status(403, "/api/stop-all", {}, headers={"Content-Type": "application/json"})
        self.assert_status(403, "/api/stop-all", {}, headers={"X-Dashboard": "1", "Origin": "https://example.com"})
        self.assert_status(403, "/api/status", headers={"Host": "evil.example"})
        self.assert_status(400, "/api/services/save", raw=b"{broken")
        self.assert_status(400, "/api/services/save", raw=b"[]")
        self.assert_status(400, "/api/services/save", {"service": {"id": "../bad"}})
        self.assert_status(400, "/api/services/save", {"service": None})
        service = self.add_service()
        self.assert_status(400, "/api/services/save", {"service": service})

    def test_external_port_is_never_terminated(self):
        service = self.add_service()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.service_port))
            listener.listen(16)
            self.wait_row(lambda row: row.get("external"))
            self.request("/api/action", {"id": service["id"], "action": "stop"})
            row = self.wait_row(lambda row: row.get("busy", {}).get("done"))
            self.assertIn("outside", row["busy"]["error"])
            self.assertIsNotNone(listener.getsockname())


class DockerAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"DASHBOARD_DATA_DIR": cls.temp.name}):
            spec = importlib.util.spec_from_file_location("dashboard_under_test", ROOT / "server.py")
            cls.dashboard = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.dashboard)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_quit_flushes_response_before_shutdown(self):
        handler = self.dashboard.Handler.__new__(self.dashboard.Handler)
        handler.headers = {"Host": "127.0.0.1", "X-Dashboard": "1"}
        handler.path = "/api/quit"
        handler.server = MagicMock()
        handler.wfile = MagicMock()
        events = []
        handler._send = lambda code, body: events.append("response")
        handler.wfile.flush.side_effect = lambda: events.append("flush")
        with patch.object(self.dashboard.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: events.append("shutdown")
            handler._post()
        self.assertEqual(events, ["response", "flush", "shutdown"])

    def test_container_name_and_id_are_resolved(self):
        ident = "abcdef1234567890"
        results = [(0, f"{ident}|my-db\n"), (0, f"{ident}|/my-db|running|2026-10-20T10:00:00Z\n")]
        with patch.object(self.dashboard, "run", side_effect=results):
            states = self.dashboard.docker_states(["my-db", "abcdef123456", "missing"])
        self.assertEqual(set(states), {"my-db", "abcdef123456"})
        self.assertEqual(states["my-db"][0], "running")

    def test_can_register_an_existing_running_container(self):
        handler = self.dashboard.Handler.__new__(self.dashboard.Handler)
        handler._send = lambda code, body: (code, body)
        svc = {"id": "db", "name": "DB", "group": "Docker", "type": "docker", "container": "db", "port": 5432}
        with patch.object(self.dashboard, "load_config", return_value={"services": [], "groups": []}), patch.object(self.dashboard, "port_open", return_value=True), patch.object(self.dashboard, "docker_states", return_value={"db": ("running", 123)}), patch.object(service_config, "save") as save:
            code, body = handler._change_service("/api/services/save", {"service": svc})
        self.assertEqual(code, 200, body)
        self.assertEqual(save.call_args[0][1]["services"][0]["container"], "db")

    def test_missing_container_is_rejected(self):
        handler = self.dashboard.Handler.__new__(self.dashboard.Handler)
        handler._send = lambda code, body: (code, body)
        svc = {"id": "db", "name": "DB", "group": "Docker", "type": "docker", "container": "missing", "port": 5432}
        with patch.object(self.dashboard, "load_config", return_value={"services": [], "groups": []}), patch.object(self.dashboard, "docker_states", return_value={}):
            code, body = handler._change_service("/api/services/save", {"service": svc})
        self.assertEqual(code, 400)
        self.assertIn("not found", body["error"])

    def test_unreachable_engine(self):
        with patch.object(self.dashboard, "run", return_value=(1, "unreachable")):
            self.assertIsNone(self.dashboard.docker_states(["db"]))

    def test_running_container_without_port_still_has_stop_control(self):
        config = {"groups": [], "services": [{"id": "db", "name": "DB", "group": "Docker", "type": "docker", "container": "db", "port": 5432}]}
        with patch.object(self.dashboard, "load_config", return_value=config), patch.object(self.dashboard, "listening_ports", return_value={}), patch.object(self.dashboard, "docker_states", return_value={"db": ("running", 123)}):
            row = self.dashboard.status()["services"][0]
        self.assertFalse(row["running"])
        self.assertTrue(row["managed"])


if __name__ == "__main__":
    unittest.main()
