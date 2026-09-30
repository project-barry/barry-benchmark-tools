"""Matrix file (YAML) loading and validation."""
from __future__ import annotations

import copy
import re
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
    "cooldown": {              # between runs; ends at whichever comes first:
        "tolerance_c": 3.0,    #   CPU and GPU within this of the idle baseline
        "plateau_s": 15,       #   or temperatures levelled off: fell less than
        "plateau_c": 1.0,      #   plateau_c over the last plateau_s
        "min_s": 15,
        "max_s": 90,           #   or this long at most
        "start_spread_c": 5,   # flag a scenario whose runs started further apart than this
        "baseline_c": None,    # fixed CPU baseline instead of the one measured at start
        "baseline_gpu_c": None,
    },
}

KINDS = {"vkmark", "steam", "android"}


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
        if kind == "android" and not sc.get("package"):
            raise SystemExit(f"{name}: android scenarios need a package (the app that runs the game)")
        s = {"name": name, "kind": kind, "title": sc.get("title") or name,
             "runs": session["runs"], "warmup": session["warmup"], **sc}
        s["kind"] = kind
        scenarios.append(s)
    if not scenarios:
        raise SystemExit("matrix has no enabled scenarios")
    return {"session": session, "scenarios": scenarios, "source": label or str(path)}


def scenario_kinds(path: Path) -> list[str]:
    """The scenario kinds a matrix uses (which devices can run it). Falls back to
    reading the text when the file does not load (no PyYAML, or a matrix with errors)."""
    try:
        return sorted({s["kind"] for s in load(Path(path))["scenarios"]})
    except SystemExit:
        text = Path(path).read_text()
        kinds = set(re.findall(r"^\s*-?\s*kind:\s*([\w-]+)", text, re.M))
        if re.search(r"^\s*-?\s*appid:", text, re.M):
            kinds.add("steam")
        return sorted(kinds)
