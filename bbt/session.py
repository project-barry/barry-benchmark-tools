"""One benchmark session: every scenario x (warm-up + runs), unattended.

Per run: cool down to the baseline -> snapshot -> run (launch, capture, kill)
-> metrics. Output (results/<YYYYMMDD-HHMM>_<tag>/):

  session.json          tag, matrix, host snapshot at start, baseline temps
  matrix.yaml           the matrix file as used
  runs.csv              one row per run (warm-ups included, marked), all metrics
  summary.json          per scenario: runs, aggregate stats, flags
  bbt_<title>_<YYYYMMDDHHMM>.md   human report, one per game/benchmark title
  <scenario>/<run>/     snapshot.json, samples.csv, raw logs, metrics.json
"""
from __future__ import annotations

import csv
import re
import datetime as dt
import fcntl
import shutil
import signal
import statistics as st
import time
import traceback
from pathlib import Path

from . import report, runners, steam, stats, sysinfo
from .sampler import thermal_zones
from .util import RESULTS, STATE, log, now_iso, rd_int, slug, write_json

PRIMARY = ("avg_fps", "vkmark_score")


def primary_metric(metrics: dict) -> str | None:
    for k in PRIMARY:
        if k in metrics:
            return k
    return None


def temps() -> dict:
    out = {}
    for g, files in thermal_zones().items():
        vals = [v for v in (rd_int(x) for x in files) if v is not None]
        if vals:
            out[g] = max(vals) / 1000
    return out


def measure_baseline(seconds: float = 10) -> dict:
    samples = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        samples.append(temps())
        time.sleep(1)
    return {g: round(st.median(s[g] for s in samples if g in s), 1) for g in ("cpu", "gpu") if any(g in s for s in samples)}


def cooldown(cfg: dict, baseline: dict) -> dict:
    """Wait for a comparable starting point, not for a cold device.

    Ends at the first of: CPU and GPU back within tolerance of the idle
    baseline; temperatures levelled off (both changed less than plateau_c
    over the last plateau_s, after min_s); max_s. Handhelds that idle warm (e.g.
    while charging) never get back to a cold-start baseline, so waiting for
    it only burns time; run-to-run start temperatures are checked instead
    (see flags_for)."""
    tol, lo, hi = cfg["tolerance_c"], cfg["min_s"], cfg["max_s"]
    win, drop = cfg.get("plateau_s", 15), cfg.get("plateau_c", 1.0)
    target = {"cpu": (cfg.get("baseline_c") or baseline.get("cpu", 45)) + tol,
              "gpu": (cfg.get("baseline_gpu_c") or baseline.get("gpu", 45)) + tol}
    t0 = time.monotonic()
    start = temps()
    hist: list[tuple[float, dict]] = []
    log(f"cooldown: cpu {start.get('cpu')} C, gpu {start.get('gpu')} C "
        f"(baseline+{tol}: {target['cpu']:.1f} / {target['gpu']:.1f} C, or level for {win} s, max {hi} s)")
    reason = "max_s"
    while True:
        waited = time.monotonic() - t0
        cur = temps()
        hist.append((waited, cur))
        if all(cur.get(g, 0) <= target[g] for g in target) and waited >= lo:
            reason = "baseline"
            break
        old = [h for h in hist if h[0] <= waited - win]
        if waited >= max(lo, win) and old:
            ref = old[-1][1]
            if all(abs(ref.get(g, 0) - cur.get(g, 0)) < drop for g in ("cpu", "gpu")):  # flat, not rising
                reason = "level"
                break
        if waited >= hi:
            break
        time.sleep(2)
    res = {"waited_s": round(waited, 1), "start_c": start, "end_c": cur, "target_c": target,
           "ended_by": reason, "timed_out": reason == "max_s"}
    log(f"cooldown done after {waited:.0f} s ({reason}) at cpu {cur.get('cpu')} / gpu {cur.get('gpu')} C")
    return res


def expected_seconds(sc: dict, sess: dict) -> float:
    """Rough session length for one scenario (for the estimate shown up front)."""
    if sc["kind"] == "vkmark":
        scene = sum(float(m.group(1)) for b in (sc.get("vkmark", {}).get("benchmarks") or [])
                    for m in [re.search(r"duration=(\d+(?:\.\d+)?)", b)] if m)
        per = 3 + (scene or 60)
    elif sc.get("capture") == "until_exit":
        per = float(sc.get("expected_s") or 120) + 25
    else:
        per = float(sc.get("settle_s", 30)) + float(sc.get("duration_s", 60)) + 25
    cool = min(float(sess["cooldown"]["max_s"]), 45.0)  # typical: levels off well before max_s
    return (sc["warmup"] + sc["runs"]) * (per + cool)


