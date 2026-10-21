"""OS adapters and identity-checked process ownership for the local dashboard."""
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

SYSTEM = platform.system()


def user_data_dir():
    if SYSTEM == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "DevServices"
    if SYSTEM == "Darwin":
        return Path.home() / "Library/Application Support/DevServices"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "dev-services"


def environment():
    env = dict(os.environ)
    extras = [Path.home() / "bin", Path.home() / ".local/bin", Path.home() / ".dotnet",
              Path.home() / ".dotnet/tools", Path.home() / ".docker/bin"]
    if SYSTEM != "Windows":
        extras += [Path("/opt/homebrew/bin"), Path("/opt/homebrew/sbin"), Path("/usr/local/bin")]
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        if getattr(sys, "frozen", False):
            original = env.pop(key + "_ORIG", None)
            if original is not None:
                env[key] = original
            else:
                env.pop(key, None)
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + [str(p) for p in extras])
    return env


def shell_command(command):
    if SYSTEM == "Windows":
        # cmd.exe uses its own quoting rules, not the C argv rules applied to a Popen list.
        return '"{}" /d /s /c "{}"'.format(os.environ.get("COMSPEC", "cmd.exe"), command)
    shell = os.environ.get("SHELL") or "/bin/sh"
    # Preserve existing macOS nvm-based commands without requiring it on other machines.
    prelude = ""
    if SYSTEM == "Darwin" and Path("/opt/homebrew/opt/nvm/nvm.sh").exists():
        prelude = '. /opt/homebrew/opt/nvm/nvm.sh >/dev/null 2>&1; '
    return [shell, "-c", prelude + command]


def port_open(port):
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.15):
                return True
        except OSError:
            pass
    return False


def tools(env):
    return {name: bool(shutil.which(name, path=env["PATH"]))
            for name in ("docker", "node", "npm", "python3", "python", "php", "dotnet", "brew", "supabase")}


class ProcessManager:
    """Remember PID + creation time; never stop an unrelated port occupant."""
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "processes.json"
        self.lock = threading.RLock()
        try:
            self.records = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.records = {}
        self.handles = {}

    def _save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.records), encoding="utf-8")
        os.replace(temp, self.path)

    def process(self, sid):
        with self.lock:
            handle = self.handles.get(sid)
            if handle is not None:
                handle.poll()  # Reap commands that have exited before probing their PID.
            record = self.records.get(sid)
            if not record:
                return None
            try:
                proc = psutil.Process(record["pid"])
                if abs(proc.create_time() - record["created"]) < 0.01 and proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    return proc
            except psutil.Error:
                pass
            return None

    def start(self, svc, log_dir, env):
        with self.lock:
            if self.process(svc["id"]):
                raise RuntimeError("This service already has a managed process. Stop it before starting again.")
            with open(Path(log_dir) / (svc["id"] + ".log"), "ab") as log:
                log.write(f"\n===== started {time.ctime()} =====\n".encode())
                log.flush()
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if SYSTEM == "Windows" else {"start_new_session": True}
                proc = subprocess.Popen(shell_command(svc["cmd"]), cwd=svc["cwd"], env=env,
                                        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options)
            try:
                created = psutil.Process(proc.pid).create_time()
            except psutil.NoSuchProcess:
                proc.wait()
                raise RuntimeError("The start command exited immediately. Check the log.")
            self.handles[svc["id"]] = proc
            self.records[svc["id"]] = {"pid": proc.pid, "created": created}
            self._save()

    def stop(self, svc):
        with self.lock:
            parent = self.process(svc["id"])
            if parent is None:
                if port_open(svc["port"]):
                    raise RuntimeError("This port belongs to a process started outside the dashboard. Stop it in its original terminal first.")
                self.handles.pop(svc["id"], None)
                self.records.pop(svc["id"], None)
                self._save()
                return
            try:
                children = parent.children(recursive=True)
                processes = children + [parent]
                for proc in processes:
                    try:
                        proc.terminate()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(processes, timeout=8)
                # Reparented children can remain zombies until the OS reaps them.
                # A zombie has exited and must not make stop/restart report failure.
                def still_active(proc):
                    try:
                        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        return False
                alive = [proc for proc in alive if still_active(proc)]
                for proc in alive:
                    try:
                        proc.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(alive, timeout=3)
                if any(still_active(proc) for proc in alive):
                    raise RuntimeError("Some service processes could not be stopped.")
            except psutil.NoSuchProcess:
                pass
            handle = self.handles.pop(svc["id"], None)
            if handle:
                handle.wait(timeout=3)
            self.records.pop(svc["id"], None)
            self._save()
