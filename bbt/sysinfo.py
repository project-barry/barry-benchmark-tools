"""System snapshot taken with every run: what was being tested.

Read-only. Nothing here writes to sysfs.
"""
from __future__ import annotations

import glob
import os
import platform
import re
import struct
from functools import lru_cache
from pathlib import Path

from .sampler import cooling_devices, cpu_policies, gpu_devfreq, power_state, thermal_zones
from .util import proc_cmdline, rd, rd_int, sh


def _cpufreq() -> list[dict]:
    out = []
    for p in cpu_policies():
        d = p["path"]
        khz = lambda f: (rd_int(f"{d}/{f}", 0) or 0) // 1000
        e = {"policy": p["name"], "cpus": p["cpus"], "governor": rd(f"{d}/scaling_governor"),
             "min_mhz": khz("scaling_min_freq"), "max_mhz": khz("scaling_max_freq"),
             "hw_min_mhz": khz("cpuinfo_min_freq"), "hw_max_mhz": khz("cpuinfo_max_freq")}
        rl = rd(f"{d}/schedutil/rate_limit_us")
        if rl:
            e["schedutil_rate_limit_us"] = int(rl)
        epp = rd(f"{d}/energy_performance_preference")
        if epp:
            e["epp"] = epp
        out.append(e)
    return out


def _devfreq() -> list[dict]:
    out = []
    for d in sorted(glob.glob("/sys/class/devfreq/*")):
        mhz = lambda f: (rd_int(f"{d}/{f}", 0) or 0) // 1_000_000
        out.append({"name": os.path.basename(d), "governor": rd(f"{d}/governor"),
                    "min_mhz": mhz("min_freq"), "max_mhz": mhz("max_freq"),
                    "polling_interval_ms": rd_int(f"{d}/polling_interval")})
    return out


def _running(comm_substr: str) -> bool:
    for pid in os.listdir("/proc"):
        if pid.isdigit() and comm_substr in " ".join(proc_cmdline(int(pid))[:2]):
            return True
    return False


