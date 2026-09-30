"""Game settings in a Proton prefix's registry (user.reg), e.g. Tomb Raider's
HKCU\\Software\\Crystal Dynamics\\Tomb Raider\\Graphics\\VSyncMode.

user.reg is plain text that wineserver loads at start and rewrites at exit,
so it is only edited while no process of that prefix runs. A backup is kept
(state/backups/<appid>-user.reg.orig once, plus one per edit).
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from . import steam
from .util import STATE, log, rd


def prefix(appid: str) -> Path:
    man = steam.app_manifest(appid)
    lib = Path(man["_library"]) if man else steam.STEAM
    return lib / "steamapps/compatdata" / str(appid) / "pfx"


def prefix_busy(pfx: Path) -> bool:
    """Any process (wineserver included) using this prefix?"""
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        env = rd(f"/proc/{d}/environ")
        if f"WINEPREFIX={pfx}" in env or f"STEAM_COMPAT_DATA_PATH={pfx.parent}" in env:
            return True
    return False


def _reg_value(v) -> str:
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return f"dword:{v & 0xFFFFFFFF:08x}"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _section_name(key: str) -> str:
    k = key.replace("/", "\\")
    for root in ("HKEY_CURRENT_USER\\", "HKCU\\"):
        if k.upper().startswith(root.upper()):
            k = k[len(root):]
    return k.replace("\\", "\\\\")  # user.reg doubles the separators


def set_values(appid: str, values: dict[str, dict]) -> bool:
    """values: {'HKCU\\Software\\X\\Y': {'Name': 1, 'Other': 'text'}} -> changed?"""
    pfx = prefix(appid)
    reg = pfx / "user.reg"
    if not reg.exists():
        raise RuntimeError(f"no prefix for {appid} yet (launch the game once first)")
    if prefix_busy(pfx):
        raise RuntimeError(f"prefix {appid} is in use; refusing to edit user.reg")
    lines = reg.read_text(errors="surrogateescape").split("\n")
    changed = False
    for key, vals in values.items():
        sec = _section_name(key)
        head = re.compile(r"^\[" + re.escape(sec) + r"\](\s|$)", re.I)
        idx = next((i for i, l in enumerate(lines) if head.match(l)), None)
        if idx is None:
            while lines and lines[-1] == "":
                lines.pop()
            lines += ["", f"[{sec}] {int(time.time())}", ""]
            idx = len(lines) - 2
            changed = True
        end = idx + 1
        while end < len(lines) and not lines[end].startswith("["):
            end += 1
        for name, v in vals.items():
            want = f'"{name}"={_reg_value(v)}'
            pat = re.compile(r'^"' + re.escape(name) + r'"=', re.I)
            hit = next((i for i in range(idx + 1, end) if pat.match(lines[i])), None)
            if hit is None:
                ins = end
                while ins > idx + 1 and lines[ins - 1] == "":
                    ins -= 1
                lines.insert(ins, want)
                end += 1
                changed = True
            elif lines[hit] != want:
                lines[hit] = want
                changed = True
    if not changed:
        return False
    bak = STATE / "backups"
    bak.mkdir(parents=True, exist_ok=True)
    first = bak / f"{appid}-user.reg.orig"
    if not first.exists():
        shutil.copy2(reg, first)
    shutil.copy2(reg, bak / f"{appid}-user.reg.{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = reg.with_name("user.reg.bbt-tmp")
    tmp.write_text("\n".join(lines), errors="surrogateescape")
    tmp.replace(reg)
    log(f"set registry values for {appid}: {values}")
    return True


def get_values(appid: str, key: str) -> dict[str, str]:
    sec = _section_name(key)
    out, inside = {}, False
    for l in rd(prefix(appid) / "user.reg").split("\n"):
        if l.startswith("["):
            inside = bool(re.match(r"^\[" + re.escape(sec) + r"\](\s|$)", l, re.I))
            continue
        m = re.match(r'^"([^"]+)"=(.*)$', l) if inside else None
        if m:
            out[m.group(1)] = m.group(2)
    return out
