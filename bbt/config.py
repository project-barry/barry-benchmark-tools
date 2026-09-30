"""Matrix file (YAML) loading and validation."""
from __future__ import annotations

import copy
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULTS = {
    "runs": 3,                 # measured runs per scenario (at least 3 for a real result)
    "warmup": 1,               # discarded runs before the measured ones
    "sample_interval_s": 1.0,
    "variance_cv_pct": 3.0,    # flag a scenario when avg FPS / score CV is above this
    "cooldown": {
        "tolerance_c": 3.0,    # wait until CPU and GPU are within this of the baseline
        "min_s": 20,
        "max_s": 600,
        "baseline_c": None,    # fixed CPU baseline instead of the one measured at start
        "baseline_gpu_c": None,
    },
}

KINDS = {"vkmark", "steam"}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path, label: str | None = None) -> dict:
    if yaml is None:
        raise SystemExit("PyYAML is missing (python3 -c 'import yaml' fails)")
    raw = yaml.safe_load(Path(path).read_text()) or {}
    session = _merge(DEFAULTS, raw.get("session") or {})
    scenarios = []
    names = set()
    for i, sc in enumerate(raw.get("scenarios") or []):
        if sc.get("enabled", True) is False:
            continue
        name = sc.get("name") or f"scenario{i + 1}"
        if name in names:
            raise SystemExit(f"duplicate scenario name: {name}")
        names.add(name)
        kind = sc.get("kind") or ("steam" if "appid" in sc else None)
        if kind not in KINDS:
            raise SystemExit(f"{name}: kind must be one of {sorted(KINDS)}")
        if kind == "steam" and not sc.get("appid"):
            raise SystemExit(f"{name}: steam scenarios need an appid")
        s = {"name": name, "kind": kind, "title": sc.get("title") or name,
             "runs": session["runs"], "warmup": session["warmup"], **sc}
        s["kind"] = kind
        scenarios.append(s)
    if not scenarios:
        raise SystemExit("matrix has no enabled scenarios")
    return {"session": session, "scenarios": scenarios, "source": label or str(path)}
