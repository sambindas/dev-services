"""Exercise a bundled/installed app against private settings and disposable processes."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from platform_runtime import ProcessManager


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", action="store_true")
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    executable = args.executable or ROOT / "dist/DevServices" / ("DevServices.exe" if os.name == "nt" else "DevServices")
    if args.app:
        executable = ROOT / "dist/DevServices.app/Contents/MacOS/DevServices"
    with tempfile.TemporaryDirectory(prefix="Dev Services smoke ") as data:
        port = free_port()
        service_port = free_port()
        while service_port == port:
            service_port = free_port()
        env = {**os.environ, "DASHBOARD_DATA_DIR": data, "DASHBOARD_PORT": str(port)}
        process = subprocess.Popen([str(executable), "--no-browser"], env=env)
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urlopen(base + path, timeout=5) as response:
                return json.load(response)

        def post(path, body):
            req = Request(base + path, data=json.dumps(body).encode(), headers={"X-Dashboard": "1", "Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                return json.load(response)

        def wait_service(condition):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                row = get("/api/status")["services"][0]
                if row.get("busy", {}).get("error"):
                    raise RuntimeError(row["busy"]["error"])
                if condition(row):
                    return
                time.sleep(0.2)
            raise RuntimeError(f"Service did not reach expected state: {row}")

        try:
            for _ in range(600):
                if process.poll() is not None:
                    raise RuntimeError(f"Bundled app exited: {process.returncode}")
                try:
                    assert get("/api/status")["services"] == []
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Bundled app did not start")
            with urlopen(base, timeout=5) as response:
                assert b"Add service" in response.read()
            assert Path(get("/api/setup")["dataDir"]).resolve() == Path(data).resolve()
            service = {"id": "smoke", "name": "Smoke service", "group": "Projects", "type": "process", "cwd": data,
                       "cmd": f'"{sys.executable}" -u -m http.server {service_port} --bind 127.0.0.1',
                       "port": service_port, "startTimeout": 90}
            assert post("/api/services/save", {"service": service})["ok"]
            post("/api/action", {"id": "smoke", "action": "start"})
            wait_service(lambda row: row["running"] and not row.get("busy"))
            with urlopen(f"http://127.0.0.1:{service_port}", timeout=5) as response:
                assert response.status == 200
            post("/api/action", {"id": "smoke", "action": "restart"})
            wait_service(lambda row: row["running"] and not row.get("busy"))
            post("/api/action", {"id": "smoke", "action": "stop"})
            wait_service(lambda row: not row["running"] and not row.get("managed") and not row.get("busy"))
            assert "Serving HTTP" in (Path(data) / "logs/smoke.log").read_text(encoding="utf-8")
            post("/api/services/delete", {"id": "smoke"})
            assert get("/api/status")["services"] == []
            assert post("/api/quit", {})["ok"]
            process.wait(timeout=10)
            assert process.returncode == 0
            print("Bundled app passed: startup, settings, UI, add/start/restart/stop/logs/remove service, quit")
        finally:
            ProcessManager(data).stop({"id": "smoke", "port": service_port})
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
