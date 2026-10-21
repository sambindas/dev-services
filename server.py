#!/usr/bin/env python3
"""Local service dashboard. Run python server.py; see README.md for installation."""
import json
import os
import sys
import webbrowser
import queue
import argparse
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import platform_runtime as runtime
import service_config

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
PORT = int(os.environ.get("DASHBOARD_PORT", "7777"))
# Source checkouts retain their configuration. Distributed apps use user-writable data.
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR") or
                (runtime.user_data_dir() if getattr(sys, "frozen", False) else HERE))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "services.json"
if not CONFIG_FILE.exists():
    service_config.save(CONFIG_FILE, {"groups": ["Projects", "Docker"], "services": []})
LOG_DIR = str(DATA_DIR / "logs")
os.makedirs(LOG_DIR, exist_ok=True)
ENV = runtime.environment()
processes = runtime.ProcessManager(DATA_DIR)


def load_config():
    return service_config.load(CONFIG_FILE)


def run(cmd, timeout=20, cwd=None):
    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if runtime.SYSTEM == "Windows" else {}
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV, cwd=cwd, **options)
        return p.returncode, p.stdout + p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


# ---------- status probes ----------

def listening_ports():
    """Check configured loopback ports without requiring privileged OS-wide inspection."""
    ports = {}
    for svc in load_config()["services"]:
        port = svc.get("port")
        if port and runtime.port_open(port):
            proc = processes.process(svc["id"])
            ports[port] = [proc.pid] if proc else [0]
    return ports


def proc_info(pids):
    info = {}
    for pid in pids:
        if not pid:
            continue
        try:
            p = runtime.psutil.Process(pid)
            info[pid] = (p.ppid(), p.create_time(), " ".join(p.cmdline()))
        except runtime.psutil.Error:
            pass
    return info


def parse_docker_time(s):
    s = s.strip()
    if not s or s.startswith("0001"):
        return None
    base = s[:19]
    return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def docker_states(names):
    """container -> (status, started_epoch). Missing containers are omitted."""
    if not names:
        return {} if run(["docker", "info"], timeout=5)[0] == 0 else None
    code, out = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}|{{.Names}}"], timeout=10)
    if code != 0:
        return None
    available = [line.split("|", 1) for line in out.splitlines() if "|" in line]
    existing = [name for name in names if any(name == label or ident.startswith(name) for ident, label in available)]
    states = {}
    if existing:
        code, out = run(["docker", "inspect", "--type", "container", "-f",
                         "{{.Id}}|{{.Name}}|{{.State.Status}}|{{.State.StartedAt}}"] + existing, timeout=10)
        if code != 0:
            return None
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) == 4:
                ident, label, state, since = parts
                for requested in existing:
                    if requested == label.lstrip("/") or ident.startswith(requested):
                        states[requested] = (state, parse_docker_time(since))
    return states


def docker_desktop_since():
    _, out = run(["/usr/bin/pgrep", "-f", "com.docker.backend"])
    pids = [int(p) for p in out.split() if p.isdigit()]
    info = proc_info(pids)
    return min((v[1] for v in info.values()), default=None)


# ---------- actions ----------

busy = {}          # id -> {"action", "since", "error"}
busy_lock = threading.Lock()


def port_open(port):
    return runtime.port_open(port)


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(1)
    return False


def stop_process(svc):
    processes.stop(svc)
    if not wait_for(lambda: not port_open(svc["port"]), 5):
        raise RuntimeError("The managed process stopped, but the port is still occupied by another process.")


def start_process(svc):
    processes.start(svc, LOG_DIR, ENV)
    end = time.time() + svc.get("startTimeout", 90)
    while time.time() < end:
        if not processes.process(svc["id"]):
            raise RuntimeError("The start command exited. Check the log for details.")
        if port_open(svc["port"]):
            return
        time.sleep(0.25)
    # Keep the ownership record so users can stop a hung startup safely.
    raise RuntimeError(f"Port {svc['port']} did not open in time. Check the log or stop the service.")