def _memory() -> dict:
    mi = {}
    for line in rd("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        mi[k] = v.strip()
    zram = {}
    for z in glob.glob("/sys/block/zram*"):
        algo = re.search(r"\[([\w-]+)\]", rd(f"{z}/comp_algorithm"))
        zram[os.path.basename(z)] = {"algorithm": algo.group(1) if algo else None,
                                     "disksize_mib": (rd_int(f"{z}/disksize", 0) or 0) // 2**20}
    vm = {k: rd(f"/proc/sys/vm/{k}") for k in ("swappiness", "vfs_cache_pressure", "dirty_ratio",
                                                "page-cluster", "min_free_kbytes", "watermark_scale_factor")}
    thp = re.search(r"\[(\w+)\]", rd("/sys/kernel/mm/transparent_hugepage/enabled"))
    return {"total": mi.get("MemTotal"), "swap_total": mi.get("SwapTotal"), "zram": zram, "vm": vm,
            "thp": thp.group(1) if thp else None,
            "zswap": rd("/sys/module/zswap/parameters/enabled") or None}


@lru_cache(maxsize=1)
def _static() -> dict:
    """Things that do not change within a session (vulkaninfo is slow)."""
    os_rel = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', rd("/etc/os-release"), re.M))
    vk = sh(["vulkaninfo", "--summary"], timeout=30)
    vk_get = lambda k: (re.search(rf"{k}\s*=\s*(.+)", vk) or [None, None])[1]
    pkgs = {}
    for line in sh(["pacman", "-Q"], timeout=30).splitlines():
        name, _, ver = line.partition(" ")
        if re.search(r"mesa|vulkan-|mangohud|gamescope|^steam$|scx|fex|box64|inputplumber|linux-firmware|steamos-manager", name):
            pkgs[name] = ver
    steam_build = rd(Path.home() / ".local/share/Steam/steamrtarm64/builddate.txt")
    return {
        "hostname": platform.node(),
        "model": rd("/proc/device-tree/model").rstrip("\0"),
        "soc": rd("/proc/device-tree/compatible").replace("\0", " ").strip(),
        "os": {"name": os_rel.get("NAME"), "version_id": os_rel.get("VERSION_ID"),
               "build_id": os_rel.get("BUILD_ID"), "variant": os_rel.get("VARIANT_ID")},
        "kernel": {"release": platform.release(), "version": platform.version(),
                   "cmdline": rd("/proc/cmdline")},
        "gpu": {"device": vk_get("deviceName"), "driver": vk_get("driverName"),
                "driver_info": vk_get("driverInfo"), "vulkan_api": vk_get("apiVersion")},
        "packages": pkgs,
        "steam_client_build": steam_build or None,
    }


def gamescope() -> dict:
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            cmd = proc_cmdline(int(pid))
            if cmd and os.path.basename(cmd[0]) == "gamescope" and "--" in cmd:
                args = cmd[1:cmd.index("--")]
                def opt(name):
                    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None
                return {"running": True, "args": " ".join(args),
                        "nested": f"{opt('--nested-width')}x{opt('--nested-height')}@{opt('--nested-refresh')}",
                        "output": f"{opt('--output-width')}x{opt('--output-height')}",
                        "framerate_limit": opt("--framerate-limit")}
    return {"running": False}


def dynamic() -> dict:
    """Settings that a test changes: clocks, governors, scheduler, memory, power."""
    temps = {}
    for g, files in thermal_zones().items():
        vals = [v for v in (rd_int(x) for x in files) if v is not None]
        temps[g] = round(max(vals) / 1000, 1) if vals else None
    return {
        "cpufreq": _cpufreq(),
        "devfreq": _devfreq(),
        "cpuidle": {"governor": rd("/sys/devices/system/cpu/cpuidle/current_governor"),
                    "driver": rd("/sys/devices/system/cpu/cpuidle/current_driver")},
        "scheduler": {"sched_ext": rd("/sys/kernel/sched_ext/state") or "absent",
                      "sched_ext_ops": rd("/sys/kernel/sched_ext/root/ops") or None,
                      "boostd": _running("sm8550-boostd"), "fand": _running("sm8550-fand")},
        "memory": _memory(),
        "power": power_state(),
        "temps_c": temps,
        "cooling": {n: rd_int(p) for n, p in cooling_devices()},
        "gamescope": gamescope(),
    }


def snapshot() -> dict:
    return {"system": _static(), **dynamic()}


# ---- binaries / emulation ------------------------------------------------------

PE_MACHINES = {0x14C: "x86", 0x8664: "x86_64", 0xAA64: "arm64", 0xA641: "arm64ec", 0x1C4: "arm"}
ELF_MACHINES = {0x03: "x86", 0x3E: "x86_64", 0xB7: "arm64", 0x28: "arm"}


def binary_arch(path) -> dict:
    """{'format': 'PE'|'ELF', 'arch': ...} from the file header."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:4] == b"\x7fELF":
                return {"format": "ELF", "arch": ELF_MACHINES.get(struct.unpack_from("<H", head, 0x12)[0], "unknown")}
            if head[:2] == b"MZ":
                off = struct.unpack_from("<I", head, 0x3C)[0]
                f.seek(off)
                pe = f.read(6)
                if pe[:4] == b"PE\0\0":
                    return {"format": "PE", "arch": PE_MACHINES.get(struct.unpack_from("<H", pe, 4)[0], "unknown")}
    except OSError:
        pass
    return {"format": None, "arch": "unknown"}


def emulation(game_arch: str, runtime: str, tool_arch: str | None = None, evidence: list[str] | None = None) -> dict:
    """How x86 code runs on this ARM device, for the report.

    runtime: 'native' (Linux binary) or 'proton'."""
    host = platform.machine()
    ev = evidence or []
    if game_arch in ("arm64", "arm64ec") and runtime == "native":
        method, emulated = "none (native aarch64)", False
    elif runtime == "proton" and tool_arch == "arm64":
        method = ("FEX via Wine WoW64 (32-bit x86 game, arm64 Proton)" if game_arch == "x86"
                  else "FEX via Wine ARM64EC (x86-64 game, arm64 Proton)" if game_arch == "x86_64"
                  else "none (arm64 Windows binary)")
        emulated = game_arch in ("x86", "x86_64")
    elif runtime == "proton":
        method, emulated = "FEX, whole process (x86-64 Proton)", True
    elif game_arch in ("x86", "x86_64"):
        method, emulated = "FEX or box64 (x86 Linux binary)", True
    else:
        method, emulated = "unknown", None
    if any("FEXServer" in e or "FEXInterpreter" in e for e in ev) and "FEX" not in method:
        method += " + FEX seen"
    return {"host_arch": host, "game_arch": game_arch, "x86_emulated": emulated, "method": method,
            "evidence": sorted(set(ev))[:20]}


def emulation_evidence(pids: list[int]) -> list[str]:
    """Emulator pieces seen in the running game's processes (cmdline + maps)."""
    keys = ("FEXInterpreter", "FEXServer", "FEXLoader", "libarm64ecfex", "libwow64fex",
            "xtajit", "box64", "box86")
    hits = set()
    for pid in pids:
        cmd = " ".join(proc_cmdline(pid)[:3])
        for k in keys:
            if k in cmd:
                hits.add(k)
        try:
            with open(f"/proc/{pid}/maps") as f:
                maps = f.read()
        except OSError:
            continue
        for k in keys:
            if k.lower() in maps.lower():
                hits.add(k)
    return sorted(hits)
