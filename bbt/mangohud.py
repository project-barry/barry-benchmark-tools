"""MangoHud logging: per-run config file and CSV log parsing.

MangoHud's log CSV is: a system-info header line, its values, then the
per-sample column header (fps,frametime,cpu_load,...,elapsed) and rows. With
log_interval=0 every frame is one row, so `frametime` is the exact series.
Columns are looked up by name because they differ between MangoHud versions.
"""
from __future__ import annotations

import csv
from pathlib import Path


def write_config(path: Path, out_dir: Path, delay_s: float, duration_s: float) -> None:
    """Invisible HUD that logs every frame for duration_s, starting after delay_s.

    Not `no_display`: that also turns logging off. A fully transparent HUD
    with a tiny font costs the same in every run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "alpha=0", "background_alpha=0", "font_size=8", "position=top-left",
        f"output_folder={out_dir}",
        f"autostart_log={max(1, int(round(delay_s)))}",
        f"log_duration={int(round(duration_s))}",  # 0 = until the game exits
        "log_interval=0",
        # metrics MangoHud should compute so they land in the log
        "fps", "frametime", "frame_timing=0",
        "cpu_stats", "cpu_temp", "cpu_power", "cpu_mhz",
        "gpu_stats", "gpu_temp", "gpu_core_clock", "gpu_mem_clock", "gpu_power",
        "vram", "ram", "swap", "procmem",
        "",
    ]))


def find_log(out_dir: Path) -> Path | None:
    logs = [p for p in Path(out_dir).glob("*.csv") if not p.name.endswith("_summary.csv")]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def parse(path: Path) -> dict:
    """{'sysinfo': {...}, 'columns': [...], 'rows': [ {col: float} ]}"""
    with open(path, newline="") as f:
        lines = list(csv.reader(f))
    sysinfo, start = {}, 0
    for i, row in enumerate(lines):
        if row and row[0].strip() in ("fps", "frametime") and "frametime" in [c.strip() for c in row]:
            start = i
            break
    if start >= 2:
        sysinfo = dict(zip([c.strip() for c in lines[start - 2]], [c.strip() for c in lines[start - 1]]))
    cols = [c.strip() for c in lines[start]] if lines else []
    rows = []
    for raw in lines[start + 1:]:
        if len(raw) != len(cols):
            continue
        r = {}
        for c, v in zip(cols, raw):
            try:
                r[c] = float(v)
            except ValueError:
                pass
        rows.append(r)
    return {"sysinfo": sysinfo, "columns": cols, "rows": rows}


def metrics(parsed: dict) -> dict:
    """Averages of MangoHud's own sensor columns (prefixed mh_)."""
    rows = parsed["rows"]
    out = {}
    for col, name in (("cpu_load", "mh_cpu_load_avg"), ("gpu_load", "mh_gpu_load_avg"),
                      ("gpu_core_clock", "mh_gpu_mhz_avg"), ("gpu_mem_clock", "mh_gpu_mem_mhz_avg"),
                      ("cpu_temp", "mh_cpu_temp_avg_c"), ("gpu_temp", "mh_gpu_temp_avg_c"),
                      ("cpu_power", "mh_cpu_w_avg"), ("gpu_power", "mh_gpu_w_avg"),
                      ("gpu_vram_used", "mh_vram_gib_avg"), ("ram_used", "mh_ram_gib_avg"),
                      ("process_rss", "mh_process_rss_gib_avg"), ("cpu_mhz", "mh_cpu_mhz_avg")):
        vals = [r[col] for r in rows if col in r]
        if vals and any(vals):
            out[name] = round(sum(vals) / len(vals), 2)
    return out
