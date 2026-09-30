"""Device registry: the devices this machine drives, in devices.json.

devices.json sits next to `bench` and is git-ignored (it holds addresses):

    {"default": "rp6",
     "devices": [{"id": "rp6", "name": "Retroid Pocket 6", "user": "steamos",
                  "host": "192.0.2.10", "port": 22, "ssh_opts": "", "remote_dir": "bench"},
                 {"id": "rp6-android", "name": "Retroid Pocket 6 (Android)", "kind": "android",
                  "serial": "0123abcd"}]}

An Android device (kind "android") is reached over adb by its hardware serial
(`adb shell getprop ro.serialno`); its sessions run on this machine, see android.py.

`bench --device ID ...` (or the web app) picks one; without --device the
default is used. A remote.conf from before the registry is imported once as
device "default" (its ssh_opts are kept).

Addresses and users are validated. ssh_opts are free-form ssh options and
can run commands on this machine (ProxyCommand etc.), so they can only be
set by editing devices.json or with `bench devices add --ssh-opts`, never
through the web app.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "devices.json"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*\.?$")


class DeviceError(ValueError):
    pass


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return bool(HOST_RE.match(host))


def check(dev: dict, *, allow_ssh_opts: bool) -> dict:
    """Normalized copy of dev, or DeviceError."""
    if dev.get("kind") == "android":
        from .adb import SERIAL_RE
        d = {k: dev.get(k) for k in ("id", "name", "serial", "notes")}
        d["kind"] = "android"
        d["id"] = (d["id"] or "").strip().lower()
        if not ID_RE.match(d["id"]):
            raise DeviceError("id: lowercase letters, digits and -, up to 32, e.g. rp6-android")
        d["name"] = (d["name"] or d["id"]).strip()[:60]
        d["serial"] = (d["serial"] or "").strip()
        if not SERIAL_RE.match(d["serial"]):
            raise DeviceError("adb serial: the device's serial (adb shell getprop ro.serialno) or an adb IP:PORT")
        d["notes"] = (d["notes"] or "").strip()[:200]
        return d
    d = {k: dev.get(k) for k in ("id", "name", "user", "host", "port", "ssh_opts", "remote_dir", "notes")}
    d["id"] = (d["id"] or "").strip().lower()
    if not ID_RE.match(d["id"]):
        raise DeviceError("id: lowercase letters, digits and -, up to 32, e.g. rp6, thor, pbos-1")
    d["name"] = (d["name"] or d["id"]).strip()[:60]
    d["user"] = (d["user"] or "steamos").strip()
    if not USER_RE.match(d["user"]):
        raise DeviceError("ssh user: a Linux user name, e.g. steamos")
    d["host"] = (d["host"] or "").strip()
    if not valid_host(d["host"]):
        raise DeviceError("address: an IP address or host name")
    try:
        d["port"] = int(d["port"] or 22)
    except (TypeError, ValueError):
        raise DeviceError("port: a number")
    if not 1 <= d["port"] <= 65535:
        raise DeviceError("port: 1-65535")
    d["remote_dir"] = (d["remote_dir"] or "bench").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", d["remote_dir"]) or ".." in d["remote_dir"].split("/"):
        raise DeviceError("remote folder: a path under the home folder, e.g. bench")
    if d.get("ssh_opts") and not allow_ssh_opts:
        raise DeviceError("ssh options can only be set in devices.json or with `bench devices add --ssh-opts`")
    d["ssh_opts"] = (d["ssh_opts"] or "").strip()
    d["notes"] = (d["notes"] or "").strip()[:200]
    return d


def _import_remote_conf() -> dict | None:
    f = ROOT / "remote.conf"
    if not f.exists():
        return None
    conf = {}
    for line in f.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip()
    target = conf.get("target", "")
    if "@" not in target:
        return None
    user, host = target.split("@", 1)
    port = 22
    if host.count(":") == 1:  # host:port
        host, p = host.split(":")
        port = int(p)
    return {"default": "default", "results": conf.get("results"),
            "devices": [{"id": "default", "name": "default", "user": user, "host": host, "port": port,
                         "ssh_opts": conf.get("ssh_opts", ""), "remote_dir": conf.get("remote_dir", "bench"),
                         "notes": "imported from remote.conf"}]}


def load() -> dict:
    if FILE.exists():
        data = json.loads(FILE.read_text())
    else:
        data = _import_remote_conf() or {"default": None, "devices": []}
        if data["devices"]:
            save(data)
    data.setdefault("devices", [])
    if data.get("default") not in {d["id"] for d in data["devices"]}:
        data["default"] = data["devices"][0]["id"] if data["devices"] else None
    return data


def save(data: dict) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(FILE)


def get(dev_id: str | None) -> dict | None:
    data = load()
    want = dev_id or data.get("default")
    return next((d for d in data["devices"] if d["id"] == want), None)


def add(dev: dict, *, allow_ssh_opts: bool, replace: bool = False) -> dict:
    d = check(dev, allow_ssh_opts=allow_ssh_opts)
    data = load()
    old = next((x for x in data["devices"] if x["id"] == d["id"]), None)
    if old and not replace:
        raise DeviceError(f"a device called {d['id']} exists already")
    if old and not allow_ssh_opts and d.get("kind") != "android":
        d["ssh_opts"] = old.get("ssh_opts", "")  # web edits keep what the CLI set
    ids = [x["id"] for x in data["devices"]]
    if d["id"] in ids:
        data["devices"][ids.index(d["id"])] = d   # keep its place in the list
    else:
        data["devices"].append(d)
    if not data.get("default"):
        data["default"] = d["id"]
    save(data)
    return d


def remove(dev_id: str) -> None:
    data = load()
    if not any(d["id"] == dev_id for d in data["devices"]):
        raise DeviceError(f"no device {dev_id}")
    data["devices"] = [d for d in data["devices"] if d["id"] != dev_id]
    if data.get("default") == dev_id:
        data["default"] = data["devices"][0]["id"] if data["devices"] else None
    save(data)


def set_default(dev_id: str) -> None:
    data = load()
    if not any(d["id"] == dev_id for d in data["devices"]):
        raise DeviceError(f"no device {dev_id}")
    data["default"] = dev_id
    save(data)


def conf(dev: dict, data: dict | None = None) -> dict:
    """The dict remote.Remote takes (android.Android for an Android device)."""
    if dev.get("kind") == "android":
        c = {"kind": "android", "serial": dev["serial"], "device": dev["id"]}
        res = (data or load()).get("results")
        if res:
            c["results"] = res
        return c
    host = f"[{dev['host']}]" if ":" in dev["host"] else dev["host"]
    opts = dev.get("ssh_opts", "")
    if int(dev.get("port") or 22) != 22:
        opts = f"-p {int(dev['port'])} {opts}".strip()
    c = {"target": f"{dev['user']}@{host.strip('[]')}", "ssh_opts": opts, "remote_dir": dev.get("remote_dir", "bench"),
         "device": dev["id"]}
    res = (data or load()).get("results")
    if res:
        c["results"] = res
    return c
