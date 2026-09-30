"""Control the Gaming Mode Steam client from an SSH session (as the steamos user).

Steam runs as the systemd user unit steam.service inside gamescope-session;
stopping that unit closes Steam cleanly while gamescope stays up. Config files
(config.vdf, localconfig.vdf) are only edited while Steam is stopped, with a
timestamped backup first.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import vdf
from .util import STATE, log, now_iso, proc_cmdline, proc_tree, pid_alive, rd, sh, wait_until

STEAM = Path.home() / ".local/share/Steam"
CONFIG_VDF = STEAM / "config/config.vdf"
LOGINUSERS = STEAM / "config/loginusers.vdf"
COMPAT_LOG = STEAM / "logs/compat_log.txt"
STEAMID64_BASE = 76561197960265728
BACKUPS = STATE / "backups"


def session_env() -> dict:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def _systemctl(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=timeout, env=session_env())


# ---- client state ------------------------------------------------------------

def client_pids() -> list[int]:
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            cmd = proc_cmdline(int(d))
            if cmd and os.path.basename(cmd[0]) == "steam" and "steamrt" in cmd[0]:
                out.append(int(d))
    return out


def is_running() -> bool:
    return _systemctl("is-active", "steam.service").stdout.strip() == "active" and bool(client_pids())


def stop(timeout: float = 60) -> None:
    if not client_pids() and _systemctl("is-active", "steam.service").stdout.strip() != "active":
        return
    log("stopping Steam (steam.service)")
    _systemctl("stop", "steam.service", timeout=timeout + 10)
    if not wait_until(lambda: not client_pids(), timeout):
        raise RuntimeError("Steam did not exit")
    time.sleep(2)


def start(timeout: float = 180) -> None:
    if is_running():
        return
    log("starting Steam (steam.service)")
    mark = COMPAT_LOG.stat().st_size if COMPAT_LOG.exists() else 0
    _systemctl("start", "steam.service")

    def ready():
        if not client_pids():
            return False
        try:
            with open(COMPAT_LOG, "rb") as f:
                size = f.seek(0, 2)
                f.seek(mark if size >= mark else 0)
                return b"Waiting for compat in post-logon" in f.read()
        except OSError:
            return False
    if not wait_until(ready, timeout, 2):
        raise RuntimeError("Steam did not finish logging on")
    time.sleep(10)  # let the library UI settle before sending URLs


# ---- accounts / files --------------------------------------------------------

def account_id() -> str:
    """userdata/<accountid> of the most recent login (never logged or stored)."""
    users = vdf.get(vdf.loads(LOGINUSERS.read_text()), "users") or []
    ids = [sid for sid, node in users if vdf.get(node, "MostRecent") == "1"] or [u[0] for u in users]
    for sid in ids:
        acc = str(int(sid) - STEAMID64_BASE)
        if (STEAM / "userdata" / acc).is_dir():
            return acc
    dirs = [p.name for p in (STEAM / "userdata").iterdir() if p.name.isdigit() and p.name != "0"]
    if len(dirs) == 1:
        return dirs[0]
    raise RuntimeError("cannot tell which Steam account is logged in")


def localconfig_vdf() -> Path:
    return STEAM / "userdata" / account_id() / "config/localconfig.vdf"


def _backup(path: Path) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    first = BACKUPS / f"{path.name}.orig"
    if not first.exists():
        shutil.copy2(path, first)  # the pre-harness original, kept forever
    dst = BACKUPS / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dst)
    return dst


def _rewrite(path: Path, mutate) -> bool:
    if client_pids():
        raise RuntimeError(f"refusing to edit {path.name} while Steam is running")
    text = path.read_text()
    tree = vdf.loads(text)
    mutate(tree)
    new = vdf.dumps(tree)
    if new == text:
        return False
    bak = _backup(path)
    tmp = path.with_name(path.name + ".bbt-tmp")
    tmp.write_text(new)
    os.chmod(tmp, path.stat().st_mode)
    tmp.replace(path)
    log(f"edited {path.name} (backup {bak.name})")
    return True


# ---- compat tools --------------------------------------------------------------

def compat_mapping(appid: str) -> str | None:
    node = vdf.get(vdf.loads(CONFIG_VDF.read_text()),
                   "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", str(appid))
    return vdf.get(node, "name") if node else None


def launch_options(appid: str) -> str | None:
    node = vdf.get(vdf.loads(localconfig_vdf().read_text()),
                   "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", str(appid))
    val = vdf.get(node, "LaunchOptions") if node else None
    return vdf.unescape(val) if val is not None else None


def configure(appid: str, tool: str | None, launch_opts: str | None) -> bool:
    """Make Steam use `tool` and `launch_opts` for appid. Restarts Steam only if
    something must change. tool None = leave mapping alone; '' = remove it."""
    appid = str(appid)
    need_tool = tool is not None and (compat_mapping(appid) or "") != tool
    need_opts = launch_opts is not None and (launch_options(appid) or "") != launch_opts
    if not (need_tool or need_opts):
        return False
    stop()
    if need_tool:
        def m(tree):
            ctm = vdf.ensure(tree, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping")
            ctm[:] = [p for p in ctm if p[0] != appid]
            if tool:
                ctm.append([appid, [["name", vdf.escape(tool)], ["config", ""], ["priority", "250"]]])
        _rewrite(CONFIG_VDF, m)
    if need_opts:
        def m(tree):
            app = vdf.ensure(tree, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid)
            vdf.set_value(app, "LaunchOptions", vdf.escape(launch_opts))
        _rewrite(localconfig_vdf(), m)
    start()  # Gaming Mode needs its client back either way
    return True


def registered_tools() -> dict[str, str]:
    """Valve tool name -> AppID, from Steam's own compat log."""
    out = {}
    for line in rd(COMPAT_LOG).splitlines():
        m = re.search(r"Registering tool (\S+), AppID (\d+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def app_manifest(appid: str) -> dict:
    for lib in library_paths():
        p = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if p.exists():
            node = vdf.get(vdf.loads(p.read_text()), "AppState") or []
            d = {k: v for k, v in node if isinstance(v, str)}
            d["_library"] = str(lib)
            d["_installpath"] = str(lib / "steamapps/common" / d.get("installdir", ""))
            return d
    return {}


def library_paths() -> list[Path]:
    libs = [STEAM]
    try:
        node = vdf.get(vdf.loads((STEAM / "steamapps/libraryfolders.vdf").read_text()), "libraryfolders") or []
        for _, v in node:
            p = vdf.get(v, "path") if isinstance(v, list) else None
            if p and Path(vdf.unescape(p)) not in libs:
                libs.append(Path(vdf.unescape(p)))
    except (OSError, vdf.VDFError):
        pass
    return libs


def custom_tools() -> dict[str, Path]:
    """compatibilitytools.d entries (GE-Proton etc.): internal name -> dir."""
    out = {}
    for base in (STEAM / "compatibilitytools.d", Path("/usr/share/steam/compatibilitytools.d")):
        if not base.is_dir():
            continue
        for d in base.iterdir():
            f = d / "compatibilitytool.vdf"
            if not f.exists():
                continue
            tools = vdf.get(vdf.loads(f.read_text()), "compatibilitytools", "compat_tools") or []
            for name, node in tools:
                ip = vdf.get(node, "install_path") or "."
                out[name] = (d / ip).resolve()
    return out


def tool_info(tool: str | None) -> dict:
    """Where a compat tool lives and which version it is."""
    info = {"name": tool}
    if not tool:
        return info
    path = None
    if tool in custom_tools():
        path, info["source"] = custom_tools()[tool], "compatibilitytools.d"
    elif tool in registered_tools():
        info["appid"] = registered_tools()[tool]
        man = app_manifest(info["appid"])
        if man:
            path, info["source"] = Path(man["_installpath"]), "steam"
            info["buildid"] = man.get("buildid")
    if path:
        info["path"] = str(path)
        info["installed"] = path.is_dir()
        ver = rd(path / "version")
        if ver:
            info["version"] = ver
    else:
        info["installed"] = False
    low = tool.lower()
    info["arch"] = "arm64" if "arm64" in low else ("x86_64" if "proton" in low else "unknown")
    return info


# ---- launching ---------------------------------------------------------------

def send_url(url: str) -> None:
    log(f"steam {url}")
    subprocess.Popen(["steam", "-ifrunning", url], env=session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def find_reaper(appid: str) -> int | None:
    tag = f"AppId={appid}"
    for d in os.listdir("/proc"):
        if d.isdigit():
            cmd = proc_cmdline(int(d))
            if cmd and os.path.basename(cmd[0]) == "reaper" and "SteamLaunch" in cmd and tag in cmd:
                return int(d)
    return None


def launch(appid: str, timeout: float = 120) -> int:
    if find_reaper(appid):
        raise RuntimeError(f"app {appid} is already running")
    send_url(f"steam://rungameid/{appid}")
    if not wait_until(lambda: find_reaper(appid) is not None, timeout, 1):
        raise RuntimeError(f"app {appid} did not start within {timeout:.0f} s")
    pid = find_reaper(appid)
    log(f"app {appid} running (reaper pid {pid})")
    return pid


def kill(appid: str, grace: float = 15) -> None:
    root = find_reaper(appid)
    log(f"closing app {appid} (reaper {root})")
    if not root:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
        for p in reversed(proc_tree(root)):
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
        if wait_until(lambda: not pid_alive(root), wait, 0.5):
            break
    # wine leaves wineserver/helpers that outlive the reaper; sweep them
    time.sleep(2)
    for d in os.listdir("/proc"):
        if d.isdigit():
            env = rd(f"/proc/{d}/environ").replace("\0", "\n")
            if f"SteamAppId={appid}\n" in env + "\n" or f"SteamGameId={appid}\n" in env + "\n":
                try:
                    os.kill(int(d), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    log(f"app {appid} stopped")
