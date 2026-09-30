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
    tag, _, dev = ref.partition("@")   # "baseline" or "baseline@thor"
    tagged = []
    for d in util.RESULTS.glob("*/summary.json"):
        try:
            j = read_json(d)
        except ValueError:
            continue
        if j.get("tag") != tag:
            continue
        if dev and j.get("device") != dev:
            continue
        tagged.append(d.parent)
    if not tagged:
        raise SystemExit(f"no session found for '{ref}' in {util.RESULTS} (see `bench list`; "
                         "TAG@DEVICE picks one device's)")
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


def _device(summary: dict, meta: dict) -> str:
    sysd = (meta.get("snapshot") or {}).get("system") or {}
    return summary.get("device") or sysd.get("model") or "unknown device"


def compare_data(ref_a: str, ref_b: str, all_metrics: bool = False) -> dict:
    """Structured comparison (used by `bench compare` and the web app)."""
    da, db = resolve(ref_a), resolve(ref_b)
    sa, sb = read_json(da / "summary.json"), read_json(db / "summary.json")
    ma, mb = read_json(da / "session.json"), read_json(db / "session.json")
    rows_b = {r["name"]: r for r in sb["scenarios"]}
    names_a = {r["name"] for r in sa["scenarios"]}
    # scenarios pair by name; one left over on each side with the same title pairs too
    # (the same game as a Steam scenario on SteamOS and an android one on Android)
    pair = {n: n for n in names_a if n in rows_b}
    left_b = [r for r in sb["scenarios"] if r["name"] not in names_a]
    for res_a in sa["scenarios"]:
        if res_a["name"] in pair:
            continue
        same_a = [r for r in sa["scenarios"] if r["title"] == res_a["title"] and r["name"] not in pair]
        same_b = [r for r in left_b if r["title"] == res_a["title"]]
        if len(same_a) == 1 and len(same_b) == 1:
            pair[res_a["name"]] = same_b[0]["name"]
    paired_b = set(pair.values())
    ta, tb = _run_tools(sa), _run_tools(sb)
    scenarios = []
    for res_a in sa["scenarios"]:
        res_b = rows_b.get(pair.get(res_a["name"], ""))
        name = res_a["name"] if not res_b or res_b["name"] == res_a["name"] else f"{res_a['name']} vs {res_b['name']}"
        sc = {"name": name, "title": res_a["title"], "only": None if res_b else "a",
              "tool_a": ta.get(res_a["name"]), "tool_b": tb.get(res_b["name"]) if res_b else None,
              "flags_a": res_a["flags"], "flags_b": res_b["flags"] if res_b else [], "metrics": []}
        scenarios.append(sc)
        if not res_b:
            continue
        aa, ab = res_a["aggregate"], res_b["aggregate"]
        km = [(k, l) for k, l in key_metrics(aa) if k in ab]
        if all_metrics:
            km += [(k, k) for k in aa if k in ab and k not in dict(km)]
        for k, label in km:
            a, b = aa[k]["mean"], ab[k]["mean"]
            if not a:
                continue
            pct = 100 * (b - a) / abs(a)
            noise = max(aa[k].get("cv_pct") or 0, ab[k].get("cv_pct") or 0)
            d = direction(k)
            if abs(pct) <= noise or abs(pct) < 0.5:
                verdict = "noise"
            elif d == 0:
                verdict = ""
            else:
                verdict = "better" if pct * d > 0 else "worse"
            sc["metrics"].append({"key": k, "label": label, "a": a, "b": b, "pct": round(pct, 2),
                                  "verdict": verdict, "direction": d, "noise_pct": noise,
                                  "n_a": aa[k]["n"], "n_b": ab[k]["n"]})
    for name, res_b in rows_b.items():
        if name not in paired_b:
            scenarios.append({"name": name, "title": res_b["title"], "only": "b", "metrics": [],
                              "flags_a": [], "flags_b": res_b["flags"], "tool_a": None, "tool_b": tb.get(name)})
    return {"a": {"session": da.name, "tag": sa["tag"], "started": sa["started"], "device": _device(sa, ma)},
            "b": {"session": db.name, "tag": sb["tag"], "started": sb["started"], "device": _device(sb, mb)},
            "scenarios": scenarios,
            "config_diff": [[k, a, b] for k, a, b in config_diff(ma["snapshot"], mb["snapshot"])]}


def compare(ref_a: str, ref_b: str, all_metrics: bool = False) -> str:
    d = compare_data(ref_a, ref_b, all_metrics)
    out = [f"# Compare: {d['a']['tag']} -> {d['b']['tag']}", "",
           f"- A: {d['a']['session']} ({d['a']['device']})", f"- B: {d['b']['session']} ({d['b']['device']})",
           "- Change = (B - A) / A. 'better'/'worse' by metric direction; '~ noise' when",
           "  the change is smaller than the larger run-to-run CV of the two sessions.", ""]
    for sc in d["scenarios"]:
        if sc["only"]:
            out.append(f"## {sc['name']}\n\nOnly in {sc['only'].upper()}.\n")
            continue
        out.append(f"## {sc['name']} ({sc['title']})\n")
        if sc["tool_a"] != sc["tool_b"]:
            out.append(f"Proton differs: A {sc['tool_a']}, B {sc['tool_b']}\n")
        rows = [[m["label"], m["a"], m["b"], f"{m['pct']:+.1f}%",
                 "~ noise" if m["verdict"] == "noise" else m["verdict"], f"{m['n_a']}/{m['n_b']}"]
                for m in sc["metrics"]]
        out.append(table(["Metric", "A mean", "B mean", "Change", "", "Runs A/B"], rows,
                         ["l", "r", "r", "r", "l", "r"]) if rows else "No common metrics.\n")
        fl = [f"A: {f}" for f in sc["flags_a"]] + [f"B: {f}" for f in sc["flags_b"]]
        if fl:
            out.append("\nFlags:\n\n" + "\n".join(f"- {f}" for f in fl) + "\n")
        out.append("")
    out.append("## System config differences (session start)\n")
    out.append(table(["Setting", "A", "B"], d["config_diff"]) if d["config_diff"]
               else "None: both sessions ran with the same settings.\n")
    return "\n".join(out)
