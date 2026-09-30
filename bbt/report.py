"""Markdown reports: bbt_<title>_<YYYYMMDDHHMM>.md, one per title in a session.

Plain CommonMark/GFM only (headings, lists, pipe tables, no HTML), with table
columns padded so the file reads well as plain text too.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .util import slug

KEY_METRICS = [
    ("vkmark_score", "vkmark score"),
    ("avg_fps", "Average FPS"),
    ("low_1pct_fps", "1% low FPS"),
    ("low_01pct_fps", "0.1% low FPS"),
    ("ft_p50_ms", "Frame time p50 (ms)"),
    ("ft_p95_ms", "Frame time p95 (ms)"),
    ("ft_p99_ms", "Frame time p99 (ms)"),
    ("ft_p999_ms", "Frame time p99.9 (ms)"),
    ("ft_stdev_ms", "Frame time stdev (ms)"),
    ("hitches", "Hitches (> 50 ms / 3x median)"),
    ("cpu_load_avg", "CPU load avg (%)"),
    ("policy0_mhz_avg", "CPU little clock avg (MHz)"),
    ("policy3_mhz_avg", "CPU mid clock avg (MHz)"),
    ("policy7_mhz_avg", "CPU prime clock avg (MHz)"),
    ("gpu_mhz_avg", "GPU clock avg (MHz)"),
    ("gpu_busy_avg", "GPU busy avg (%)"),
    ("mh_gpu_load_avg", "GPU load avg, MangoHud (%)"),
    ("cpu_temp_max_c", "CPU temp max (C)"),
    ("gpu_temp_max_c", "GPU temp max (C)"),
    ("mem_temp_max_c", "Memory temp max (C)"),
    ("system_w_avg", "System power avg (W)"),
    ("mj_per_frame", "Energy per frame (mJ)"),
    ("ram_used_mib_max", "RAM used max (MiB)"),
    ("swap_used_mib_max", "Swap used max (MiB)"),
    ("gpu_mem_mib_max", "GPU memory max (MiB)"),
    ("mh_vram_gib_avg", "VRAM avg, MangoHud (GiB)"),
]


def key_metrics(agg: dict) -> list[tuple[str, str]]:
    """KEY_METRICS plus workload-specific ones (vkmark scenes, the game's own
    benchmark numbers) in display order, limited to what agg has."""
    out = []
    for key, label in KEY_METRICS:
        if key in agg:
            out.append((key, label))
        if key == "vkmark_score":
            out += [(k, f"vkmark {k[7:-4]} (fps)") for k in agg if k.startswith("vkmark_") and k.endswith("_fps")]
        if key == "low_01pct_fps":
            out += [(k, f"Game-reported {k[5:].replace('_', ' ')}") for k in agg if k.startswith("game_")]
    return out


def fmt(v, nd=2) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.{nd}f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.0f}"
    return str(v)


def table(headers: list[str], rows: list[list], align: list[str] | None = None) -> str:
    """GFM pipe table with padded columns. align: 'l' or 'r' per column."""
    cells = [[str(h) for h in headers]] + [[fmt(c).replace("|", "\\|") for c in r] for r in rows]
    align = align or ["l"] * len(headers)
    w = [max(3, *(len(r[i]) for r in cells)) for i in range(len(headers))]

    def line(r):
        return "| " + " | ".join(c.rjust(w[i]) if align[i] == "r" else c.ljust(w[i]) for i, c in enumerate(r)) + " |"
    sep = "| " + " | ".join(("-" * (w[i] - 1) + ":") if align[i] == "r" else "-" * w[i] for i in range(len(w))) + " |"
    return "\n".join([line(cells[0]), sep] + [line(r) for r in cells[1:]]) + "\n"


def _power_text(src: str | None) -> str:
    return {
        "battery": "Battery (system draw = battery V x I)",
        "ac-estimate": "AC charger (system draw estimated = USB input - battery charge power)",
        "ac-unknown": "AC charger (no input power reading, system draw unknown)",
    }.get(src or "", src or "unknown")


def _conditions(summary: dict, meta: dict, results: list[dict]) -> list[list]:
    snap = meta["snapshot"]
    sysd = snap["system"]
    runs = [r for res in results for r in res["runs"] if r["status"] == "ok"]
    first = runs[0]["metrics"] if runs else {}
    last = runs[-1]["metrics"] if runs else {}
    pw = snap["power"]
    srcs = sorted({r["metrics"].get("power_source") for r in runs if r["metrics"].get("power_source")})
    rows = [
        ["Power source", "; ".join(_power_text(s) for s in srcs) if srcs else _power_text(pw["power_source"])],
        ["Battery", f"{first.get('battery_pct_start', pw['battery_pct'])}% at first run -> "
                    f"{last.get('battery_pct_end', pw['battery_pct'])}% at last run ({pw['battery_status']} at start)"],
        ["Idle baseline temps", f"CPU {meta['baseline_c'].get('cpu')} C, GPU {meta['baseline_c'].get('gpu')} C "
                                f"(runs start within +{meta['matrix']['session']['cooldown']['tolerance_c']} C)"],
        ["Device", f"{sysd['model']} ({sysd['soc']})"],
        ["OS", f"{sysd['os']['name']} {sysd['os']['version_id']} (build {sysd['os']['build_id']})"],
        ["Kernel", sysd["kernel"]["release"]],
        ["GPU driver", f"{sysd['gpu']['driver']} / {sysd['gpu']['driver_info']} (Vulkan {sysd['gpu']['vulkan_api']})"],
    ]
    for p in snap["cpufreq"]:
        cpus = p["cpus"]
        rng = f"cpu{cpus[0]}-{cpus[-1]}" if len(cpus) > 1 else f"cpu{cpus[0]}"
        cap = "" if (p["min_mhz"], p["max_mhz"]) == (p["hw_min_mhz"], p["hw_max_mhz"]) else \
            f" (hardware {p['hw_min_mhz']}-{p['hw_max_mhz']})"
        rows.append([f"CPU {p['policy']} ({rng})", f"{p['governor']}, {p['min_mhz']}-{p['max_mhz']} MHz{cap}"])
    for d in snap["devfreq"]:
        if "gpu" in d["name"]:
            rows.append(["GPU devfreq", f"{d['governor']}, {d['min_mhz']}-{d['max_mhz']} MHz, "
                                        f"polling {d['polling_interval_ms']} ms"])
    sch = snap["scheduler"]
    rows.append(["CPU scheduler", f"sched_ext {sch['sched_ext']}{' (' + sch['sched_ext_ops'] + ')' if sch['sched_ext_ops'] else ''}, "
                                  f"boostd {'on' if sch['boostd'] else 'off'}, cpuidle {snap['cpuidle']['governor']}"])
    mem = snap["memory"]
    zr = ", ".join(f"{k} {v['algorithm']} {v['disksize_mib']} MiB" for k, v in mem["zram"].items()) or "none"
    rows.append(["Memory", f"{mem['total']}, zram: {zr}, swappiness {mem['vm']['swappiness']}, THP {mem['thp']}"])
    gs = snap["gamescope"]
    if gs.get("running"):
        rows.append(["Gamescope", f"nested {gs['nested']}, output {gs['output']}, frame limit {gs['framerate_limit']}"])
    return rows


def _scenario_section(res: dict) -> str:
    cfg = res["config"]
    out = [f"### {res['name']}\n"]
    desc = [f"- Kind: {res['kind']}"]
    if res["kind"] == "steam":
        desc.append(f"- App: {cfg['appid']}, API: {cfg.get('api', 'not set')}, "
                    f"args: {' '.join(map(str, cfg.get('args') or [])) or 'none'}")
        if cfg.get("capture") == "until_exit":
            desc.append(f"- Capture: first frame until the game quits by itself, trimmed "
                        f"{cfg.get('trim_start_s', 0)} s at the start and {cfg.get('trim_end_s', 0)} s at the end")
        else:
            desc.append(f"- Capture: {cfg.get('duration_s', 60)} s, starting {cfg.get('settle_s', 30)} s after the first frame")
        for key, vals in (cfg.get("wine_registry") or {}).items():
            desc.append(f"- Game settings (registry {key}): " + ", ".join(f"{k}={v}" for k, v in vals.items()))
    else:
        v = cfg.get("vkmark", {})
        desc.append(f"- vkmark {v.get('winsys', 'headless')} {v.get('size', '1920x1080')}, "
                    f"scenes: {', '.join(v.get('benchmarks') or ['default set'])}")
    ok = [r for r in res["runs"] if r["status"] == "ok"]
    ref = next((r for r in ok if not r["warmup"]), ok[0] if ok else None)
    if ref and ref.get("proton"):
        p = ref["proton"]
        desc.append(f"- Proton: {p.get('name')} ({p.get('version', 'version unknown')}, {p.get('arch')})")
    if ref and ref.get("emulation"):
        e = ref["emulation"]
        emu = {True: "yes", False: "no", None: "unknown"}[e.get("x86_emulated")]
        desc.append(f"- x86 emulation: {emu} - game binary {e.get('game_arch')}, {e.get('method')}"
                    + (f"; seen: {', '.join(e['evidence'])}" if e.get("evidence") else ""))
    measured = [r for r in res["runs"] if not r["warmup"]]
    desc.append(f"- Runs: {len([r for r in measured if r['status'] == 'ok'])} of {len(measured)} measured OK, "
                f"{len([r for r in res['runs'] if r['warmup']])} warm-up discarded")
    out.append("\n".join(desc) + "\n")

    agg = res["aggregate"]
    rows = [[label, agg[key]["mean"], agg[key]["median"], agg[key]["stdev"], agg[key]["cv_pct"],
             agg[key]["min"], agg[key]["max"]] for key, label in key_metrics(agg)]
    if rows:
        out.append("Summary over measured runs:\n")
        out.append(table(["Metric", "Mean", "Median", "Stdev", "CV %", "Min", "Max"], rows,
                         ["l", "r", "r", "r", "r", "r", "r"]))
    # per-run table
    pm = res.get("primary_metric") or "avg_fps"
    if pm == "vkmark_score":
        scenes = [k for k in agg if k.startswith("vkmark_") and k.endswith("_fps")]
        cols = [pm] + scenes + ["gpu_mhz_avg", "gpu_temp_max_c", "system_w_avg"]
    else:
        cols = [pm] + [k for k in ("low_1pct_fps", "low_01pct_fps", "ft_p99_ms", "cpu_temp_max_c",
                                   "gpu_temp_max_c", "system_w_avg") if k != pm]
    labels = dict(key_metrics(agg)) | dict(KEY_METRICS)
    head = ["Run", "Status"] + [labels.get(c, c) for c in cols] + ["Cooldown (s)"]
    prow = []
    for r in res["runs"]:
        name = r["run"] + (" (discarded)" if r["warmup"] else "")
        prow.append([name, r["status"]] + [r["metrics"].get(c) for c in cols] + [r["cooldown"]["waited_s"]])
    out.append("\nPer run:\n")
    out.append(table(head, prow, ["l", "l"] + ["r"] * (len(head) - 2)))
    if res["flags"]:
        out.append("\nFlags:\n")
        out.append("\n".join(f"- {f}" for f in res["flags"]) + "\n")
    else:
        out.append("\nFlags: none\n")
    return "\n".join(out)


def render(title: str, summary: dict, meta: dict, results: list[dict]) -> str:
    started = dt.datetime.fromisoformat(summary["started"])
    ended = dt.datetime.fromisoformat(summary["ended"]) if summary.get("ended") else None
    lines = [
        f"# Barry Benchmark Tools report: {title}",
        "",
        f"- Session: {summary['session']}",
        f"- Tag: {summary['tag']}",
        f"- Started: {started:%Y-%m-%d %H:%M}" + (f", ended {ended:%H:%M}" if ended else ""),
        f"- Matrix: {meta['matrix']['source']}",
        f"- Scenarios: {', '.join(r['name'] for r in results)}",
        "",
        "## Test conditions",
        "",
        table(["Condition", "Value"], _conditions(summary, meta, results)),
        "## Results",
        "",
    ]
    lines += [_scenario_section(r) for r in results]
    lines += [
        "## Notes",
        "",
        "- Average FPS = frames / capture time. 1% and 0.1% lows = 1000 / mean of the",
        "  slowest 1% / 0.1% frame times. Frame-time percentiles are per frame",
        "  (MangoHud log_interval=0).",
        "- Clocks, load, temps and power come from the harness's own sysfs sampler",
        "  (1 s) over the capture window; mh_* values are MangoHud's own readings.",
        "- On AC power, system draw is an estimate; compare power only between runs",
        "  with the same power source.",
        "- Raw data: runs.csv and summary.json in this folder; per-run logs, samples",
        "  and system snapshots in the scenario folders.",
        "",
    ]
    return "\n".join(lines)


def write_reports(session_dir: Path, summary: dict, meta: dict) -> list[Path]:
    started = dt.datetime.fromisoformat(summary["started"])
    by_title: dict[str, list[dict]] = {}
    for res in summary["scenarios"]:
        by_title.setdefault(res["title"], []).append(res)
    out = []
    for title, results in by_title.items():
        p = session_dir / f"bbt_{slug(title)}_{started:%Y%m%d%H%M}.md"
        p.write_text(render(title, summary, meta, results))
        out.append(p)
    return out