def do_action(svc, action):
    t = svc["type"]
    if t in ("brew", "app") and runtime.SYSTEM != "Darwin":
        raise RuntimeError("This legacy service type is only supported on macOS.")
    if t == "brew":
        verb = {"start": "start", "restart": "restart", "stop": "stop"}[action]
        code, out = run(["brew", "services", verb, svc["brew"]], timeout=60)
        if code != 0:
            raise RuntimeError(out.strip()[-300:])
        if action != "stop" and not wait_for(lambda: port_open(svc["port"]), svc.get("startTimeout", 60)):
            raise RuntimeError(f"Port {svc['port']} did not open in time.")
    elif t == "process":
        if action in ("stop", "restart"):
            stop_process(svc)
        if action in ("start", "restart"):
            if port_open(svc["port"]):
                raise RuntimeError(f"Port {svc['port']} is already in use.")
            start_process(svc)
    elif t == "docker":
        code, out = run(["docker", action, svc["container"]], timeout=120)
        if code != 0:
            raise RuntimeError(out.strip()[-300:])
        if action != "stop" and not wait_for(lambda: port_open(svc["port"]), svc.get("startTimeout", 120)):
            raise RuntimeError(f"Container started but port {svc['port']} did not open in time.")
    elif t == "supabase":
        if action in ("stop", "restart"):
            code, out = run(["supabase", "stop"], timeout=180, cwd=svc["cwd"])  # plain stop keeps the data volumes
            if code != 0:
                raise RuntimeError(out.strip()[-300:])
        if action in ("start", "restart"):
            code, out = run(["supabase", "start"], timeout=svc.get("startTimeout", 300), cwd=svc["cwd"])
            if code != 0:
                raise RuntimeError(out.strip()[-300:])
    elif t == "app":
        if action == "stop":
            run(["/usr/bin/osascript", "-e", 'quit app "Docker"'])
        else:
            if action == "restart":
                run(["/usr/bin/osascript", "-e", 'quit app "Docker"'])
                wait_for(lambda: run(["docker", "info"], timeout=5)[0] != 0, 60)
            run(["/usr/bin/open", "-a", "Docker"])
            if not wait_for(lambda: run(["docker", "info"], timeout=5)[0] == 0, 180):
                raise RuntimeError("Docker engine did not come up in time.")


def action_thread(svc, action):
    try:
        do_action(svc, action)
        with busy_lock:
            busy.pop(svc["id"], None)
    except Exception as e:  # surfaced in the UI
        with busy_lock:
            busy[svc["id"]] = {"action": action, "since": time.time(), "error": str(e), "done": True}


# Start/stop everything, in dependency order (stop runs the stages in reverse).
# Services in the same stage run in parallel. Opt a service out with "startAll": false / "stopAll": false.
STAGES = [("app",), ("brew",), ("docker", "supabase", "process")]
bulk_state = {"op": None}


