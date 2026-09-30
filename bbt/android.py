"""Android devices: sessions driven from this machine over adb.

Nothing is installed on the device and no root is needed. The session itself
(cooldowns, runs, statistics, reports) runs here; the device does three things
for each run, all as adb's `shell` user:

  sampler     a small sh loop in /data/local/tmp/bbt that prints /proc/stat,
              /proc/meminfo, clocks, temperatures, throttling and power once
              per interval (the same columns as the Linux sampler, see
              sampler.py; GPU busy is the whole GPU from KGSL, not per process)
  frames      Perfetto's SurfaceFlinger frame timeline (see perfetto.py), or
              for apps it cannot see (GameNative and Winlator draw through a
              SurfaceView it tracks as one frame) SurfaceFlinger's per-layer
              frame log, `dumpsys SurfaceFlinger --latency` (frame_source:
              surfaceflinger; layer: then matches `dumpsys SurfaceFlinger --list`)
  the game    started with an intent (`am start ...`), or by hand ("manual")

Scenario (kind: android):

  - name: tr2013-gamenative
    title: Tomb Raider (2013)
    kind: android
    package: app.gamenative            # the app that runs the game
    launch:                            # `am start` arguments; "manual" = you start it
      [-n, app.gamenative/.MainActivity, -a, app.gamenative.LAUNCH_GAME, --ei, app_id, 203160]
    process: 'TombRaider\\.exe'         # regex on `ps` command lines: the game is running
    layer: 'SurfaceView'               # regex on the layer that shows the game (default: the busiest)
    capture: until_exit                # or window (settle_s, duration_s), as for Steam scenarios
    trim_start_s: 6
    trim_end_s: 1
    timeout_s: 600

The game counts as running while `process` matches (or, without it, while a
layer matching `layer` exists, or else while the package's process is alive).
The app is force-stopped before and after every run (stop_app: false keeps it).
"""
from __future__ import annotations

import csv
import json
import re
import shlex
import threading
import time
import uuid
from pathlib import Path

from . import perfetto, stats
from .adb import Adb, AdbError
from .sampler import power_from, summarize, window, zone_group
from .util import log, wait_until

DIR = "/data/local/tmp/bbt"
TRACES = "/data/misc/perfetto-traces"
KGSL = "/sys/class/kgsl/kgsl-3d0"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"
# cooling devices that slow the CPU, GPU, memory or display down (Qualcomm names)
COOLING = ("cpufreq", "devfreq", "gpu", "cpu-cluster", "thermal-cluster", "pause-cpu", "thermal-pause",
           "cpu-hotplug", "ddr-cdev", "display-fps")
EMU_KEYS = ("box64", "box86", "fex", "wowbox64", "arm64ec", "wine", "wineserver", "proton")


class RunError(RuntimeError):
    pass


def _i(v, default=0) -> int:
    try:
        return int(str(v).split()[0])
    except (ValueError, IndexError, TypeError):
        return default


