"""bench: headless benchmark harness for SteamOS on ARM handhelds.

Runs on the device, or from another machine that has devices registered
(remote mode: sessions run on a device, results land locally):
  bench devices add rp6 steamos@192.0.2.10     register a device (IP or host name)
  bench --device thor run MATRIX --tag T       pick a device (default: bench devices default)

  bench run MATRIX.yaml --tag TAG     run every scenario, write a results folder
  bench compare A B                   % change per scenario/metric (A, B = tags or session names)
  bench list                          sessions in ~/bench/results
  bench snapshot                      print the current system config as JSON
  bench sample [SECONDS]              print live clocks/temps/power once per second
  bench steam status APPID            compat tool, launch options, install state
  bench steam tools                   compat tools Steam knows + installed custom tools
  bench steam set-tool APPID TOOL     map a compat tool (restarts Steam if it changes)
  bench steam unwrap APPID            remove the harness launch options for an app
  bench setup                         fetch vkmark into ~/bench/opt (no root needed)
  bench web [--host H] [--port P]     web app for starting and reviewing runs

Remote mode only:
  bench devices [list|add|remove|default|test]   the devices this machine drives
  bench deploy                        copy the harness to the device
  bench status                        what runs on the device, what is not pulled yet
  bench attach                        follow the current/last run, then pull it
  bench stop                          end the running session (keeps what was measured)
  bench pull                          fetch finished sessions still on the device
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import util
from .util import OPT, log, read_json


def cmd_run(a):
    from . import config, session
    m = config.load(Path(a.matrix), a.matrix_label)
    if a.only:
        m["scenarios"] = [s for s in m["scenarios"] if s["name"] in a.only]
        if not m["scenarios"]:
            raise SystemExit("no scenario matches --only")
    for s in m["scenarios"]:
        if a.runs is not None:
            s["runs"] = a.runs
        if a.warmup is not None:
            s["warmup"] = a.warmup
        if s["runs"] < 3:
            log(f"note: {s['name']} has {s['runs']} measured run(s); results will be flagged (want >= 3)")
    if a.dry_run:
        print(json.dumps(m, indent=2))
        return
    d = session.Session(m, a.tag, Path(a.matrix), device=a.device_label).run()
    print(d)


def cmd_compare(a):
    from . import compare
    text = compare.compare(a.a, a.b, all_metrics=a.all)
    if a.out:
        Path(a.out).write_text(text)
        log(f"wrote {a.out}")
    print(text)


def cmd_list(a):
    if not util.RESULTS.is_dir():
        return
    for d in sorted(util.RESULTS.iterdir()):
        s = d / "summary.json"
        if not s.exists():
            continue
        j = read_json(s)
        scen = ", ".join(f"{r['name']}({len([x for x in r['runs'] if not x['warmup'] and x['status'] == 'ok'])})"
                         for r in j["scenarios"])
        print(f"{d.name:44} tag={j['tag']:18} device={j.get('device') or '-':10} {scen}")


def cmd_snapshot(a):
    from . import sysinfo
    print(json.dumps(sysinfo.snapshot(), indent=2))


def cmd_sample(a):
    from .sampler import Sampler
    import tempfile
    smp = Sampler(Path(tempfile.mkstemp(suffix=".csv")[1]), 1.0).start()
    shown = 0
    end = time.monotonic() + a.seconds
    try:
        while time.monotonic() < end:
            time.sleep(0.5)
            while shown < len(smp.rows):
                r = smp.rows[shown]
                shown += 1
                if a.json:
                    print(json.dumps(r), flush=True)
                    continue
                clk = "/".join(str(r.get(k, "-")) for k in r if k.startswith("policy") and k.endswith("_mhz"))
                print(f"t={r['t']:6.1f} cpu {r['cpu_load']}% {clk} MHz | gpu {r.get('gpu_mhz')} MHz | "
                      f"cpu {r.get('cpu_temp_c')} C gpu {r.get('gpu_temp_c')} C | {r['power_source']} "
                      f"{r['system_w']} W bat {r['battery_pct']}% | fan {r.get('fan_rpm')}", flush=True)
    except KeyboardInterrupt:
        pass
    smp.stop()


def cmd_steam(a):
    from . import steam
    if a.action == "tools":
        reg = steam.registered_tools()
        for name in sorted(reg):
            i = steam.tool_info(name)
            print(f"{name:34} appid {reg[name]:8} {'installed' if i.get('installed') else '-':9} {i.get('version', '')}")
        for name, path in sorted(steam.custom_tools().items()):
            i = steam.tool_info(name)
            print(f"{name:34} custom         {'installed':9} {i.get('version', '')}  ({path})")
    elif a.action == "status":
        man = steam.app_manifest(a.appid)
        print(json.dumps({"appid": a.appid, "name": man.get("name"), "installed": bool(man),
                          "state_flags": man.get("StateFlags"), "path": man.get("_installpath"),
                          "compat_tool": steam.compat_mapping(a.appid),
                          "launch_options": steam.launch_options(a.appid),
                          "running": steam.find_reaper(a.appid) is not None,
                          "steam_running": steam.is_running()}, indent=2))
    elif a.action == "set-tool":
        steam.configure(a.appid, a.tool, None)
        print(steam.compat_mapping(a.appid))
    elif a.action == "unwrap":
        steam.configure(a.appid, None, "")
        print("launch options cleared")


def cmd_devices(a):
    from . import devices, remote
    act = a.action or "list"
    try:
        if act == "list":
            data = devices.load()
            if not data["devices"]:
                print("no devices yet: bench devices add ID USER@HOST[:PORT]")
            for d in data["devices"]:
                mark = "*" if d["id"] == data.get("default") else " "
                port = f":{d['port']}" if d.get("port", 22) != 22 else ""
                print(f"{mark} {d['id']:14} {d['user']}@{d['host']}{port:6} {d['name']}"
                      + (f"  (ssh_opts: {d['ssh_opts']})" if d.get("ssh_opts") else ""))
        elif act == "add":
            if not a.id or not a.target or "@" not in a.target:
                raise SystemExit("usage: bench devices add ID USER@HOST[:PORT] [--name N] [--ssh-opts '...']")
            user, host = a.target.split("@", 1)
            port = 22
            if host.count(":") == 1:
                host, port = host.split(":")
            d = devices.add({"id": a.id, "name": a.name, "user": user, "host": host, "port": port,
                             "ssh_opts": a.ssh_opts, "remote_dir": a.remote_dir}, allow_ssh_opts=True, replace=a.replace)
            print(f"added {d['id']}; testing the connection...")
            _print_probe(remote.Remote(devices.conf(d)).probe())
        elif act == "remove":
            devices.remove(a.id)
            print(f"removed {a.id} (its sessions stay in the results folder)")
        elif act == "default":
            devices.set_default(a.id)
            print(f"default device: {a.id}")
        elif act == "test":
            d = devices.get(a.id)
            if not d:
                raise SystemExit(f"no device {a.id or '(default)'}")
            _print_probe(remote.Remote(devices.conf(d)).probe())
    except devices.DeviceError as e:
        raise SystemExit(str(e))


def _print_probe(r: dict) -> None:
    if not r["ok"]:
        hint = {"auth": "the device does not accept this machine's SSH key: ssh-copy-id USER@HOST",
                "hostkey": "the device's host key changed; check it, then fix ~/.ssh/known_hosts",
                "unreachable": "no connection: check the address, that it is on, and the network"}[r["problem"]]
        print(f"FAILED ({r['problem']}): {hint}\n  {r['detail']}")
        return
    f = r["facts"]
    for k in ("user", "hostname", "model", "os", "kernel", "arch", "python", "pyyaml", "steam_service", "harness", "vkmark"):
        print(f"  {k:14} {f.get(k) or '-'}")
    print("  ready to run" if f["can_run"] else f"  missing: {', '.join(f['missing'])}")


def cmd_web(a):
    from .web.server import serve
    serve(a.host, a.port, not a.no_browser)


def cmd_setup(a):
    """vkmark + assimp from the SteamOS repo, unpacked under ~/bench/opt."""
    pkgs = OPT / "pkgs"
    pkgs.mkdir(parents=True, exist_ok=True)
    urls = subprocess.run(["pacman", "-Sp", "vkmark"], capture_output=True, text=True).stdout.split()
    urls = [u for u in urls if u.startswith("http")]
    if not urls:
        raise SystemExit("pacman -Sp vkmark gave no package URLs")
    for u in urls:
        name = u.rsplit("/", 1)[1]
        dst = pkgs / name.replace(":", "_")
        if not dst.exists():
            log(f"downloading {name}")
            subprocess.run(["curl", "-sfL", "-o", str(dst), u], check=True)
        with tarfile.open(dst, "r:*") if not dst.name.endswith(".zst") else _zst(dst) as tf:
            members = [m for m in tf.getmembers() if m.name.startswith("usr/") and not m.name.startswith("usr/include")]
            tf.extractall(OPT, members=members, filter="data")
    log(f"vkmark installed under {OPT / 'usr'}")


def _zst(path):
    """tarfile over a zstd stream (Python 3.12 has no zstd): decompress via zstd(1)."""
    import io
    raw = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    return tarfile.open(fileobj=io.BytesIO(raw))


def main(argv=None):
    p = argparse.ArgumentParser(prog="bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", "-d", help="device id from `bench devices` (remote mode; default: the default device)")
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run", help="run a matrix")
    r.add_argument("matrix")
    r.add_argument("--tag", required=True, help="label for this session, e.g. baseline, gpu-cap-550")
    r.add_argument("--only", nargs="+", help="run only these scenario names")
    r.add_argument("--runs", type=int, help="override measured runs per scenario")
    r.add_argument("--warmup", type=int, help="override warm-up runs per scenario")
    r.add_argument("--dry-run", action="store_true", help="print the resolved matrix and exit")
    r.add_argument("--matrix-label", help=argparse.SUPPRESS)
    r.add_argument("--device-label", help=argparse.SUPPRESS)
    r.add_argument("--no-deploy", action="store_true", help="remote mode: skip copying the harness first")
    r.add_argument("--keep-remote", action="store_true", help="remote mode: keep the device copy after pulling")
    r.set_defaults(fn=cmd_run)
    for name, hlp in (("deploy", "copy the harness to the device"), ("status", "remote run status"),
                      ("attach", "follow the current/last remote run"), ("stop", "stop the remote run"),
                      ("pull", "fetch finished sessions from the device")):
        x = sp.add_parser(name, help=f"remote mode: {hlp}")
        x.add_argument("--keep-remote", action="store_true", help="keep the device copy after pulling")
        x.set_defaults(fn=None)
    c = sp.add_parser("compare", help="compare two sessions")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--all", action="store_true", help="include every metric, not just the key ones")
    c.add_argument("--out", help="also write the comparison to this .md file")
    c.set_defaults(fn=cmd_compare)
    sp.add_parser("list", help="list sessions").set_defaults(fn=cmd_list)
    dv = sp.add_parser("devices", help="the devices this machine drives (remote mode)")
    dv.add_argument("action", nargs="?", choices=["list", "add", "remove", "default", "test"])
    dv.add_argument("id", nargs="?")
    dv.add_argument("target", nargs="?", help="USER@HOST[:PORT] (add)")
    dv.add_argument("--name", help="display name (add)")
    dv.add_argument("--ssh-opts", default="", help="extra ssh options (add), e.g. \"-o UserKnownHostsFile=...\"")
    dv.add_argument("--remote-dir", default="bench", help="harness folder on the device, under ~ (add)")
    dv.add_argument("--replace", action="store_true", help="overwrite an existing device (add)")
    dv.set_defaults(fn=cmd_devices)
    sp.add_parser("snapshot", help="print system config JSON").set_defaults(fn=cmd_snapshot)
    s = sp.add_parser("sample", help="live sensor readout")
    s.add_argument("seconds", nargs="?", type=float, default=30)
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.set_defaults(fn=cmd_sample)
    st = sp.add_parser("steam", help="Steam helpers")
    st.add_argument("action", choices=["status", "tools", "set-tool", "unwrap"])
    st.add_argument("appid", nargs="?")
    st.add_argument("tool", nargs="?")
    st.set_defaults(fn=cmd_steam)
    sp.add_parser("setup", help="fetch vkmark").set_defaults(fn=cmd_setup)
    w = sp.add_parser("web", help="start the web app (run and review sessions in a browser)")
    w.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 = every interface)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-browser", action="store_true", help="do not open a browser")
    w.set_defaults(fn=cmd_web)
    argv = sys.argv[1:] if argv is None else argv
    a = p.parse_args(argv)
    if a.cmd == "steam" and a.action in ("status", "set-tool", "unwrap") and not a.appid:
        p.error("this action needs an APPID")
    if a.cmd == "steam" and a.action == "set-tool" and a.tool is None:
        p.error("set-tool needs a TOOL name ('' removes the mapping)")
    from . import remote
    if a.cmd == "devices":
        return a.fn(a)
    # argv for the device side: without this machine's --device option
    dev_argv, skip = [], False
    for i, x in enumerate(argv):
        if skip:
            skip = False
            continue
        if x in ("--device", "-d") and i < argv.index(a.cmd):
            skip = True
            continue
        if x.startswith("--device=") and i < argv.index(a.cmd):
            continue
        dev_argv.append(x)
    conf = remote.load_conf(a.device)
    if conf is None:
        if a.fn is None:
            p.error(f"`{a.cmd}` needs remote mode: register a device first (bench devices add ID USER@HOST)")
        a.fn(a)
        return
    util.RESULTS = remote.local_results(conf)
    if a.cmd in ("list", "compare", "web"):
        a.fn(a)
        return
    r = remote.Remote(conf)
    handlers = {"run": remote.cmd_run, "attach": remote.cmd_attach, "stop": remote.cmd_stop,
                "pull": remote.cmd_pull, "status": remote.cmd_status, "deploy": lambda r, a: r.deploy()}
    if a.cmd in handlers:
        handlers[a.cmd](r, a)
    else:  # snapshot, sample, steam, setup: run on the device as-is
        remote.passthrough(r, dev_argv, tty=a.cmd == "sample" and sys.stdout.isatty())


if __name__ == "__main__":
    main()
