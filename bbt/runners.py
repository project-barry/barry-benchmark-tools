"""Workload runners. Each takes a scenario dict and a run directory, drives one
run from start to finish, and returns {'metrics': {...}, ...extra}.

  vkmark  native Vulkan (aarch64), headless by default; own FPS output
  steam   any Steam app (native or Proton) launched into the Gaming Mode
          session; frame times from MangoHud's per-frame log
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import mangohud, stats, steam, sysinfo, wine
from .sampler import Sampler, summarize, window
from .util import OPT, STATE, BENCH, log, proc_cmdline, proc_tree, pid_alive, wait_until

WRAP_OPTS = f"{BENCH}/bin/bbt-wrap %command%"


class RunError(RuntimeError):
    pass


# ---- vkmark --------------------------------------------------------------------

VKMARK_LINE = re.compile(r"^\[(?P<scene>[^\]]+)\]\s*(?P<opts>.*?):?\s*FPS:\s*(?P<fps>[\d.]+)\s+FrameTime:\s*(?P<ft>[\d.]+) ms", re.M)


def vkmark_cmd(sc: dict) -> tuple[list[str], dict]:
    v = sc.get("vkmark", {})
    usr = OPT / "usr"
    exe = usr / "bin/vkmark"
    if not exe.exists():
        raise RunError(f"vkmark not found at {exe}; run `bench setup`")
    cmd = [str(exe), "--winsys-dir", str(usr / "lib/vkmark"), "--data-dir", str(usr / "share/vkmark"),
           "--winsys", v.get("winsys", "headless"), "-s", v.get("size", "1920x1080")]
    if v.get("present_mode"):
        cmd += ["-p", v["present_mode"]]
    for b in v.get("benchmarks", []):
        cmd += ["-b", b]
    env = dict(os.environ, LD_LIBRARY_PATH=str(usr / "lib"))
    if v.get("winsys", "headless") != "headless":
        env = dict(steam.session_env(), LD_LIBRARY_PATH=str(usr / "lib"))
    env.update({k: str(x) for k, x in (sc.get("env") or {}).items()})
    return cmd, env


def run_vkmark(sc: dict, run_dir: Path, sampler_interval: float) -> dict:
    cmd, env = vkmark_cmd(sc)
    proc_holder = {}
    smp = Sampler(run_dir / "samples.csv", sampler_interval,
                  root_pid_fn=lambda: proc_holder.get("pid")).start()
    smp.mark("capture_start")
    log(f"vkmark: {' '.join(shlex.quote(c) for c in cmd[5:])}")
    with open(run_dir / "vkmark.log", "w") as out:
        p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env)
        proc_holder["pid"] = p.pid
        try:
            rc = p.wait(timeout=sc.get("timeout_s", 900))
        except subprocess.TimeoutExpired:
            p.kill()
            raise RunError("vkmark timed out")
    smp.mark("capture_end")
    rows = smp.stop()
    text = (run_dir / "vkmark.log").read_text()
    if rc != 0:
        raise RunError(f"vkmark exited {rc}: {text.strip().splitlines()[-1] if text.strip() else ''}")
    m = {}
    seen: dict[str, int] = {}
    for hit in VKMARK_LINE.finditer(text):
        name = re.sub(r"[^a-z0-9]+", "_", hit["scene"].lower()).strip("_")
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}{seen[name]}"
        m[f"vkmark_{name}_fps"] = float(hit["fps"])
    score = re.search(r"vkmark Score:\s*(\d+)", text)
    if not score:
        raise RunError("no vkmark score in output")
    m = {"vkmark_score": int(score.group(1)), **m}
    hw = summarize(window(rows, smp.marks["capture_start"], smp.marks["capture_end"]))
    return {"metrics": {**m, **hw}, "marks": smp.marks,
            "emulation": sysinfo.emulation(sysinfo.binary_arch(cmd[0])["arch"], "native")}


# ---- Steam ---------------------------------------------------------------------

def _write_run_env(env: dict, args: list[str], ttl_s: float) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "run.env"
    lines = [f"BBT_EXPIRES={int(time.time() + ttl_s)}",
             "BBT_ENV=(" + " ".join(shlex.quote(f"{k}={v}") for k, v in env.items()) + ")",
             "BBT_ARGS=(" + " ".join(shlex.quote(a) for a in args) + ")", ""]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)
    return path


def _clear_run_env() -> None:
    try:
        (STATE / "run.env").unlink()
    except FileNotFoundError:
        pass


def _game_exe(pids: list[int], install: str) -> str | None:
    """The game binary: an argv entry under the install dir (.exe first)."""
    best = None
    for pid in pids:
        for a in proc_cmdline(pid):
            a = a.replace("\\", "/")
            if install and a.startswith(install) and os.path.isfile(a):
                if a.lower().endswith(".exe"):
                    return a
                best = best or a
    return best


def _game_results(sc: dict, install: str, since: float, run_dir: Path) -> tuple[dict, list[str]]:
    """Move the game's own result files (written during this run) into the run
    folder and pull numbers out of them with the scenario's regexes."""
    gr = sc.get("game_results")
    if not gr:
        return {}, []
    base = Path(gr.get("dir") or install)
    files = sorted(p for p in base.glob(gr.get("glob", "*")) if p.stat().st_mtime >= since - 1)
    if not files:
        return {}, []
    dst = run_dir / "game"
    dst.mkdir(exist_ok=True)
    moved = []
    for f in files:
        shutil.move(str(f), dst / f.name)
        moved.append(f"game/{f.name}")
    text = (dst / files[-1].name).read_text(errors="replace")
    out = {}
    for name, pat in (gr.get("metrics") or {}).items():
        m = re.search(pat, text, re.M)
        if m:
            out[name] = float(m.group(1))
    return out, moved