class Android:
    """The platform a Session runs against when the device is an Android one."""
    kind = "android"

    def __init__(self, serial: str, device_id: str | None = None):
        self.adb = Adb(serial)
        self.id = device_id or serial
        self._hw: dict | None = None
        self._static: dict | None = None
        self._rd: dict[str, str] = {}

    # -- discovery -----------------------------------------------------------------------
    @property
    def hw(self) -> dict:
        """Readable sysfs nodes: CPU policies, KGSL, thermal zones, cooling devices, supplies."""
        if self._hw is None:
            out = self.adb.sh(
                f'for p in {CPUFREQ}/policy*; do echo "P $p $(cat $p/related_cpus)"; done; '
                'for z in /sys/class/thermal/thermal_zone*; do read t < $z/temp 2>/dev/null && echo "Z $z $(cat $z/type)"; done; '
                'for c in /sys/class/thermal/cooling_device*; do read s < $c/cur_state 2>/dev/null && echo "C $c $(cat $c/type)"; done; '
                'for s in /sys/class/power_supply/*; do echo "S ${s##*/}"; done; '
                f'read g < {KGSL}/gpuclk 2>/dev/null && echo "G {KGSL}"', timeout=30)
            hw = {"policies": [], "zones": {}, "cooling": [], "supplies": [], "gpu": None}
            for line in out.splitlines():
                tag, _, rest = line.partition(" ")
                path, _, val = rest.partition(" ")
                if tag == "P":
                    hw["policies"].append({"name": path.rsplit("/", 1)[1], "path": path,
                                           "cpus": [int(c) for c in val.split()]})
                elif tag == "Z" and zone_group(val):
                    hw["zones"].setdefault(zone_group(val), []).append(f"{path}/temp")
                elif tag == "C" and val.startswith(COOLING):
                    hw["cooling"].append((val, f"{path}/cur_state"))
                elif tag == "S" and path != "battery":
                    hw["supplies"].append(path)
                elif tag == "G":
                    hw["gpu"] = path
            hw["policies"].sort(key=lambda p: int(p["name"][6:]))
            if not hw["policies"]:
                raise AdbError("no CPU frequency policies readable over adb")
            self._hw = hw
        return self._hw

    def rd(self, path: str, default: str = "") -> str:
        """One sysfs file (cached): used for the frequency tables behind throttle steps."""
        if path not in self._rd:
            self._rd[path] = self.adb.cat([path]).get(path) or ""
        return self._rd[path] or default

    def uptime(self) -> float:
        return float(self.adb.sh("cat /proc/uptime").split()[0])

    # -- sensors --------------------------------------------------------------------------
    def temps(self) -> dict:
        files = [f for fs in self.hw["zones"].values() for f in fs]
        vals = self.adb.cat(files)
        out = {}
        for g, fs in self.hw["zones"].items():
            v = [_i(vals[f], None) for f in fs if vals.get(f)]
            v = [x for x in v if x is not None]
            if v:
                out[g] = max(v) / 1000
        return out

    def power(self) -> dict:
        keys = ("voltage_now", "current_now", "capacity", "status", "online")
        names = ["battery"] + self.hw["supplies"]
        files = [f"/sys/class/power_supply/{n}/{k}" for n in names for k in keys]
        v = self.adb.cat(files)
        ue = {n: {k.upper(): v.get(f"/sys/class/power_supply/{n}/{k}") or "" for k in keys} for n in names}
        return power_from(ue["battery"], [ue[n] for n in self.hw["supplies"]])

    # -- snapshot (same shape as sysinfo.snapshot, so reports and diffs line up) -----------
    def _vulkan(self) -> dict:
        try:
            vk = json.loads(self.adb.sh("cmd gpu vkjson", timeout=30))
            dev = vk["devices"][0]
            props = dev.get("properties", {})
            drv = dev.get("VK_KHR_driver_properties", {}).get("driverPropertiesKHR", {})
            api = int(props.get("apiVersion", vk.get("apiVersion", 0)))
            return {"device": props.get("deviceName"), "driver": drv.get("driverName") or "Qualcomm proprietary",
                    "driver_info": " ".join((drv.get("driverInfo") or "").split()) or None,
                    "vulkan_api": f"{(api >> 22) & 0x7F}.{(api >> 12) & 0x3FF}.{api & 0xFFF}" if api else None}
        except (ValueError, KeyError, IndexError, TypeError, AdbError):
            gles = re.search(r"GLES: (.+)", self.adb.sh("dumpsys SurfaceFlinger | grep -m1 GLES:", timeout=20))
            return {"device": None, "driver": "Qualcomm proprietary", "driver_info": gles.group(1) if gles else None,
                    "vulkan_api": None}

    def _packages(self) -> dict:
        names = re.findall(r"^package:(\S+)", self.adb.sh("pm list packages", timeout=30), re.M)
        out = {}
        for n in names:
            if re.search(r"gamenative|winlator|gamehub|mobox|termux|retroidpocket\.gamelauncher", n):
                m = re.search(r"versionName=(\S+)", self.adb.sh(f"dumpsys package {n} | grep -m1 versionName"))
                out[n] = m.group(1) if m else None
        return out

    def static(self) -> dict:
        if self._static is None:
            p = self.adb.getprops()
            settings = {}
            for ns in ("system", "global", "secure"):
                for line in self.adb.sh(f"settings list {ns}").splitlines():
                    k, _, v = line.partition("=")
                    if re.search(r"fan|perf|refresh|game|charg", k, re.I):
                        settings[f"{ns}.{k}"] = v
            self._static = {
                "hostname": p.get("net.hostname") or p.get("ro.product.device"),
                "model": p.get("ro.product.model"),
                "soc": f"{p.get('ro.soc.manufacturer', '')} {p.get('ro.soc.model', '')} ({p.get('ro.board.platform')})".strip(),
                "serial": p.get("ro.serialno"),
                "os": {"name": "Android", "version_id": p.get("ro.build.version.release"),
                       "build_id": p.get("ro.build.display.id"), "variant": p.get("ro.build.type"),
                       "sdk": p.get("ro.build.version.sdk"), "security_patch": p.get("ro.build.version.security_patch")},
                "kernel": {"release": self.adb.sh("uname -r"), "version": self.adb.sh("uname -v"), "cmdline": None},
                "gpu": self._vulkan(),
                "packages": self._packages(),
                "steam_client_build": None,
                "bootloader_unlocked": p.get("ro.boot.flash.locked") == "0",
                "settings": settings,
            }
        return self._static

    def _display(self) -> dict:
        sf = self.adb.sh("dumpsys SurfaceFlinger | grep -m1 -E 'refresh-rate'", timeout=20)
        rate = re.search(r"([\d.]+)\s*Hz", sf)
        peak = self.adb.sh("settings get system peak_refresh_rate")
        mn = self.adb.sh("settings get system min_refresh_rate")
        return {"refresh_hz": float(rate.group(1)) if rate else None,
                "peak_refresh_setting": None if peak in ("", "null") else peak,
                "min_refresh_setting": None if mn in ("", "null") else mn}

    def dynamic(self) -> dict:
        hw = self.hw
        pf = ("scaling_governor", "scaling_min_freq", "scaling_max_freq", "cpuinfo_min_freq", "cpuinfo_max_freq",
              "scaling_available_frequencies")
        g = ("devfreq/governor", "min_clock_mhz", "max_clock_mhz", "gpuclk", "thermal_pwrlevel", "default_pwrlevel")
        mem_files = ["/proc/sys/vm/" + k for k in ("swappiness", "vfs_cache_pressure", "dirty_ratio", "page-cluster",
                                                  "min_free_kbytes", "watermark_scale_factor")]
        extra = ["/sys/devices/system/cpu/cpuidle/current_governor", "/sys/devices/system/cpu/cpuidle/current_driver",
                 "/sys/kernel/mm/transparent_hugepage/enabled", "/sys/block/zram0/comp_algorithm", "/sys/block/zram0/disksize"]
        files = [f"{p['path']}/{f}" for p in hw["policies"] for f in pf] + \
                ([f"{hw['gpu']}/{f}" for f in g] if hw["gpu"] else []) + mem_files + extra + \
                [f for fs in hw["zones"].values() for f in fs] + [c for _, c in hw["cooling"]]
        v = self.adb.cat(files)
        khz = lambda path: _i(v.get(path)) // 1000
        cpufreq = []
        for p in hw["policies"]:
            d = p["path"]
            table = [int(x) // 1000 for x in (v.get(f"{d}/scaling_available_frequencies") or "").split() if x.isdigit()]
            # cpuinfo_min_freq is root-only on Android: fall back to the frequency table
            hw_min = khz(f"{d}/cpuinfo_min_freq") if v.get(f"{d}/cpuinfo_min_freq") else (min(table) if table else None)
            hw_max = khz(f"{d}/cpuinfo_max_freq") if v.get(f"{d}/cpuinfo_max_freq") else (max(table) if table else None)
            cpufreq.append({"policy": p["name"], "cpus": p["cpus"], "governor": v.get(f"{d}/scaling_governor"),
                            "min_mhz": khz(f"{d}/scaling_min_freq"), "max_mhz": khz(f"{d}/scaling_max_freq"),
                            "hw_min_mhz": hw_min, "hw_max_mhz": hw_max})
        devfreq = []
        if hw["gpu"]:
            k = hw["gpu"]
            devfreq.append({"name": "gpu (kgsl-3d0)", "governor": v.get(f"{k}/devfreq/governor"),
                            "min_mhz": _i(v.get(f"{k}/min_clock_mhz")), "max_mhz": _i(v.get(f"{k}/max_clock_mhz")),
                            "polling_interval_ms": None, "thermal_pwrlevel": _i(v.get(f"{k}/thermal_pwrlevel"), None)})
        mi = dict(re.findall(r"^(\w+):\s+(.+)$", self.adb.sh("cat /proc/meminfo"), re.M))
        algo = re.search(r"\[([\w-]+)\]", v.get("/sys/block/zram0/comp_algorithm") or "")
        thp = re.search(r"\[(\w+)\]", v.get("/sys/kernel/mm/transparent_hugepage/enabled") or "")
        zram = {"zram0": {"algorithm": algo.group(1) if algo else None,
                          "disksize_mib": _i(v.get("/sys/block/zram0/disksize")) // 2**20}} if algo else {}
        temps = {}
        for grp, fs in hw["zones"].items():
            vals = [_i(v.get(f), None) for f in fs]
            vals = [x for x in vals if x is not None]
            temps[grp] = round(max(vals) / 1000, 1) if vals else None
        cooling: dict[str, int] = {}
        for name, path in hw["cooling"]:
            cooling[name] = max(cooling.get(name, 0), _i(v.get(path)))
        perf_hal = self.adb.sh("getprop init.svc.vendor.perfservice") or None
        return {
            "cpufreq": cpufreq,
            "devfreq": devfreq,
            "cpuidle": {"governor": v.get("/sys/devices/system/cpu/cpuidle/current_governor"),
                        "driver": v.get("/sys/devices/system/cpu/cpuidle/current_driver")},
            "scheduler": {"sched_ext": "absent", "sched_ext_ops": None, "boostd": False, "fand": False,
                          "vendor_perf_hal": perf_hal},
            "memory": {"total": mi.get("MemTotal"), "swap_total": mi.get("SwapTotal"), "zram": zram,
                       "vm": {f.rsplit("/", 1)[1]: v.get(f) for f in mem_files},
                       "thp": thp.group(1) if thp else None, "zswap": None},
            "power": self.power(),
            "temps_c": temps,
            "cooling": cooling,
            "gamescope": {"running": False},
            "display": self._display(),
        }

    def snapshot(self) -> dict:
        return {"system": self.static(), **self.dynamic()}

    def run_snapshot(self) -> dict:
        return {"static": self.static(), **self.dynamic()}

    # -- session hooks ----------------------------------------------------------------------
    def lock_path(self) -> Path:
        from .remote import ROOT
        return ROOT / ".bbt-state" / f"{self.id}.lock"

    def prepare(self) -> None:
        """Session start: clean the work folder and keep the screen on (a game draws
        nothing while the display sleeps). finish() puts the settings back."""
        self.adb.transport()
        self.adb.sh(f"mkdir -p {DIR}; rm -f {DIR}/sampler* {DIR}/frames-* {TRACES}/bbt-*.pftrace", check=True)
        self._saved = {k: self.adb.sh(f"settings get {k}") for k in
                       ("global stay_on_while_plugged_in", "system screen_off_timeout")}
        self.adb.sh("settings put global stay_on_while_plugged_in 7; "   # AC, USB, wireless
                    "settings put system screen_off_timeout 1800000")     # 30 min on battery
        self.wake()

    def finish(self) -> None:
        for k, v in (getattr(self, "_saved", None) or {}).items():
            if v and v != "null":
                self.adb.sh(f"settings put {k} {shlex.quote(v)}")
        self._saved = None

    def wake(self) -> None:
        """Screen on and past the lock screen (only works without a PIN or pattern)."""
        self.adb.sh("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        time.sleep(1)
        if "isKeyguardShowing=true" in self.adb.sh("dumpsys window | grep -m1 isKeyguardShowing"):
            raise RunError("the lock screen is showing: unlock the device (a PIN or pattern cannot be dismissed over adb)")

    def runner(self, kind: str):
        if kind != "android":
            raise RunError(f"{kind} scenarios run on SteamOS devices, not on Android (use kind: android)")
        return self.run_android

    def cleanup(self, sc: dict) -> None:
        if sc.get("package") and sc.get("stop_app", True):
            self.adb.sh(f"am force-stop {shlex.quote(sc['package'])}")

    # the web app's device page asks these of every device; Android sessions run here
    def running_units(self) -> list[str]:
        return []

    def remote_sessions(self) -> list[str]:
        return []

    def probe(self) -> dict:
        """Can we reach it over adb, and what does it offer? Never raises."""
        try:
            self.adb.transport()
            p = self.adb.getprops()
            pv = self.adb.sh("perfetto --version").split()
            sources = self.adb.sh("perfetto --query 2>/dev/null | grep -c android.surfaceflinger.frametimeline")
            v = self.adb.cat([f"{KGSL}/gpu_busy_percentage", f"{CPUFREQ}/policy0/scaling_cur_freq"])
        except AdbError as e:
            return {"ok": False, "problem": "adb", "detail": str(e)}
        facts = {"transport": self.adb._t, "serial": p.get("ro.serialno"), "model": p.get("ro.product.model"),
                 "os": f"Android {p.get('ro.build.version.release')} ({p.get('ro.build.display.id')})",
                 "kernel": self.adb.sh("uname -r"), "arch": p.get("ro.product.cpu.abi"),
                 "perfetto": pv[1] if len(pv) > 1 else None,
                 "frametimeline": sources.strip() not in ("", "0"),
                 "gpu_busy": v.get(f"{KGSL}/gpu_busy_percentage") is not None,
                 "cpufreq": v.get(f"{CPUFREQ}/policy0/scaling_cur_freq") is not None,
                 "bootloader_unlocked": p.get("ro.boot.flash.locked") == "0"}
        facts["packages"] = ", ".join(f"{k} {v or ''}".strip() for k, v in self._packages().items()) or None
        missing = [k for k in ("frametimeline", "cpufreq") if not facts[k]]
        facts.update(can_run=not missing, missing=missing, steam=False, kind="android")
        return {"ok": True, "facts": facts}

    # -- the runner ----------------------------------------------------------------------------
    def _ps(self) -> list[tuple[int, str, str]]:
        out = self.adb.sh("ps -A -o PID,USER,ARGS", timeout=20)
        rows = []
        for line in out.splitlines()[1:]:
            f = line.split(None, 2)
            if len(f) == 3 and f[0].isdigit():
                rows.append((int(f[0]), f[1], f[2]))
        return rows

    def _app_user(self, pkg: str) -> str | None:
        m = re.search(r"uid:(\d+)", self.adb.sh(f"pm list packages -U {shlex.quote(pkg)}"))
        if not m:
            return None
        uid = int(m.group(1))
        return f"u{uid // 100000}_a{uid % 100000 - 10000}"

    def _layers(self) -> list[str]:
        return self.adb.sh("dumpsys SurfaceFlinger --list", timeout=20).splitlines()

    def _running_fn(self, sc: dict):
        pkg = sc["package"]
        if sc.get("process"):
            rx = re.compile(sc["process"])
            return lambda: any(rx.search(a) for _, _, a in self._ps())
        if sc.get("layer"):
            rx = re.compile(sc["layer"])
            return lambda: any(rx.search(l) for l in self._layers())
        return lambda: bool(self.adb.sh(f"pidof {shlex.quote(pkg)}").strip())

    def _launch(self, sc: dict, launch, running, timeout: float, warnings: list[str]) -> bool:
        """am start, then wait for the game. If the app dies before the game is up
        (GameNative crashes on a failed network lookup at start, for one), dismiss
        Android's crash dialog and start it again, up to `launch_retries` times (2)."""
        pkg = sc["package"]
        args = [str(a) for a in (launch if isinstance(launch, list) else shlex.split(str(launch)))]
        end = time.monotonic() + timeout
        for attempt in range(int(sc.get("launch_retries", 2)) + 1):
            r = self.adb.sh("am start " + " ".join(shlex.quote(a) for a in args), timeout=30)
            if "Error" in r:
                raise RunError(f"am start failed: {r.strip()[-200:]}")
            t0 = time.monotonic()
            while time.monotonic() < end:
                if running():
                    return True
                self._tap_buttons(sc, warnings)
                if time.monotonic() - t0 > 15 and not self.adb.sh(f"pidof {shlex.quote(pkg)}").strip():
                    break  # the app is gone: crashed while starting
                time.sleep(2)
            else:
                return False
            warnings.append(f"{pkg} quit while starting the game (attempt {attempt + 1}); started it again")
            log(warnings[-1])
            self.adb.sh("am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS")  # the crash dialog
            self.adb.sh(f"am force-stop {shlex.quote(pkg)}")
            time.sleep(3)
            self.wake()
        return False

    def _tap_buttons(self, sc: dict, warnings: list[str]) -> None:
        """Press a button in a dialog the app shows while starting the game, e.g.
        GameNative's Steam Cloud conflict (tap_buttons: regexes on the button text,
        read from `uiautomator dump`, which fails now and then while the screen changes)."""
        pats = [re.compile(p) for p in (sc.get("tap_buttons") or [])]
        if not pats:
            return
        xml = self.adb.sh("uiautomator dump /sdcard/bbt-ui.xml >/dev/null 2>&1 && cat /sdcard/bbt-ui.xml; "
                          "rm -f /sdcard/bbt-ui.xml", timeout=20)
        nodes = []
        for n in re.findall(r"<node [^>]*>", xml):
            t = re.search(r' text="([^"]*)"', n)
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
            if t and t.group(1) and b:
                x0, y0, x1, y1 = map(int, b.groups())
                nodes.append((t.group(1), (x0 + x1) // 2, (y0 + y1) // 2))
        for p in pats:
            hit = next((n for n in nodes if p.search(n[0])), None)
            if hit:
                self.adb.sh(f"input tap {hit[1]} {hit[2]}")
                warnings.append(f"pressed {hit[0]!r} in a dialog")
                log(warnings[-1])
                time.sleep(2)
                return
        if any("cloud" in n[0].lower() for n in nodes):  # a dialog we have no button for: say what it offers
            log("dialog on screen, no tap_buttons match: " + " | ".join(n[0] for n in nodes)[:300])

    def _notify(self, text: str) -> None:
        self.adb.sh(f"cmd notification post -S bigtext -t 'Barry Benchmark' bbt {shlex.quote(text)}")

    def run_android(self, sc: dict, run_dir: Path, sampler_interval: float) -> dict:
        pkg = sc.get("package")
        if not pkg:
            raise RunError("android scenarios need a package")
        mode = sc.get("capture", "until_exit")
        settle, dur = float(sc.get("settle_s", 30)), float(sc.get("duration_s", 60))
        if mode == "until_exit":
            trim0, trim1 = float(sc.get("trim_start_s", 0)), float(sc.get("trim_end_s", 0))
            timeout = float(sc.get("timeout_s", 900))
        elif mode == "window":
            trim0 = trim1 = 0.0
            timeout = float(sc.get("timeout_s", settle + dur + 180))
        else:
            raise RunError(f"unknown capture mode {mode!r}")
        launch = sc.get("launch", "manual")
        start_timeout = float(sc.get("launch_timeout_s", 300 if launch == "manual" else 180))
        running = self._running_fn(sc)
        user = self._app_user(pkg)
        if user is None:
            raise RunError(f"{pkg} is not installed")
        if sc.get("stop_app", True):
            self.adb.sh(f"am force-stop {shlex.quote(pkg)}")
            time.sleep(2)
        self.wake()

        trace = f"{TRACES}/bbt-{uuid.uuid4().hex[:12]}.pftrace"
        self.adb.push_text(perfetto.config(start_timeout + timeout + 120), f"{DIR}/frametimeline.pbtx")
        out = self.adb.sh(f"cat {DIR}/frametimeline.pbtx | perfetto --background --txt -c - -o {trace}", check=True)
        tpid = next((l.strip() for l in reversed(out.splitlines()) if l.strip().isdigit()), None)
        if not tpid:
            raise RunError(f"perfetto did not start: {out[-200:]}")
        smp = AndroidSampler(self, run_dir / "samples.csv", sampler_interval).start()
        poller = None
        if sc.get("frame_source", "frametimeline") == "surfaceflinger":
            poller = LatencyPoller(self, sc.get("layer") or re.escape(pkg), start_timeout + timeout + 60).start()
        elif sc.get("frame_source", "frametimeline") != "frametimeline":
            raise RunError(f"frame_source: frametimeline or surfaceflinger, not {sc['frame_source']!r}")
        warnings, procs, exited_early = [], [], False
        try:
            if launch == "manual":
                msg = f"Start {sc.get('title', sc['name'])} now"
                log(f"{msg} (waiting up to {start_timeout:.0f} s)")
                self._notify(msg)
            smp.mark("launched")
            if launch == "manual":
                started = wait_until(running, start_timeout, 2)
            else:
                started = self._launch(sc, launch, running, start_timeout, warnings)
            if not started:
                raise RunError(f"the game did not start within {start_timeout:.0f} s")
            t_up = self.uptime()
            smp.mark_boot("game_up", t_up)
            log("game is running; capturing" + (" until it quits" if mode == "until_exit" else ""))
            time.sleep(10)  # fully up: note what runs it
            procs = [a for _, u, a in self._ps() if u == user]
            if mode == "until_exit":
                if not wait_until(lambda: not running(), timeout, 2):
                    raise RunError(f"game still running after {timeout:.0f} s (benchmark did not finish?)")
            else:
                exited_early = wait_until(lambda: not running(), settle + dur + 5, 2)
            t_down = self.uptime()
            smp.mark_boot("game_down", t_down)
        finally:
            self.adb.sh(f"kill -TERM {tpid}")
            wait_until(lambda: not self.adb.sh(f"ls /proc/{tpid}/ 2>/dev/null | head -1"), 20, 0.5)
            if poller:
                poller.stop()
            if sc.get("stop_app", True):
                self.adb.sh(f"am force-stop {shlex.quote(pkg)}")
            rows = smp.stop()
        local = run_dir / "frametimeline.pftrace"
        self.adb.pull(trace, local)
        self.adb.sh(f"rm -f {trace}")
        return self._metrics(sc, run_dir, local, rows, smp, t_up, t_down, mode, (trim0, trim1), (settle, dur),
                             procs, warnings + (["game exited during capture"] if exited_early else []), poller)

    def _metrics(self, sc, run_dir, trace, rows, smp, t_up, t_down, mode, trims, window_s, procs, warnings,
                 poller=None) -> dict:
        lo, hi = int((t_up - 15) * 1e9), int((t_down + 2) * 1e9)
        if poller:  # SurfaceFlinger's frame log: present times on CLOCK_MONOTONIC
            off = perfetto.boot_minus_mono(trace)
            if off is None:
                off = 0
                warnings.append("no clock snapshot in the trace: frame times taken as boot time")
            layer = poller.layers[-1] if poller.layers else None
            seen = {l: None for l in poller.layers}
            mine = [{"end_ns": t + off, "present": "shown"} for t in sorted(poller.presents) if lo <= t + off <= hi]
            if not mine:
                raise RunError(f"no frames from a layer matching {sc.get('layer')!r} "
                               f"(layers found: {', '.join(poller.layers) or 'none'})")
            if poller.overruns:
                warnings.append(f"frame log read too late {poller.overruns} time(s): some frames may be missing")
        else:
            frames = perfetto.surface_frames(trace)
            rx = re.compile(sc["layer"]) if sc.get("layer") else None
            seen: dict[str, int] = {}
            for layer, fr in perfetto.by_layer(frames).items():
                n = sum(1 for f in fr if lo <= f["end_ns"] <= hi and f["present"] != "dropped")
                if n:
                    seen[layer] = n
            cand = {l: n for l, n in seen.items() if (rx.search(l) if rx else sc["package"] in l)} or \
                   ({} if rx else seen)
            if not cand:
                raise RunError(f"no frames from a layer matching {sc.get('layer') or sc['package']!r}; "
                               f"layers seen: {', '.join(sorted(seen)) or 'none'}")
            layer = max(cand, key=cand.get)
            mine = [f for f in frames if f["layer"] == layer and lo <= f["end_ns"] <= hi]
        shown = [f for f in mine if f["present"] != "dropped"]
        e0, e1 = shown[0]["end_ns"], shown[-1]["end_ns"]
        if mode == "until_exit":
            c0, c1 = e0 + trims[0] * 1e9, e1 - trims[1] * 1e9
            trim_s = list(trims)
        else:
            c0, c1 = e0 + window_s[0] * 1e9, e0 + (window_s[0] + window_s[1]) * 1e9
            trim_s = [window_s[0], max(0.0, round((e1 - c1) / 1e9, 3))]
        # frames.csv: every shown frame of the game's layer (MangoHud-style columns, read by the web app)
        with open(run_dir / "frames.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frametime", "elapsed", "present"])
            for a, b in zip(shown, shown[1:]):
                w.writerow([round((b["end_ns"] - a["end_ns"]) / 1e6, 4), b["end_ns"] - e0, b["present"]])
        win = [f for f in shown if c0 <= f["end_ns"] <= c1]
        ft = [(b["end_ns"] - a["end_ns"]) / 1e6 for a, b in zip(win, win[1:])]
        m = stats.frametime_metrics(ft)
        if m.get("frames", 0) < 10:
            raise RunError(f"only {m.get('frames', 0)} frames from layer {layer!r} in the capture window")
        if not poller:  # the frame timeline also says which frames were late or never shown
            in_win = [f for f in mine if c0 <= f["end_ns"] <= c1]
            dropped = sum(1 for f in in_win if f["present"] == "dropped")
            late = sum(1 for f in in_win if f["present"] == "late")
            m["dropped_frames"] = dropped
            m["late_frames_pct"] = round(100 * late / max(1, len(in_win) - dropped), 2)
            m["app_fps"] = round((m["frames"] + 1 + dropped) / m["capture_s"], 2)  # buffers queued, shown or not
        t0 = (c0 / 1e9) - smp.t0_boot
        m.update(summarize(window(rows, t0, t0 + m["capture_s"])))
        if m.get("system_w_avg") and m.get("avg_fps"):
            m["mj_per_frame"] = round(1000 * m["system_w_avg"] / m["avg_fps"], 2)
        smp.marks["capture_start"] = round(e0 / 1e9 - smp.t0_boot, 3)
        smp.marks["capture_end"] = round(e1 / 1e9 - smp.t0_boot, 3)
        ev = sorted({k for a in procs for k in EMU_KEYS if k in a.lower()})
        exe = next((a for a in procs if sc.get("process") and re.search(sc["process"], a)), None) or \
            next((a for a in procs if re.search(r"\.exe\b", a, re.I)), None)
        # the Wine/Proton build the app runs it with, from wineserver's path (GameNative, Winlator)
        pm = next((m for a in procs for m in [re.search(r"/(proton|wine)[-_]?([^/\s]+)/bin/wineserver", a)] if m), None)
        proton = {"name": f"{pm.group(1)}-{pm.group(2)}", "version": pm.group(2),
                  "arch": "arm64ec" if "arm64ec" in pm.group(2) else ("arm64" if "arm64" in pm.group(2) else "x86_64")} \
            if pm else None
        out = {"metrics": m, "marks": smp.marks, "frame_log": "frames.csv", "trace": trace.name,
               "frame_source": "surfaceflinger --latency" if poller else "perfetto frametimeline",
               "layer": layer, "layers_seen": seen, "trim_s": trim_s, "game_exe": exe, "proton": proton,
               "game_processes": procs[:30],
               "emulation": {"host_arch": "aarch64", "game_arch": "unknown",
                             "x86_emulated": True if {"box64", "box86", "fex", "wowbox64", "arm64ec"} & set(ev) else None,
                             "method": (" + ".join(e for e in ("box64", "box86", "wowbox64", "fex", "arm64ec", "wine")
                                                   if e in ev) or "unknown") + f" ({sc['package']})",
                             "evidence": ev}}
        if smp.gaps:
            warnings.append(f"sampler lost contact {smp.gaps} time(s)")
        if warnings:
            out["warning"] = "; ".join(warnings)
        return out


class AndroidSampler:
    """sampler.Sampler for an Android device: an sh loop on the device streams raw
    values over adb, turned into the same rows (and samples.csv) here."""

    def __init__(self, dev: Android, csv_path: Path, interval: float = 1.0, max_s: float = 7200):
        self.dev, self.csv_path, self.interval = dev, Path(csv_path), interval
        hw = dev.hw
        self.rows: list[dict] = []
        self.marks: dict[str, float] = {}
        self.gaps = 0
        self.t0_boot = dev.uptime()
        self._last_boot = self.t0_boot
        self.files: list[tuple[str, str]] = []   # (key, path), printed in this order
        for p in hw["policies"]:
            self.files.append((f"{p['name']}_mhz", f"{p['path']}/scaling_cur_freq"))
        if hw["gpu"]:
            self.files += [("gpu_mhz", f"{hw['gpu']}/gpuclk"), ("gpu_busy", f"{hw['gpu']}/gpu_busy_percentage")]
        for g, fs in hw["zones"].items():
            self.files += [(f"temp:{g}", f) for f in fs]
        self.files += [(f"cool:{n}", p) for n, p in hw["cooling"]]
        for n in ["battery"] + hw["supplies"]:
            self.files += [(f"ps:{n}:{k.upper()}", f"/sys/class/power_supply/{n}/{k}")
                           for k in ("voltage_now", "current_now", "capacity", "status", "online")]
        n_max = int(max_s / max(interval, 0.1))
        tag = uuid.uuid4().hex[:8]   # own files: a live readout can run next to a session
        self.sh_path, self.stop_path = f"{DIR}/sampler-{tag}.sh", f"{DIR}/sampler-{tag}.stop"
        self.script = (
            f"rm -f {self.stop_path}; n=0\n"
            f"while [ ! -e {self.stop_path} ] && [ $n -lt {n_max} ]; do\n"
            "  read up x < /proc/uptime; echo \"@ $up\"\n"
            "  while IFS= read -r l; do case \"$l\" in cpu*) echo \"$l\";; *) break;; esac; done < /proc/stat\n"
            "  while IFS= read -r l; do case \"$l\" in MemTotal:*|MemAvailable:*|SwapTotal:*) echo \"$l\";;"
            " SwapFree:*) echo \"$l\"; break;; esac; done < /proc/meminfo\n"
            "  for f in " + " ".join(shlex.quote(p) for _, p in self.files) + "; do\n"
            "    v=; IFS= read -r v < \"$f\" 2>/dev/null; echo \"= $v\"\n"
            "  done\n"
            "  echo .; n=$((n+1)); sleep " + str(interval) + "\n"
            "done\n")
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._p = None

    def start(self):
        self.dev.adb.push_text(self.script, self.sh_path)
        self._t.start()
        return self

    def mark(self, name: str) -> float:
        """A named point in time, as the sampler's t (s since start)."""
        return self.mark_boot(name, self._last_boot)

    def mark_boot(self, name: str, boot_s: float) -> float:
        self.marks[name] = round(boot_s - self.t0_boot, 3)
        return self.marks[name]

    def stop(self) -> list[dict]:
        self._stop.set()
        try:
            self.dev.adb.sh(f"touch {self.stop_path}")
        except AdbError:
            pass
        self._t.join(timeout=self.interval * 3 + 10)
        if self._p and self._p.poll() is None:
            self._p.kill()
        try:
            self.dev.adb.sh(f"rm -f {self.sh_path} {self.stop_path}")
        except AdbError:
            pass
        return self.rows

    def _loop(self):
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        prev_stat = None
        fields = None
        with open(self.csv_path, "w", newline="") as f:
            while not self._stop.is_set():
                try:
                    self._p = self.dev.adb.popen(f"sh {self.sh_path}")
                except AdbError:
                    self.gaps += 1
                    time.sleep(2)
                    continue
                block: list[str] = []
                for raw in self._p.stdout:
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    if line != ".":
                        block.append(line)
                        continue
                    row, prev_stat = self._row(block, prev_stat)
                    block = []
                    if row is None:
                        continue
                    self.rows.append(row)
                    if fields is None:
                        fields = list(row)
                        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                        w.writeheader()
                    w.writerow(row)
                    f.flush()
                self._p.wait()
                if not self._stop.is_set():  # adb dropped: reconnect and carry on
                    self.gaps += 1
                    time.sleep(1)

    def _row(self, block: list[str], prev_stat):
        if not block or not block[0].startswith("@ "):
            return None, prev_stat
        boot = float(block[0][2:])
        self._last_boot = boot
        stat, mem, vals = {}, {}, []
        for line in block[1:]:
            if line.startswith("cpu"):
                fl = line.split()
                nums = list(map(int, fl[1:]))
                stat[fl[0]] = (sum(nums[:8]), nums[3] + nums[4])
            elif line.startswith("= "):
                vals.append(line[2:].strip())
            elif line == "=":
                vals.append("")
            elif ":" in line:
                k, _, v = line.partition(":")
                mem[k] = _i(v)
        if len(vals) != len(self.files):
            return None, stat
        v = {k: x for (k, _), x in zip(self.files, vals)}
        row = {"t": round(boot - self.t0_boot, 3)}

        def load(keys):
            if not prev_stat:
                return None
            dt = sum(stat[k][0] - prev_stat[k][0] for k in keys if k in stat and k in prev_stat)
            di = sum(stat[k][1] - prev_stat[k][1] for k in keys if k in stat and k in prev_stat)
            return round(100 * (1 - di / dt), 1) if dt > 0 else None
        row["cpu_load"] = load(["cpu"])
        for p in self.dev.hw["policies"]:
            row[f"{p['name']}_load"] = load([f"cpu{c}" for c in p["cpus"]])
            row[f"{p['name']}_mhz"] = _i(v.get(f"{p['name']}_mhz")) // 1000
        if "gpu_mhz" in v:
            row["gpu_mhz"] = _i(v["gpu_mhz"]) // 1_000_000
            row["gpu_busy"] = float(_i(v["gpu_busy"], None)) if _i(v["gpu_busy"], None) is not None else None
        for g in self.dev.hw["zones"]:
            t = [_i(x, None) for (k, _), x in zip(self.files, vals) if k == f"temp:{g}"]
            t = [x for x in t if x is not None]
            row[f"{g}_temp_c"] = round(max(t) / 1000, 1) if t else None
        cool: dict[str, int] = {}
        for (k, _), x in zip(self.files, vals):
            if k.startswith("cool:"):
                cool[k[5:]] = max(cool.get(k[5:], 0), _i(x))
        row.update({f"cool_{n}": s for n, s in sorted(cool.items())})
        ps: dict[str, dict] = {}
        for (k, _), x in zip(self.files, vals):
            if k.startswith("ps:"):
                name, key = k[3:].rsplit(":", 1)
                ps.setdefault(name, {})[key] = x
        row.update(power_from(ps.get("battery", {}), [ps[n] for n in self.dev.hw["supplies"] if n in ps]))
        row["ram_used_mib"] = round((mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / 1024)
        row["swap_used_mib"] = round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024)
        return row, stat


class LatencyPoller:
    """Frames of one layer from SurfaceFlinger's frame log, `dumpsys SurfaceFlinger
    --latency LAYER` (one line per frame: desired present, actual present, frame
    ready; CLOCK_MONOTONIC ns). It keeps only the last 128 frames, so an sh loop on
    the device reads it every 0.4 s (128 frames last 1.07 s at 120 fps) and each
    present time is kept once here. The layer is the first one in `dumpsys
    SurfaceFlinger --list` matching `pattern` (grep -E), found again if it goes away."""

    def __init__(self, dev: Android, pattern: str, max_s: float = 3600):
        self.dev = dev
        self.presents: set[int] = set()
        self.layers: list[str] = []
        self.overruns = 0
        tag = uuid.uuid4().hex[:8]
        self.sh_path, self.stop_path, tmp = (f"{DIR}/frames-{tag}.sh", f"{DIR}/frames-{tag}.stop",
                                             f"{DIR}/frames-{tag}.txt")
        self.script = (
            f"PAT={shlex.quote(pattern)}; L=; n=0; rm -f {self.stop_path}\n"
            f"while [ ! -e {self.stop_path} ] && [ $n -lt {int(max_s / 0.4)} ]; do\n"
            "  n=$((n+1))\n"
            "  if [ -z \"$L\" ]; then\n"
            "    L=$(dumpsys SurfaceFlinger --list | grep -m1 -E \"$PAT\"); [ -n \"$L\" ] && echo \"L $L\"\n"
            "    sleep 0.5; continue\n"
            "  fi\n"
            f"  dumpsys SurfaceFlinger --latency \"$L\" > {tmp}; c=0\n"
            "  while read a b x; do [ -n \"$x\" ] || continue; c=$((c+1)); [ \"$b\" != 0 ] && echo \"F $b\"; done"
            f" < {tmp}\n"
            "  echo .; [ $c -eq 0 ] && L=\n"   # the layer went away: look for it again
            "  sleep 0.4\n"
            f"done\nrm -f {tmp}\n")
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._p = None

    def start(self):
        self.dev.adb.push_text(self.script, self.sh_path)
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self.dev.adb.sh(f"touch {self.stop_path}")
        except AdbError:
            pass
        self._t.join(timeout=10)
        if self._p and self._p.poll() is None:
            self._p.kill()
        try:
            self.dev.adb.sh(f"rm -f {self.sh_path} {self.stop_path}")
        except AdbError:
            pass

    def _loop(self):
        prev_max = None
        while not self._stop.is_set():
            try:
                self._p = self.dev.adb.popen(f"sh {self.sh_path}")
            except AdbError:
                time.sleep(2)
                continue
            block: list[int] = []
            for raw in self._p.stdout:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line.startswith("F "):
                    t = _i(line[2:], 0)
                    if 0 < t < 2**62:  # INT64_MAX: not presented yet
                        block.append(t)
                elif line.startswith("L "):
                    self.layers.append(line[2:].strip())
                    prev_max = None
                elif line == "." and block:
                    # every frame in the log is new: frames between two reads were lost
                    if prev_max is not None and min(block) > prev_max:
                        self.overruns += 1
                    self.presents.update(block)
                    prev_max = max(block)
                    block = []
            self._p.wait()
            if not self._stop.is_set():
                time.sleep(1)
