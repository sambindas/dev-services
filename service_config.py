"""Validation and atomic service configuration updates."""
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

LOCK = threading.RLock()
EDITABLE_TYPES = {"process", "docker"}


def load(path):
    with LOCK:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)


def validate(raw, existing, original_id=None):
    if not isinstance(raw, dict):
        raise ValueError("Service must be an object.")
    allowed = {"id", "name", "group", "type", "port", "url", "notes", "cwd", "cmd", "container", "startTimeout", "startAll", "stopAll"}
    svc = {k: v for k, v in raw.items() if k in allowed}
    for key in ("id", "name", "group", "type", "url", "notes", "cwd", "cmd", "container"):
        value = svc.get(key, "")
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError(f"Invalid {key}.")
        svc[key] = value.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", svc["id"]):
        raise ValueError("ID must use 1–64 lowercase letters, numbers, hyphens or underscores.")
    if original_id and svc["id"] != original_id:
        raise ValueError("The service ID cannot change when editing.")
    if any(s["id"] == svc["id"] and s["id"] != original_id for s in existing):
        raise ValueError("That service ID already exists.")
    if not svc["name"] or not svc["group"]:
        raise ValueError("Name and group are required.")
    if svc["type"] not in EDITABLE_TYPES:
        raise ValueError("Choose a project command or Docker container.")
    if type(svc.get("port")) is not int or not 1 <= svc["port"] <= 65535:
        raise ValueError("Port must be a number between 1 and 65535.")
    if any(s.get("port") == svc["port"] and s["id"] != original_id and not s.get("planned") for s in existing):
        raise ValueError("Another service already uses that port.")
    if svc["url"]:
        url = urlparse(svc["url"])
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("URL must start with http:// or https:// and include a host.")
    if type(svc.get("startTimeout", 90)) is not int or not 1 <= svc.get("startTimeout", 90) <= 600:
        raise ValueError("Startup timeout must be between 1 and 600 seconds.")
    for key in ("startAll", "stopAll"):
        if key in svc and type(svc[key]) is not bool:
            raise ValueError(f"{key} must be true or false.")
    if svc["type"] == "process":
        folder = Path(svc["cwd"]).expanduser()
        if not svc["cwd"] or not folder.is_absolute() or not folder.is_dir():
            raise ValueError("Enter an existing absolute project folder path on this machine.")
        svc["cwd"] = str(folder.resolve())
        if not svc["cmd"]:
            raise ValueError("A start command is required.")
        svc.pop("container", None)
    else:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", svc["container"]):
            raise ValueError("Enter a valid existing Docker container name or ID.")
        svc.pop("cwd", None)
        svc.pop("cmd", None)
    return {k: v for k, v in svc.items() if v != ""}


def save(path, config):
    with LOCK:
        temp = Path(str(path) + ".tmp")
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp, path)
