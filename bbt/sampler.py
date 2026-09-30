"""Background sysfs sampler: clocks, load, temps, power, memory, throttling.

Runs next to every benchmark (MangoHud or not), so each run has the same set
of hardware numbers whatever the workload. Everything here is read-only.

Power: on battery the system draw is the battery's V*I (power_now on this
PMIC is not usable). On a charger it is estimated as USB input minus what
goes into the battery; `power_source` in each row says which one applies.
"""
from __future__ import annotations

import csv
import glob
import os
import threading
import time
from pathlib import Path

from .util import proc_tree, rd, rd_int

PS = "/sys/class/power_supply"


def cpu_policies() -> list[dict]:
    out = []
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*"), key=lambda s: int(s.rsplit("policy", 1)[1])):
        cpus = [int(c) for c in rd(f"{p}/related_cpus").split()]
        out.append({"name": os.path.basename(p), "path": p, "cpus": cpus})
    return out


def gpu_devfreq() -> str | None:
    for d in glob.glob("/sys/class/devfreq/*"):
        if "gpu" in os.path.basename(d) or "kgsl" in d:
            return d
    return None


def zone_group(t: str) -> str | None:
    """Thermal zone type -> group (cpu, gpu, mem, battery) or None."""
    if t.startswith(("cpu", "cpuss")):
        return "cpu"
    if t.startswith("gpu"):
        return "gpu"
    if t.startswith("mem") or t == "ddr":
        return "mem"
    if t == "battery":
        return "battery"
    return None


def thermal_zones() -> dict[str, str]:
    """group -> list of temp files; groups: cpu, gpu, mem, battery."""
    groups: dict[str, list[str]] = {}
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        g = zone_group(rd(f"{z}/type"))
        if g:
            groups.setdefault(g, []).append(f"{z}/temp")
    return groups


def cooling_devices() -> list[tuple[str, str]]:
    out = []
    for c in glob.glob("/sys/class/thermal/cooling_device*"):
        t = rd(f"{c}/type")
        if t.startswith(("cpufreq", "devfreq")):
            out.append((t, f"{c}/cur_state"))
    return sorted(out)


def fan_input() -> str | None:
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if os.path.exists(f"{h}/fan1_input"):
            return f"{h}/fan1_input"
    return None


def _uevent(name: str) -> dict:
    out = {}
    for line in rd(f"{PS}/{name}/uevent").splitlines():
        k, _, v = line.partition("=")
        out[k.replace("POWER_SUPPLY_", "")] = v
    return out


def power_state() -> dict:
    """Instantaneous power source + estimated system draw (W)."""
    names = [n for n in (os.listdir(PS) if os.path.isdir(PS) else []) if n != "battery"]
    return power_from(_uevent("battery"), [_uevent(n) for n in names])


def power_from(bat: dict, supplies: list[dict]) -> dict:
    """power_state() from uevent-style dicts (VOLTAGE_NOW, CURRENT_NOW, ONLINE,
    STATUS, CAPACITY): the battery and the other power supplies (USB, charger)."""
    v = int(bat.get("VOLTAGE_NOW", 0) or 0) / 1e6
    i = int(bat.get("CURRENT_NOW", 0) or 0) / 1e6   # + = charging, - = discharging
    ac_in_w, ac = 0.0, False
    for u in supplies:
        if u.get("ONLINE") == "1":
            ac = True
            vin = int(u.get("VOLTAGE_NOW", 0) or 0) / 1e6
            iin = int(u.get("CURRENT_NOW", 0) or 0) / 1e6
            if vin > 0 and iin > 0:
                ac_in_w = max(ac_in_w, vin * iin)
    batt_w = v * i
    if ac:
        sys_w = ac_in_w - batt_w if ac_in_w > 0 else None
        src = "ac-estimate" if sys_w is not None else "ac-unknown"
    else:
        sys_w, src = -batt_w, "battery"
    return {"power_source": src, "ac_online": ac, "battery_status": bat.get("STATUS", ""),
            "battery_pct": int(bat.get("CAPACITY", 0) or 0), "battery_w": round(batt_w, 3),
            "ac_in_w": round(ac_in_w, 3), "system_w": None if sys_w is None else round(sys_w, 3)}


