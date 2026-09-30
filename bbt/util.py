"""Shared helpers: paths, logging, sysfs reads, process-tree walking."""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(os.environ.get("BBT_HOME", Path.home() / "bench"))
STATE = BENCH / "state"
RESULTS = BENCH / "results"
OPT = BENCH / "opt"


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def rd(path, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def rd_int(path, default=None):
    try:
        return int(rd(path))
    except ValueError:
        return default


def sh(cmd, timeout: float = 30, env=None, check: bool = False) -> str:
    """Run a command, return stdout ('' on failure unless check)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, shell=isinstance(cmd, str))
    except (OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise
        return ""
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd!r} failed ({p.returncode}): {p.stderr.strip()}")
    return p.stdout.strip()


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def slug(s: str) -> str:
    """'Tomb Raider (2013)' -> 'Tomb_Raider_2013' (safe in file names)."""
    out, prev_us = [], False
    for ch in s:
        if ch.isalnum() or ch in "-.":
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_.") or "untitled"


# ---- processes -------------------------------------------------------------

def proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def proc_children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        st = rd(f"/proc/{d}/stat")
        if not st:
            continue
        try:
            ppid = int(st[st.rindex(")") + 2:].split()[1])
        except (ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    return kids


def proc_tree(root: int) -> list[int]:
    kids = proc_children_map()
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


def pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def wait_until(pred, timeout: float, interval: float = 1.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())