def run_steam(sc: dict, run_dir: Path, sampler_interval: float) -> dict:
    """Launch through Steam, capture with MangoHud, stop the game.

    capture: window      log duration_s, starting settle_s after the game's
                         first frame; then the game is closed (default)
             until_exit  log from the first frame until the game quits by
                         itself (built-in benchmarks); trim_start_s and
                         trim_end_s cut loading / fade-out from both ends
    """
    appid = str(sc["appid"])
    man = steam.app_manifest(appid)
    if not man:
        raise RunError(f"app {appid} is not installed")
    if man.get("StateFlags") not in ("4", None):
        raise RunError(f"app {appid} is not fully installed/updated (StateFlags {man.get('StateFlags')})")
    tool = sc.get("proton")
    steam.configure(appid, tool, WRAP_OPTS)
    if not steam.is_running():
        steam.start()
    warnings = []
    if sc.get("wine_registry"):
        try:
            wine.set_values(appid, sc["wine_registry"])
        except RuntimeError as e:
            warnings.append(f"registry settings not applied: {e}")

    mode = sc.get("capture", "window")
    settle, dur = float(sc.get("settle_s", 30)), float(sc.get("duration_s", 60))
    if mode == "until_exit":
        trim0, trim1 = float(sc.get("trim_start_s", 0)), float(sc.get("trim_end_s", 0))
        timeout = float(sc.get("timeout_s", 900))
        mangohud.write_config(run_dir / "mangohud.conf", run_dir / "mangohud", 1, 0)
    elif mode == "window":
        trim0 = trim1 = 0.0
        timeout = float(sc.get("timeout_s", settle + dur + 180))
        mangohud.write_config(run_dir / "mangohud.conf", run_dir / "mangohud", settle, dur)
    else:
        raise RunError(f"unknown capture mode {mode!r}")
    mh_dir = run_dir / "mangohud"
    env = {"MANGOHUD": "1", "MANGOHUD_CONFIGFILE": str(run_dir / "mangohud.conf"),
           **{k: str(v) for k, v in (sc.get("env") or {}).items()}}
    _write_run_env(env, [str(a) for a in sc.get("args") or []], timeout + 300)

    smp = Sampler(run_dir / "samples.csv", sampler_interval, root_pid_fn=lambda: steam.find_reaper(appid)).start()
    evidence, exe, exited_early = [], None, False
    t_wall = time.time()
    try:
        root = steam.launch(appid, timeout=sc.get("launch_timeout_s", 120))
        smp.mark("launched")
        t_launch = time.monotonic()
        # MangoHud creates its log when autostart_log fires: that is capture start
        wait_until(lambda: mangohud.find_log(mh_dir) is not None or not pid_alive(root),
                   (settle if mode == "window" else 0) + 300, 0.5)
        if mangohud.find_log(mh_dir) is None:
            raise RunError("MangoHud log never appeared (layer not loaded, or the game hung before capture)"
                           if pid_alive(root) else "game exited before capture started")
        smp.mark("capture_start")
        log(f"capturing ({mode}; log started {time.monotonic() - t_launch:.0f} s after launch)")
        time.sleep(10)  # game fully up: note which binary runs and what emulates it
        tree = proc_tree(root)
        evidence = sysinfo.emulation_evidence(tree)
        exe = _game_exe(tree, man.get("_installpath", ""))
        if mode == "until_exit":
            if not wait_until(lambda: not pid_alive(root), timeout, 1):
                raise RunError(f"game still running after {timeout:.0f} s (benchmark did not finish?)")
            time.sleep(3)  # MangoHud writes the log as the game exits
        else:
            end = time.monotonic() + dur + 5
            wait_until(lambda: not pid_alive(root), max(0.0, end - time.monotonic()), 1)
            exited_early = not pid_alive(root)
        smp.mark("capture_end")
    finally:
        _clear_run_env()
        steam.kill(appid)
        rows = smp.stop()

    log_path = mangohud.find_log(mh_dir)
    parsed = mangohud.parse(log_path)
    frames = [r for r in parsed["rows"] if "frametime" in r]
    if frames and "elapsed" in frames[0] and (trim0 or trim1):
        e0, e1 = frames[0]["elapsed"], frames[-1]["elapsed"]
        frames = [r for r in frames if e0 + trim0 * 1e9 <= r["elapsed"] <= e1 - trim1 * 1e9]
    m = stats.frametime_metrics([r["frametime"] for r in frames])
    if m.get("frames", 0) < 10:
        raise RunError(f"MangoHud log has {m.get('frames', 0)} usable frames")
    m.update(mangohud.metrics({"rows": frames}))
    game_m, game_files = _game_results(sc, man.get("_installpath", ""), t_wall, run_dir)
    m.update(game_m)
    # hardware numbers over the same window as the frame times
    c0 = smp.marks["capture_start"] + trim0
    m.update(summarize(window(rows, c0, c0 + m["capture_s"])))
    if m.get("system_w_avg") and m.get("avg_fps"):
        m["mj_per_frame"] = round(1000 * m["system_w_avg"] / m["avg_fps"], 2)
    tinfo = steam.tool_info(tool or steam.compat_mapping(appid))
    arch = sysinfo.binary_arch(exe)["arch"] if exe else "unknown"
    runtime = "proton" if (exe or "").lower().endswith(".exe") or "proton" in (tool or "").lower() else "native"
    if exited_early:
        warnings.append("game exited during capture")
    out = {"metrics": m, "marks": smp.marks, "mangohud_log": str(log_path.relative_to(run_dir)),
           "mangohud_sysinfo": parsed["sysinfo"], "proton": tinfo, "game_exe": exe,
           "game_files": game_files, "trim_s": [trim0, trim1],
           "emulation": sysinfo.emulation(arch, runtime, tinfo.get("arch"), evidence)}
    if sc.get("wine_registry"):
        out["wine_registry"] = {k: wine.get_values(appid, k) for k in sc["wine_registry"]}
    if warnings:
        out["warning"] = "; ".join(warnings)
    return out


RUNNERS = {"vkmark": run_vkmark, "steam": run_steam}