def bulk_thread(action):
    try:
        rows = {r["id"]: r for r in status()["services"]}
        flag = "startAll" if action == "start" else "stopAll"
        svcs = [s for s in load_config()["services"] if not s.get("planned") and s.get(flag, True)
                and not rows[s["id"]].get("unsupported") and not rows[s["id"]].get("external")]
        for types in (STAGES if action == "start" else list(reversed(STAGES))):
            threads = []
            for s in svcs:
                if s["type"] not in types or (rows[s["id"]]["running"] or rows[s["id"]].get("managed", False)) == (action == "start"):
                    continue
                with busy_lock:
                    if s["id"] in busy and not busy[s["id"]].get("done"):
                        continue
                    busy[s["id"]] = {"action": action, "since": time.time()}
                t = threading.Thread(target=action_thread, args=(s, action), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
    finally:
        bulk_state["op"] = None


# ---------- status ----------

def status():
    cfg = load_config()
    services = [s for s in cfg["services"] if not s.get("planned")]
    ports = listening_ports()
    all_pids = sorted({p for s in services if s.get("port") for p in ports.get(s["port"], [])})
    pinfo = proc_info(all_pids)
    containers = [s["container"] for s in services if s.get("container")]
    dstates = docker_states(containers) if containers or any(s["type"] == "app" for s in services) else {}
    docker_up = dstates is not None

    out = []
    for s in cfg["services"]:
        row = {k: s.get(k) for k in ("id", "name", "group", "type", "port", "url", "notes", "planned")}
        row["running"], row["since"], row["detail"] = False, None, ""
        row["editable"] = s["type"] in service_config.EDITABLE_TYPES
        row["unsupported"] = s["type"] in ("brew", "app") and runtime.SYSTEM != "Darwin"
        if row["unsupported"]:
            row["detail"] = "Available on macOS only"
        elif s.get("planned"):
            row["detail"] = "Not installed yet"
        elif s["type"] in ("brew", "process"):
            pids = ports.get(s["port"], [])
            row["running"] = bool(pids)
            starts = [pinfo[p][1] for p in pids if p in pinfo]
            row["since"] = min(starts) if starts else None
            row["detail"] = f"pid {', '.join(map(str, pids))}" if pids and pids != [0] else ("port in use" if pids else "not listening")
            if s["type"] == "process":
                managed = processes.process(s["id"])
                row["managed"] = managed is not None
                row["external"] = bool(pids) and managed is None
                row["running"] = bool(pids) and managed is not None
                if managed and not pids:
                    row["detail"] = "process active; waiting for port"
                elif row["external"]:
                    row["detail"] = "Port used outside dashboard; stop it in its original terminal first"

        elif s["type"] in ("docker", "supabase"):
            if not docker_up:
                row["detail"] = "Docker is not running"
            elif s["container"] not in dstates:
                row["detail"] = "container not found"
            else:
                st, since = dstates[s["container"]]
                row["managed"] = st == "running"
                row["running"] = st == "running" and (s["port"] in ports)
                row["since"] = since if st == "running" else None
                row["detail"] = "" if row["running"] else (st if st != "running" else "running, port not open yet")
        elif s["type"] == "app":
            row["running"] = docker_up
            row["since"] = docker_desktop_since() if docker_up else None
            row["detail"] = "engine up" if docker_up else "engine not reachable"
        with busy_lock:
            b = busy.get(s["id"])
        if b:
            row["busy"] = b
        out.append(row)
    return {"groups": cfg.get("groups", []), "services": out, "now": time.time(),
            "bulk": bulk_state["op"], "platform": runtime.SYSTEM}


# ---------- logs: live streaming + clear ----------

CLEARED_FILE = os.path.join(LOG_DIR, ".cleared.json")
cleared_lock = threading.Lock()
TAIL_LINES = 200


def load_cleared():
    try:
        with open(CLEARED_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cleared(data):
    with open(CLEARED_FILE, "w") as f:
        json.dump(data, f)


def log_path(svc):
    """Log file for brew/process services. Before the dashboard first starts a project, its log is ~/<id>.log."""
    if svc.get("log"):
        return svc["log"]
    own = os.path.join(LOG_DIR, f"{svc['id']}.log")
    legacy = os.path.join(HOME, f"{svc['id']}.log")
    return own if os.path.exists(own) or not os.path.exists(legacy) else legacy


def clear_logs(svc):
    """Dashboard-owned logs are truncated (the process appends, so that is safe).
    Other logs (brew, docker, hand-started) are left on disk; we just remember where 'clear' happened."""
    with cleared_lock:
        data = load_cleared()
        if svc["type"] == "docker":
            data[svc["id"]] = {"since": time.time()}
        else:
            path = log_path(svc)
            if path.startswith(LOG_DIR + os.sep) and os.path.exists(path):
                open(path, "w").close()
                data.pop(svc["id"], None)
            else:
                size = os.path.getsize(path) if os.path.exists(path) else 0
                data[svc["id"]] = {"file": path, "offset": size}
        save_cleared(data)


def cleared_offset(svc, path):
    c = load_cleared().get(svc["id"], {})
    return c.get("offset", 0) if c.get("file") == path else 0


def tail_start(path, floor):
    """Byte offset where the last TAIL_LINES lines begin (never before `floor`)."""
    size = os.path.getsize(path)
    start = max(floor, size - 256 * 1024)
    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read()
    idx = len(chunk)
    for _ in range(TAIL_LINES + 1):
        idx = chunk.rfind(b"\n", 0, idx)
        if idx < 0:
            return start
    return start + idx + 1


class Stream:
    """Server-sent events writer. send() returns False once the browser has gone away."""

    def __init__(self, handler):
        self.h = handler
        self.last = time.time()

    def send(self, obj):
        try:
            self.h.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.h.wfile.flush()
            self.last = time.time()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def heartbeat(self):
        if time.time() - self.last < 15:
            return True
        try:
            self.h.wfile.write(b": ping\n\n")
            self.h.wfile.flush()
            self.last = time.time()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


def stream_file(svc, out):
    path, pos, pending = None, 0, b""
    while True:
        cur = log_path(svc)
        if cur != path:  # first pass, or the service moved to the dashboard's own log after a restart
            if path is not None and not out.send({"note": f"Switched to {cur}"}):
                return
            path, pending = cur, b""
            if os.path.exists(path):
                pos = tail_start(path, cleared_offset(svc, path))
            else:
                pos = 0
                if not out.send({"note": "No log yet. It appears here once the service writes output."}):
                    return
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size < pos:  # truncated (cleared) or rotated
                pos, pending = 0, b""
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(min(size - pos, 1024 * 1024))
                pos += len(data)
                data = pending + data
                *lines, pending = data.split(b"\n")
                if lines and not out.send({"lines": [l.decode("utf-8", "replace") for l in lines]}):
                    return
        if not out.heartbeat():
            return
        time.sleep(0.5)


def stream_docker(svc, out):
    # Windows select() cannot read subprocess pipes. A bounded reader queue works on all OSes.
    cmd = ["docker", "logs", "-f", "--tail", str(TAIL_LINES)]
    since = load_cleared().get(svc["id"], {}).get("since")
    if since:
        cmd += ["--since", str(int(since))]
    try:
        p = subprocess.Popen(cmd + [svc["container"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV,
                             **({"creationflags": subprocess.CREATE_NO_WINDOW} if runtime.SYSTEM == "Windows" else {}))
    except OSError as error:
        out.send({"note": str(error), "done": True})
        return
    chunks = queue.Queue(maxsize=32)
    closed = threading.Event()

    def reader():
        try:
            while not closed.is_set():
                chunk = p.stdout.read1(65536)
                while not closed.is_set():
                    try:
                        chunks.put(chunk, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    break
        except OSError:
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    pending = b""
    try:
        while True:
            try:
                chunk = chunks.get(timeout=1)
            except queue.Empty:
                if not out.heartbeat():
                    return
                continue
            if not chunk:
                if pending:
                    out.send({"lines": [pending.decode("utf-8", "replace")]})
                out.send({"note": "Container log stream ended.", "done": True})
                return
            *lines, pending = (pending + chunk).split(b"\n")
            if lines and not out.send({"lines": [line.decode("utf-8", "replace") for line in lines]}):
                return
    finally:
        closed.set()
        if p.poll() is None:
            p.kill()
        p.wait()
        thread.join(timeout=2)
        p.stdout.close()


def stream_logs(svc, handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    out = Stream(handler)
    if svc["type"] == "supabase":
        _, text = run(["supabase", "status"], timeout=30, cwd=svc["cwd"])
        safe = [l for l in text.splitlines() if not any(w in l.lower() for w in ("key", "secret", "jwt", "token"))]
        out.send({"lines": safe, "note": "Supabase has no single log; this is its status.", "done": True})
    elif svc["type"] == "docker":
        stream_docker(svc, out)
    else:
        stream_file(svc, out)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        valid_origin = not origin or origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")
        return host in ("127.0.0.1", "localhost") and valid_origin

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/health":
            return self._send(200, {"app": "dev-services", "version": "0.2.1"})
        if u.path == "/api/setup":
            return self._send(200, {"platform": runtime.SYSTEM, "tools": runtime.tools(ENV),
                                    "dataDir": str(DATA_DIR), "shell": os.environ.get("COMSPEC", "cmd.exe") if runtime.SYSTEM == "Windows" else runtime.shell_command("")[0]})
        if u.path == "/api/services":
            return self._send(200, load_config())
        if u.path == "/api/status":
            return self._send(200, status())
        if u.path == "/api/logs/stream":
            sid = parse_qs(u.query).get("id", [""])[0]
            svc = next((s for s in load_config()["services"] if s["id"] == sid), None)
            if not svc or svc.get("planned") or svc["type"] == "app":
                return self._send(404, {"error": "unknown service"})
            return stream_logs(svc, self)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        with service_config.LOCK:
            return self._post()

    def _post(self):
        # Custom header forces a CORS preflight, which we never answer, so other websites can't trigger actions.
        if not self._host_ok() or self.headers.get("X-Dashboard") != "1":
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/api/quit":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._send(200, {"ok": True})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 65536:
                raise ValueError("Request is too large.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object.")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "Invalid JSON request (maximum 64 KB)."})
        if path in ("/api/services/save", "/api/services/delete"):
            return self._change_service(path, body)
        if path in ("/api/start-all", "/api/stop-all"):
            action = "start" if path == "/api/start-all" else "stop"
            with busy_lock:
                if bulk_state["op"]:
                    return self._send(409, {"error": "already busy"})
                bulk_state["op"] = action
            threading.Thread(target=bulk_thread, args=(action,), daemon=True).start()
            return self._send(202, {"ok": True})
        if path not in ("/api/action", "/api/logs/clear"):
            return self._send(404, {"error": "not found"})
        svc = next((s for s in load_config()["services"] if s["id"] == body.get("id")), None)
        if path == "/api/logs/clear":
            if not svc or svc.get("planned") or svc["type"] in ("app", "supabase"):
                return self._send(400, {"error": "bad request"})
            clear_logs(svc)
            return self._send(200, {"ok": True})
        action = body.get("action")
        if not svc or svc.get("planned") or (svc["type"] in ("brew", "app") and runtime.SYSTEM != "Darwin") or action not in ("start", "restart", "stop"):
            return self._send(400, {"error": "bad request"})
        with busy_lock:
            if svc["id"] in busy and not busy[svc["id"]].get("done"):
                return self._send(409, {"error": "already busy"})
            busy[svc["id"]] = {"action": action, "since": time.time()}
        threading.Thread(target=action_thread, args=(svc, action), daemon=True).start()
        self._send(202, {"ok": True})

    def _change_service(self, path, body):
        # Hold both locks while checking active operations and committing configuration.
        with service_config.LOCK, busy_lock:
            cfg = load_config()
            original_id = body.get("originalId") if path.endswith("save") else body.get("id")
            if original_id is not None and not isinstance(original_id, str):
                return self._send(400, {"error": "Invalid service ID."})
            old = next((s for s in cfg["services"] if s["id"] == original_id), None)
            if original_id and not old:
                return self._send(404, {"error": "Service not found."})
            if bulk_state["op"] or (old and busy.get(original_id) and not busy[original_id].get("done")):
                return self._send(409, {"error": "Wait for the current operation to finish."})
            if old and (processes.process(original_id) or (old.get("port") and port_open(old["port"]))):
                return self._send(409, {"error": "Stop this service before editing or removing it."})
            if old and old["type"] not in service_config.EDITABLE_TYPES:
                return self._send(400, {"error": "Edit legacy service types in services.json."})
            try:
                if path.endswith("delete"):
                    if not old:
                        raise ValueError("Select an existing service.")
                    # A container may still be running without its published port.
                    if old["type"] == "docker":
                        states = docker_states([old["container"]])
                        if states is None:
                            raise ValueError("Connect Docker before removing a container service.")
                        if states.get(old["container"], (None,))[0] == "running":
                            raise ValueError("Stop the Docker container before removing it.")
                    cfg["services"] = [s for s in cfg["services"] if s["id"] != original_id]
                else:
                    svc = service_config.validate(body.get("service"), cfg["services"], original_id)
                    if svc["port"] == PORT:
                        raise ValueError("That port is used by the dashboard.")
                    if svc["type"] == "process" and port_open(svc["port"]):
                        raise ValueError("That port is already in use. Stop its process or choose another port.")
                    if svc["type"] == "docker":
                        states = docker_states([svc["container"]])
                        if states is None:
                            raise ValueError("Docker is unavailable. Install Docker and start its engine first.")
                        if svc["container"] not in states:
                            raise ValueError("Container not found. Create it first or check its name with docker ps -a.")
                    if old and old["type"] == "docker":
                        states = docker_states([old["container"]])
                        if states is None or states.get(old["container"], (None,))[0] == "running":
                            raise ValueError("Connect Docker and stop the container before editing it.")
                    if old:
                        svc = {**old, **svc}
                        if svc["type"] == "process":
                            svc.pop("container", None)
                        else:
                            svc.pop("cwd", None)
                            svc.pop("cmd", None)
                        cfg["services"] = [svc if s["id"] == original_id else s for s in cfg["services"]]
                    else:
                        cfg["services"].append(svc)
                    if svc["group"] not in cfg.setdefault("groups", []):
                        cfg["groups"].append(svc["group"])
                service_config.save(CONFIG_FILE, cfg)
                busy.pop(original_id, None)
            except (ValueError, OSError) as error:
                return self._send(400, {"error": str(error)})
            return self._send(200, {"ok": True})


def main():
    parser = argparse.ArgumentParser(description="Local service dashboard")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # Opening twice should reopen the existing app, never start a second controller.
        from urllib.request import urlopen
        try:
            with urlopen(url + "/api/health", timeout=2) as response:
                existing = json.load(response)
            if existing.get("app") == "dev-services":
                if not args.no_browser:
                    webbrowser.open(url)
                return
        except Exception:
            pass
        raise SystemExit(f"Port {PORT} is occupied. Set DASHBOARD_PORT to another port.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if sys.stdout:
        print(f"Dev Services: {url}  |  Settings: {CONFIG_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
