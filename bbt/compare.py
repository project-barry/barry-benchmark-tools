"""`bench compare A B`: % change per scenario and metric, plus what changed in
the system config between the two sessions."""
from __future__ import annotations

from pathlib import Path

from .report import key_metrics, table
from .stats import direction
from . import util
from .util import read_json, slug

IGNORE_CONFIG = ("temps_c", "power", "cooling", "gamescope.args", "system.hostname")


def resolve(ref: str) -> Path:
    """A session dir, a session name, or a tag (latest session with it)."""
    p = Path(ref)
    if (p / "summary.json").exists():
        return p
    if (util.RESULTS / ref / "summary.json").exists():
        return util.RESULTS / ref
    tagged = []
    for d in util.RESULTS.glob("*/summary.json"):
        try:
            if read_json(d)["tag"] == ref or d.parent.name.split("_", 1)[-1] == slug(ref):
                tagged.append(d.parent)
        except (KeyError, ValueError):
            continue
    if not tagged:
        raise SystemExit(f"no session found for '{ref}' in {util.RESULTS} (see `bench list`)")
    return sorted(tagged)[-1]


def _flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        for i, v in enumerate(obj):
            key = v.get("policy") or v.get("name") or str(i)
            out.update(_flatten({kk: vv for kk, vv in v.items() if kk not in ("policy", "name")}, f"{prefix}{key}."))
    else:
        out[prefix[:-1]] = obj
    return out


def config_diff(a: dict, b: dict) -> list[list]:
    fa, fb = _flatten(a), _flatten(b)
    rows = []
    for k in sorted(set(fa) | set(fb)):
        if any(k == i or k.startswith(i + ".") for i in IGNORE_CONFIG):
            continue
        if fa.get(k) != fb.get(k):
            rows.append([k, fa.get(k, "-"), fb.get(k, "-")])
    return rows


def _run_tools(summary: dict) -> dict:
    """scenario -> 'proton name version' from the first good run."""
    out = {}
    for res in summary["scenarios"]:
        for r in res["runs"]:
            if r["status"] == "ok" and r.get("proton"):
                out[res["name"]] = f"{r['proton'].get('name')} {r['proton'].get('version', '')}".strip()
                break
    return out


def compare(ref_a: str, ref_b: str, all_metrics: bool = False) -> str:
    da, db = resolve(ref_a), resolve(ref_b)
    sa, sb = read_json(da / "summary.json"), read_json(db / "summary.json")
    ma, mb = read_json(da / "session.json"), read_json(db / "session.json")
    out = [f"# Compare: {sa['tag']} -> {sb['tag']}", "",
           f"- A: {da.name}", f"- B: {db.name}",
           "- Change = (B - A) / A. 'better'/'worse' by metric direction; '~ noise' when",
           "  the change is smaller than the larger run-to-run CV of the two sessions.", ""]
    rows_b = {r["name"]: r for r in sb["scenarios"]}
    ta, tb = _run_tools(sa), _run_tools(sb)
    for res_a in sa["scenarios"]:
        res_b = rows_b.get(res_a["name"])
        if not res_b:
            out.append(f"## {res_a['name']}\n\nOnly in A.\n")
            continue
        aa, ab = res_a["aggregate"], res_b["aggregate"]
        km = [(k, l) for k, l in key_metrics(aa) if k in ab]
        labels = dict(km)
        keys = [k for k, _ in km]
        if all_metrics:
            keys += [k for k in aa if k in ab and k not in keys]
        rows = []
        for k in keys:
            a, b = aa[k]["mean"], ab[k]["mean"]
            if not a:
                continue
            pct = 100 * (b - a) / abs(a)
            noise = max(aa[k].get("cv_pct") or 0, ab[k].get("cv_pct") or 0)
            d = direction(k)
            if abs(pct) <= noise or abs(pct) < 0.5:
                verdict = "~ noise"
            elif d == 0:
                verdict = ""
            else:
                verdict = "better" if pct * d > 0 else "worse"
            rows.append([labels.get(k, k), a, b, f"{pct:+.1f}%", verdict,
                         f"{aa[k]['n']}/{ab[k]['n']}"])
        out.append(f"## {res_a['name']} ({res_a['title']})\n")
        if ta.get(res_a["name"]) != tb.get(res_b["name"]):
            out.append(f"Proton differs: A {ta.get(res_a['name'])}, B {tb.get(res_b['name'])}\n")
        out.append(table(["Metric", "A mean", "B mean", "Change", "", "Runs A/B"], rows,
                         ["l", "r", "r", "r", "l", "r"]) if rows else "No common metrics.\n")
        fl = [f"A: {f}" for f in res_a["flags"]] + [f"B: {f}" for f in res_b["flags"]]
        if fl:
            out.append("\nFlags:\n\n" + "\n".join(f"- {f}" for f in fl) + "\n")
        out.append("")
    for name in rows_b:
        if name not in {r["name"] for r in sa["scenarios"]}:
            out.append(f"## {name}\n\nOnly in B.\n")
    diff = config_diff(ma["snapshot"], mb["snapshot"])
    out.append("## System config differences (session start)\n")
    out.append(table(["Setting", "A", "B"], diff) if diff else "None: both sessions ran with the same settings.\n")
    return "\n".join(out)