def flags_for(sc_result: dict, sess: dict) -> list[str]:
    runs = [r for r in sc_result["runs"] if not r["warmup"] and r["status"] == "ok"]
    fl = []
    want = sc_result["config"]["runs"]
    if len(runs) < max(3, want):
        fl.append(f"only {len(runs)} good measured run(s) (want >= {max(3, want)})")
    agg = sc_result["aggregate"]
    pm = sc_result.get("primary_metric")
    thr = sess["variance_cv_pct"]
    if pm and pm in agg and (agg[pm]["cv_pct"] or 0) > thr:
        fl.append(f"HIGH VARIANCE: {pm} CV {agg[pm]['cv_pct']}% > {thr}%")
    if "low_1pct_fps" in agg and (agg["low_1pct_fps"]["cv_pct"] or 0) > 3 * thr:
        fl.append(f"HIGH VARIANCE: low_1pct_fps CV {agg['low_1pct_fps']['cv_pct']}% > {3 * thr}%")
    if pm and pm in agg:
        med = agg[pm]["median"]
        for r in runs:
            v = r["metrics"].get(pm)
            if med and v is not None and abs(v - med) / med * 100 > 2 * thr:
                fl.append(f"outlier: {r['run']} {pm} {v} vs median {med}")
    for r in sc_result["runs"]:
        if r["warmup"]:
            continue
        if r["status"] != "ok":
            fl.append(f"{r['run']} failed: {r.get('error')}")
        if (r.get("cooldown") or {}).get("timed_out"):
            fl.append(f"{r['run']} started while still cooling (hit the {sess['cooldown']['max_s']} s cooldown limit)")
        if r["metrics"].get("throttled"):
            fl.append(f"{r['run']} thermal throttling: {r['metrics']['throttled']}")
        if r.get("warning"):
            fl.append(f"{r['run']}: {r['warning']}")
    starts = [((r.get("cooldown") or {}).get("end_c") or {}) for r in runs]
    if len(starts) > 1:
        for g in ("cpu", "gpu"):
            vals = [x.get(g) for x in starts if x.get(g) is not None]
            if vals and max(vals) - min(vals) > sess["cooldown"].get("start_spread_c", 5):
                fl.append(f"measured runs started at different {g.upper()} temperatures "
                          f"({min(vals):.0f}-{max(vals):.0f} C): heat may affect the comparison")
    srcs = {r["metrics"].get("power_source") for r in runs}
    if len(srcs) > 1:
        fl.append(f"power source changed between runs: {sorted(s for s in srcs if s)}")
    # frame cap: median FPS sitting on a refresh-rate multiple
    if "avg_fps" in agg and "ft_p50_ms" in agg:
        fps50 = 1000 / agg["ft_p50_ms"]["median"]
        for cap in (30, 40, 45, 60, 72, 90, 120):
            if abs(fps50 - cap) / cap < 0.02:
                fl.append(f"median frame rate ~{cap} fps: likely capped (vsync / frame limit), GPU/CPU changes may not show")
                break
    return fl