def _proc_stat() -> dict[str, tuple[int, int]]:
    out = {}
    for line in rd("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        f = line.split()
        vals = list(map(int, f[1:]))
        idle = vals[3] + vals[4]
        out[f[0]] = (sum(vals[:8]), idle)
    return out


def _meminfo() -> dict[str, int]:
    out = {}
    for line in rd("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        out[k] = int(v.split()[0]) if v.split() else 0
    return out


def _gpu_fdinfo(pids: list[int]) -> tuple[int, int]:
    """(engine ns summed over unique DRM clients, resident KiB) for these pids."""
    seen, ns, res = set(), 0, 0
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fdinfo")
        except OSError:
            continue
        for fd in fds:
            txt = rd(f"/proc/{pid}/fdinfo/{fd}")
            if "drm-driver:" not in txt:
                continue
            kv = dict(l.split(":", 1) for l in txt.splitlines() if ":" in l)
            cid = kv.get("drm-client-id", "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            ns += int(kv.get("drm-engine-gpu", "0 ns").split()[0] or 0)
            r = kv.get("drm-resident-memory", kv.get("drm-total-memory", "0")).split()
            if r:
                res += int(r[0]) * (1024 if len(r) > 1 and r[1] == "MiB" else 1)
    return ns, res


class Sampler:
    """Samples every `interval` s into memory and a CSV file until stop()."""

    def __init__(self, csv_path: Path, interval: float = 1.0, root_pid_fn=None):
        self.csv_path = Path(csv_path)
        self.interval = interval
        self.root_pid_fn = root_pid_fn  # () -> pid of the workload, or None
        self.rows: list[dict] = []
        self.marks: dict[str, float] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self.policies = cpu_policies()
        self.gpu = gpu_devfreq()
        self.zones = thermal_zones()
        self.cooling = cooling_devices()
        self.fan = fan_input()
        self.t0 = time.monotonic()

    def start(self):
        self._t.start()
        return self

    def mark(self, name: str) -> float:
        """Record a named point in time (s since start), e.g. capture_start."""
        self.marks[name] = round(time.monotonic() - self.t0, 3)
        return self.marks[name]

    def stop(self) -> list[dict]:
        self._stop.set()
        self._t.join(timeout=10)
        return self.rows

    def _loop(self):
        prev_stat, prev_gpu, prev_t = _proc_stat(), None, time.monotonic()
        fields = None
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv_path, "w", newline="") as f:
            while not self._stop.wait(self.interval):
                now = time.monotonic()
                row = {"t": round(now - self.t0, 3)}
                # CPU load (all + per cluster) and clocks
                st = _proc_stat()
                def load(keys):
                    dt = sum(st[k][0] - prev_stat[k][0] for k in keys if k in st and k in prev_stat)
                    di = sum(st[k][1] - prev_stat[k][1] for k in keys if k in st and k in prev_stat)
                    return round(100 * (1 - di / dt), 1) if dt > 0 else None
                row["cpu_load"] = load(["cpu"])
                for p in self.policies:
                    row[f"{p['name']}_load"] = load([f"cpu{c}" for c in p["cpus"]])
                    row[f"{p['name']}_mhz"] = (rd_int(f"{p['path']}/scaling_cur_freq", 0) or 0) // 1000
                prev_stat = st
                # GPU clock + busy (fdinfo of the workload's processes)
                if self.gpu:
                    row["gpu_mhz"] = (rd_int(f"{self.gpu}/cur_freq", 0) or 0) // 1_000_000
                root = self.root_pid_fn() if self.root_pid_fn else None
                if root:
                    ns, res = _gpu_fdinfo(proc_tree(root))
                    if prev_gpu is not None and ns >= prev_gpu:
                        row["gpu_busy"] = round(min(100.0, 100 * (ns - prev_gpu) / 1e9 / (now - prev_t)), 1)
                    row["gpu_mem_mib"] = round(res / 1024, 1)
                    prev_gpu = ns
                else:
                    prev_gpu = None
                prev_t = now
                # temps (max per group)
                for g, files in self.zones.items():
                    vals = [v for v in (rd_int(x) for x in files) if v is not None]
                    row[f"{g}_temp_c"] = round(max(vals) / 1000, 1) if vals else None
                if self.fan:
                    row["fan_rpm"] = rd_int(self.fan)
                # throttling: highest cooling state per device
                for name, path in self.cooling:
                    row[f"cool_{name}"] = rd_int(path, 0)
                # power + memory
                row.update(power_state())
                mi = _meminfo()
                row["ram_used_mib"] = round((mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)) / 1024)
                row["swap_used_mib"] = round((mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) / 1024)
                self.rows.append(row)
                if fields is None:
                    fields = list(row)
                    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    w.writeheader()
                w.writerow(row)
                f.flush()


def window(rows: list[dict], t0: float | None, t1: float | None) -> list[dict]:
    return [r for r in rows if (t0 is None or r["t"] >= t0) and (t1 is None or r["t"] <= t1)]


def summarize(rows: list[dict]) -> dict:
    """Per-run hardware metrics over the capture window."""
    if not rows:
        return {}
    def vals(k):
        return [r[k] for r in rows if r.get(k) is not None]
    def avg(k):
        v = vals(k)
        return round(sum(v) / len(v), 2) if v else None
    def mx(k):
        v = vals(k)
        return max(v) if v else None
    out = {"cpu_load_avg": avg("cpu_load")}
    for k in rows[0]:
        if k.startswith("policy") and (k.endswith("_mhz") or k.endswith("_load")):
            out[f"{k}_avg"] = avg(k)
    out.update({
        "gpu_mhz_avg": avg("gpu_mhz"), "gpu_busy_avg": avg("gpu_busy"), "gpu_mem_mib_max": mx("gpu_mem_mib"),
        "cpu_temp_max_c": mx("cpu_temp_c"), "gpu_temp_max_c": mx("gpu_temp_c"),
        "mem_temp_max_c": mx("mem_temp_c"), "battery_temp_max_c": mx("battery_temp_c"),
        "cpu_temp_avg_c": avg("cpu_temp_c"), "gpu_temp_avg_c": avg("gpu_temp_c"),
        "fan_rpm_avg": avg("fan_rpm"),
        "system_w_avg": avg("system_w"),
        "ram_used_mib_max": mx("ram_used_mib"), "swap_used_mib_max": mx("swap_used_mib"),
    })
    srcs = sorted({r.get("power_source") for r in rows})
    out["power_source"] = srcs[0] if len(srcs) == 1 else "mixed:" + ",".join(srcs)
    out["battery_pct_start"] = rows[0].get("battery_pct")
    out["battery_pct_end"] = rows[-1].get("battery_pct")
    thr = {k[5:]: mx(k) for k in rows[0] if k.startswith("cool_") and (mx(k) or 0) > 0}
    out["throttled"] = thr or None
    return {k: v for k, v in out.items() if v is not None or k == "throttled"}
