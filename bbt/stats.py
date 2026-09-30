"""Frame-time metrics and run aggregation.

Definitions (the same for every workload, so sessions stay comparable):
  avg_fps        frames / time = 1000 / mean frame time
  low_1pct_fps   1000 / mean of the slowest 1 % of frame times ("1 % low")
  low_01pct_fps  the same for the slowest 0.1 %
  ft_pNN_ms      frame-time percentiles (linear interpolation)
  hitches        frames longer than max(50 ms, 3 x median)
"""
from __future__ import annotations

import math
import statistics as st


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return math.nan
    k = (len(sorted_vals) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def frametime_metrics(ft_ms: list[float]) -> dict:
    ft = [x for x in ft_ms if x and x > 0]
    if len(ft) < 10:
        return {"frames": len(ft)}
    s = sorted(ft)
    total = sum(ft)

    def low(frac):
        n = max(1, int(round(len(s) * frac)))
        return 1000 / (sum(s[-n:]) / n)
    med = percentile(s, 50)
    return {
        "frames": len(ft),
        "capture_s": round(total / 1000, 2),
        "avg_fps": round(1000 * len(ft) / total, 2),
        "low_1pct_fps": round(low(0.01), 2),
        "low_01pct_fps": round(low(0.001), 2),
        "ft_avg_ms": round(total / len(ft), 3),
        "ft_p50_ms": round(med, 3),
        "ft_p90_ms": round(percentile(s, 90), 3),
        "ft_p95_ms": round(percentile(s, 95), 3),
        "ft_p99_ms": round(percentile(s, 99), 3),
        "ft_p999_ms": round(percentile(s, 99.9), 3),
        "ft_stdev_ms": round(st.pstdev(ft), 3),
        "hitches": sum(1 for x in ft if x > max(50.0, 3 * med)),
    }


def aggregate(runs: list[dict]) -> dict:
    """metric -> {n, mean, median, stdev, cv_pct, min, max} over measured runs."""
    keys = []
    for r in runs:
        for k, v in r.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in keys:
                keys.append(k)
    out = {}
    for k in keys:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
        if not vals:
            continue
        mean = st.fmean(vals)
        sd = st.stdev(vals) if len(vals) > 1 else 0.0
        out[k] = {"n": len(vals), "mean": round(mean, 3), "median": round(st.median(vals), 3),
                  "stdev": round(sd, 3), "cv_pct": round(100 * sd / mean, 2) if mean else None,
                  "min": min(vals), "max": max(vals)}
    return out


# Which way is "better" for a metric (used by compare and the report).
def direction(metric: str) -> int:
    m = metric.lower()
    if m.endswith("_fps") or "score" in m or m.endswith("fps_avg"):
        return 1
    if m.startswith("ft_") or m == "hitches" or "temp" in m or m.startswith("system_w") \
            or "per_frame" in m or m.endswith("_ms"):
        return -1
    return 0