class Session:
    def __init__(self, matrix: dict, tag: str, matrix_path: Path, device: str | None = None):
        self.m = matrix
        self.tag = tag
        self.device = device
        self.start = dt.datetime.now()
        # the device id is part of the name, so sessions from several devices never collide
        base = f"{self.start:%Y%m%d-%H%M}_{slug(tag)}" + (f"_{slug(device)}" if device else "")
        self.dir = RESULTS / base
        n = 2
        while self.dir.exists():
            self.dir = RESULTS / f"{base}-{n}"
            n += 1
        self.matrix_path = matrix_path
        self.results: list[dict] = []
        self.stop_requested = False

    def _lock(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self._lockf = open(STATE / "bench.lock", "w")
        try:
            fcntl.flock(self._lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another `bench run` is active")

    def run(self) -> Path:
        self._lock()
        self.dir.mkdir(parents=True)
        shutil.copy2(self.matrix_path, self.dir / "matrix.yaml")
        sess = self.m["session"]
        est = sum(expected_seconds(sc, sess) for sc in self.m["scenarios"]) + 15
        log(f"session {self.dir.name}: {len(self.m['scenarios'])} scenario(s), about {est / 60:.0f} min")
        snap = sysinfo.snapshot()
        log("measuring idle baseline temps (10 s)")
        baseline = measure_baseline()
        log(f"baseline: {baseline}")
        self.meta = {"tag": self.tag, "device": self.device, "session": self.dir.name, "started": now_iso(),
                     "matrix": self.m, "baseline_c": baseline, "snapshot": snap}
        write_json(self.dir / "session.json", self.meta)

        def on_sig(signum, frame):
            self.stop_requested = True
            raise KeyboardInterrupt
        old = signal.signal(signal.SIGTERM, on_sig)
        try:
            for sc in self.m["scenarios"]:
                if self.stop_requested:
                    break
                self._scenario(sc, sess, baseline)
        except KeyboardInterrupt:
            log("interrupted: writing what was measured so far")
        finally:
            signal.signal(signal.SIGTERM, old)
            self.meta["ended"] = now_iso()
            self.meta["interrupted"] = self.stop_requested
            write_json(self.dir / "session.json", self.meta)
            self._write_outputs()
        return self.dir

    def _scenario(self, sc: dict, sess: dict, baseline: dict):
        runner = runners.RUNNERS[sc["kind"]]
        res = {"name": sc["name"], "title": sc["title"], "kind": sc["kind"], "config": sc, "runs": []}
        self.results.append(res)
        total = sc["warmup"] + sc["runs"]
        for i in range(total):
            warm = i < sc["warmup"]
            label = f"warmup{i + 1}" if warm else f"run{i - sc['warmup'] + 1}"
            run_dir = self.dir / slug(sc["name"]) / label
            run_dir.mkdir(parents=True)
            log(f"== {sc['name']} {label} ({i + 1}/{total})")
            cd = cooldown(sess["cooldown"], baseline)
            rec = {"scenario": sc["name"], "run": label, "warmup": warm, "cooldown": cd,
                   "started": now_iso(), "metrics": {}}
            snap = sysinfo.dynamic()
            write_json(run_dir / "snapshot.json", {"static": sysinfo._static(), **snap})
            try:
                out = runner(sc, run_dir, sess["sample_interval_s"])
                rec.update(out)
                rec["status"] = "ok"
            except KeyboardInterrupt:
                rec.update(status="interrupted", error="interrupted")
                self._finish_run(rec, run_dir, res)
                raise
            except Exception as e:  # keep going: one bad run should not end the session
                rec.update(status="failed", error=str(e))
                (run_dir / "error.txt").write_text(traceback.format_exc())
                log(f"run failed: {e}")
                if sc["kind"] == "steam":
                    runners._clear_run_env()
                    steam.kill(str(sc["appid"]))
            self._finish_run(rec, run_dir, res)
            pm = primary_metric(rec["metrics"])
            if pm:
                extra = f", 1% low {rec['metrics']['low_1pct_fps']}" if "low_1pct_fps" in rec["metrics"] else ""
                log(f"{label}: {pm} {rec['metrics'][pm]}{extra}")
        measured = [r["metrics"] for r in res["runs"] if not r["warmup"] and r["status"] == "ok"]
        res["aggregate"] = stats.aggregate(measured)
        res["primary_metric"] = next((primary_metric(m) for m in measured if primary_metric(m)), None)
        res["flags"] = flags_for(res, sess)
        for f in res["flags"]:
            log(f"FLAG {sc['name']}: {f}")

    def _finish_run(self, rec, run_dir, res):
        rec["ended"] = now_iso()
        rec["snapshot"] = str((run_dir / "snapshot.json").relative_to(self.dir))
        write_json(run_dir / "metrics.json", rec)
        res["runs"].append(rec)

    def _write_outputs(self):
        for res in self.results:  # scenarios cut short by an interrupt
            if "aggregate" not in res:
                measured = [r["metrics"] for r in res["runs"] if not r["warmup"] and r["status"] == "ok"]
                res["aggregate"] = stats.aggregate(measured)
                res["primary_metric"] = next((primary_metric(m) for m in measured if primary_metric(m)), None)
                res["flags"] = flags_for(res, self.m["session"]) + ["scenario interrupted"]
        summary = {"tag": self.tag, "device": self.device, "session": self.dir.name, "started": self.meta["started"],
                   "ended": self.meta.get("ended"), "baseline_c": self.meta["baseline_c"],
                   "scenarios": self.results}
        write_json(self.dir / "summary.json", summary)
        # raw CSV: one row per run
        cols = ["scenario", "title", "run", "warmup", "status", "started", "ended", "error",
                "proton", "proton_version", "game_arch", "x86_emulated", "emulation",
                "cooldown_waited_s", "cooldown_ended_by", "cooldown_timed_out"]
        mcols = []
        for res in self.results:
            for r in res["runs"]:
                for k, v in r["metrics"].items():
                    if k not in mcols and not isinstance(v, (dict, list)):
                        mcols.append(k)
        with open(self.dir / "runs.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols + mcols, extrasaction="ignore")
            w.writeheader()
            for res in self.results:
                for r in res["runs"]:
                    emu = r.get("emulation") or {}
                    row = {"scenario": res["name"], "title": res["title"], "run": r["run"],
                           "warmup": r["warmup"], "status": r["status"], "started": r["started"],
                           "ended": r.get("ended"), "error": r.get("error", ""),
                           "proton": (r.get("proton") or {}).get("name", ""),
                           "proton_version": (r.get("proton") or {}).get("version", ""),
                           "game_arch": emu.get("game_arch", ""), "x86_emulated": emu.get("x86_emulated", ""),
                           "emulation": emu.get("method", ""),
                           "cooldown_waited_s": r["cooldown"]["waited_s"],
                           "cooldown_ended_by": r["cooldown"].get("ended_by", ""),
                           "cooldown_timed_out": r["cooldown"]["timed_out"],
                           **{k: v for k, v in r["metrics"].items() if not isinstance(v, (dict, list))}}
                    w.writerow(row)
        for path in report.write_reports(self.dir, summary, self.meta):
            log(f"report: {path}")
        log(f"results: {self.dir}")
