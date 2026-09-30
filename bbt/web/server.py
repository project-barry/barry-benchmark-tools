"""`bench web`: a local web app for starting and reviewing benchmark sessions.

Standard library only. Serves the single-page app in ./static and a JSON API
over the same results folder the CLI uses (local results in remote mode).
Runs, pulls, deploys etc. are `bench` subprocesses ("jobs"), so the web app
does exactly what the CLI does; their output streams to the browser as
server-sent events.

Security: every request needs the session token (printed at start, set as a
SameSite=Lax HttpOnly cookie on first visit). Changing requests also need
an X-BBT header, which cross-site pages cannot send without a CORS preflight
this server never grants. It binds to 127.0.0.1 unless told otherwise.
"""
from __future__ import annotations

import csv
import http.server
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from .. import mangohud, util
from ..util import read_json, slug

STATIC = Path(__file__).parent / "static"
MATRIX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.ya?ml$")
SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,60}$")


class Jobs:
    """One bench subprocess at a time (plus quick side jobs), logs on disk."""

    def __init__(self, root: Path, work: Path):
        self.root, self.dir = root, work / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen] = {}

    def _meta_path(self, jid):
        return self.dir / f"{jid}.json"

    def meta(self, jid: str) -> dict | None:
        p = self._meta_path(jid)
        return read_json(p) if p.exists() else None

    def list(self, n=20) -> list[dict]:
        metas = [read_json(p) for p in sorted(self.dir.glob("*.json"))[-n:]]
        return list(reversed(metas))

    def running(self) -> list[dict]:
        with self.lock:
            return [m for jid, p in self.procs.items() if p.poll() is None and (m := self.meta(jid))]

    def start(self, kind: str, args: list[str], label: str, device: str | None = None) -> dict:
        with self.lock:
            for jid, p in self.procs.items():
                if p.poll() is None and (self.meta(jid) or {}).get("device") == device:
                    raise RuntimeError(f"{device or 'this device'} is busy with another job; wait for it or stop it first")
            jid = time.strftime("%Y%m%d-%H%M%S") + f"-{kind}" + (f"-{device}" if device else "")
            logf = self.dir / f"{jid}.log"
            meta = {"id": jid, "kind": kind, "label": label, "device": device, "args": args,
                    "started": util.now_iso(), "ended": None, "rc": None, "state": "running"}
            util.write_json(self._meta_path(jid), meta)
            out = open(logf, "wb")
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            full = (["--device", device] if device else []) + args
            p = subprocess.Popen([sys.executable, str(self.root / "bench"), *full], cwd=self.root,
                                 stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=env, start_new_session=True)
            out.close()
            self.procs[jid] = p
        threading.Thread(target=self._watch, args=(jid, p), daemon=True).start()
        return meta

    def _watch(self, jid, p):
        rc = p.wait()
        m = self.meta(jid) or {}
        m.update(ended=util.now_iso(), rc=rc, state="done" if rc == 0 else ("interrupted" if m.get("cancelled") else "failed"))
        util.write_json(self._meta_path(jid), m)

    def cancel(self, jid: str) -> None:
        p = self.procs.get(jid)
        if not p or p.poll() is not None:
            raise RuntimeError("job is not running")
        m = self.meta(jid)
        m["cancelled"] = True
        util.write_json(self._meta_path(jid), m)
        os.killpg(p.pid, signal.SIGINT)  # remote run: detaches; local run: session writes what it has

    def log_path(self, jid: str) -> Path:
        return self.dir / f"{jid}.log"


class App:
    def __init__(self, token: str):
        from .. import devices, remote
        self.devices_mod, self.remote_mod = devices, remote
        self.root = remote.ROOT
        data = devices.load()
        if data.get("results"):
            util.RESULTS = remote.local_results({"results": data["results"]})
        elif data["devices"]:
            util.RESULTS = remote.local_results({})
        self.token = token
        self.work = self.root / ".bbt-web"
        self.jobs = Jobs(self.root, self.work)

    @property
    def remote_mode(self) -> bool:
        return bool(self.devices_mod.load()["devices"])

    def device(self, dev_id: str | None) -> dict:
        d = self.devices_mod.get(dev_id)
        if not d:
            raise LookupError(f"no device {dev_id or '(default)'}")
        return d

    def remote_for(self, dev_id: str | None):
        d = self.device(dev_id)
        if d.get("kind") == "android":
            from ..android import Android
            return Android(d["serial"], d["id"])
        return self.remote_mod.Remote(self.devices_mod.conf(d))

    def device_list(self) -> dict:
        data = self.devices_mod.load()
        busy = {m.get("device") for m in self.jobs.running()}
        return {"default": data.get("default"),
                "devices": [{"id": d["id"], "name": d["name"], "kind": d.get("kind", "ssh"), "serial": d.get("serial"),
                             "user": d.get("user"), "host": d.get("host"),
                             "port": d.get("port", 22), "remote_dir": d.get("remote_dir", "bench"),
                             "notes": d.get("notes", ""), "custom_ssh": bool(d.get("ssh_opts")),
                             "busy": d["id"] in busy} for d in data["devices"]]}

    # -- helpers ---------------------------------------------------------------------
    def bench(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / "bench"), *args], cwd=self.root,
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, PYTHONUNBUFFERED="1"))

    def session_dir(self, name: str) -> Path:
        if not SESSION_NAME.match(name):
            raise LookupError("bad session name")
        d = util.RESULTS / name
        if not (d / "summary.json").exists():
            raise LookupError(f"no session {name}")
        return d

    def matrices_dir(self) -> Path:
        return self.root / "matrices"

    # -- data ------------------------------------------------------------------------
    def info(self):
        dl = self.device_list()
        return {"mode": "remote" if dl["devices"] else "local", "host": platform.node(),
                "default_device": dl["default"], "devices": dl["devices"],
                "results": str(util.RESULTS), "jobs": self.jobs.running()}

    def sessions(self):
        out = []
        if not util.RESULTS.is_dir():
            return out
        for d in sorted(util.RESULTS.iterdir(), reverse=True):
            f = d / "summary.json"
            if not f.exists():
                continue
            try:
                s = read_json(f)
            except ValueError:
                continue
            scen = []
            power = None
            for r in s["scenarios"]:
                pm = r.get("primary_metric")
                agg = r.get("aggregate", {}).get(pm or "", {})
                ok = [x for x in r["runs"] if not x["warmup"] and x["status"] == "ok"]
                if ok and not power:
                    power = ok[0]["metrics"].get("power_source")
                scen.append({"name": r["name"], "title": r["title"], "kind": r["kind"], "runs_ok": len(ok),
                             "runs": len([x for x in r["runs"] if not x["warmup"]]),
                             "primary_metric": pm, "mean": agg.get("mean"), "cv_pct": agg.get("cv_pct"),
                             "flags": len(r.get("flags", []))})
            model = None
            try:
                model = read_json(d / "session.json")["snapshot"]["system"].get("model")
            except (OSError, ValueError, KeyError):
                pass
            out.append({"name": d.name, "tag": s["tag"], "started": s["started"], "ended": s.get("ended"),
                        "device": s.get("device"), "model": model,
                        "power_source": power, "scenarios": scen,
                        "reports": sorted(p.name for p in d.glob("bbt_*.md"))})
        return out

    def session(self, name: str):
        d = self.session_dir(name)
        summary = read_json(d / "summary.json")
        meta = read_json(d / "session.json")
        for res in summary["scenarios"]:
            res["dir"] = slug(res["name"])
        return {"name": name, "summary": summary, "meta": meta,
                "reports": sorted(p.name for p in d.glob("bbt_*.md")),
                "files": sorted(str(p.relative_to(d)) for p in d.glob("*") if p.is_file())}

    def run_detail(self, name: str, scen_dir: str, run: str, points: int = 600):
        d = self.session_dir(name)
        if not re.match(r"^[A-Za-z0-9._-]+$", scen_dir) or not re.match(r"^(run|warmup)\d+$", run):
            raise LookupError("bad run")
        rd = d / scen_dir / run
        if not (rd / "metrics.json").exists():
            raise LookupError("no such run")
        m = read_json(rd / "metrics.json")
        out = {"metrics": m, "frames": None, "samples": None}
        log = mangohud.find_log(rd / "mangohud") if (rd / "mangohud").is_dir() else None
        if log is None and (rd / "frames.csv").exists():  # Android: Perfetto frame timeline, same columns
            log = rd / "frames.csv"
        if log:
            rows = [r for r in mangohud.parse(log)["rows"] if "frametime" in r and "elapsed" in r]
            if rows:
                e0 = rows[0]["elapsed"]
                t_end = (rows[-1]["elapsed"] - e0) / 1e9
                trim0, trim1 = (m.get("trim_s") or [0, 0])
                n = max(1, len(rows) // points)
                buckets = []
                for i in range(0, len(rows), n):
                    chunk = rows[i:i + n]
                    fts = [c["frametime"] for c in chunk]
                    buckets.append([round((chunk[0]["elapsed"] - e0) / 1e9, 3), round(sum(fts) / len(fts), 3),
                                    round(min(fts), 3), round(max(fts), 3)])
                out["frames"] = {"t_mean_min_max": buckets, "window": [trim0, round(t_end - trim1, 3)],
                                 "count": len(rows), "per_point": n}
        sp = rd / "samples.csv"
        if sp.exists():
            with open(sp, newline="") as f:
                rows = list(csv.DictReader(f))
            keep = [k for k in (rows[0].keys() if rows else []) if k == "t" or k.endswith(("_mhz", "_temp_c", "_load", "_busy"))
                    or k in ("system_w", "cpu_load", "gpu_busy", "fan_rpm", "battery_pct")]
            cols = {}
            for k in keep:
                vals = []
                for r in rows:
                    try:
                        vals.append(float(r[k]))
                    except (ValueError, TypeError, KeyError):
                        vals.append(None)
                cols[k] = vals
            marks = m.get("marks", {})
            win = None
            if "capture_start" in marks:
                c0 = marks["capture_start"] + ((m.get("trim_s") or [0, 0])[0])
                win = [c0, c0 + (m["metrics"].get("capture_s") or (marks.get("capture_end", c0) - c0))]
            out["samples"] = {"columns": cols, "window": win}
        return out


class Handler(http.server.BaseHTTPRequestHandler):
    app: App = None  # set by serve()
    server_version = "bbt-web"

    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("BBT_WEB_DEBUG"):
            super().log_message(fmt, *args)

    # -- plumbing --------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _authed(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)bbt_token=([A-Za-z0-9_-]+)", cookie)
        return bool(m) and secrets.compare_digest(m.group(1), self.app.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    # -- dispatch --------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        path = urllib.parse.unquote(url.path)
        # first visit: /?token=... -> cookie, then a clean URL
        if method == "GET" and "token" in q:
            if secrets.compare_digest(q["token"], self.app.token):
                # Lax, not Strict: a link opened from a chat app is a cross-site navigation,
                # and a Strict cookie would not go with the redirect that follows it.
                # Changes still need the X-BBT header, which other sites cannot send.
                return self._send(303, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"bbt_token={self.app.token}; Path=/; HttpOnly; SameSite=Lax"})
            return self._send(403, b"wrong token", "text/plain")
        if not self._authed():
            page = (b"<!doctype html><meta charset=utf-8><title>Barry Benchmark Tools</title>"
                    b"<body style='font-family:system-ui;margin:3rem'><h1>Not signed in</h1>"
                    b"<p>Open the link that <code>bench web</code> printed in its terminal "
                    b"(it ends in <code>?token=...</code>).</p>")
            return self._send(401, page, "text/html; charset=utf-8") if not path.startswith("/api/") \
                else self._err(401, "not signed in: open the link printed by `bench web`")
        if method != "GET" and self.headers.get("X-BBT") != "1":
            return self._err(403, "missing X-BBT header")
        try:
            if path.startswith("/api/"):
                return self._api(method, path[5:], q)
            return self._static(path)
        except LookupError as e:
            return self._err(404, str(e))
        except (ValueError, RuntimeError) as e:
            return self._err(400, str(e))
        except BrokenPipeError:
            pass

    def _static(self, path):
        if path in ("/", "/index.html"):
            f = STATIC / "index.html"
        else:
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        return self._send(200, f.read_bytes(), ctype)

    def _api(self, method, p, q):
        app = self.app
        parts = [x for x in p.split("/") if x]
        if method == "GET" and parts == ["info"]:
            return self._json(app.info())
        if method == "GET" and parts == ["sessions"]:
            return self._json(app.sessions())
        if parts[:1] == ["sessions"] and len(parts) >= 2:
            name = parts[1]
            if method == "GET" and len(parts) == 2:
                return self._json(app.session(name))
            if method == "DELETE" and len(parts) == 2:
                d = app.session_dir(name)
                shutil.rmtree(d)
                return self._json({"deleted": name})
            if method == "GET" and len(parts) == 5 and parts[2] == "runs":
                return self._json(app.run_detail(name, parts[3], parts[4]))
            if method == "GET" and len(parts) >= 4 and parts[2] == "files":
                d = app.session_dir(name)
                f = (d / "/".join(parts[3:])).resolve()
                if d.resolve() not in f.parents or not f.is_file():
                    raise LookupError("no such file")
                ctype = {".md": "text/markdown", ".csv": "text/csv", ".json": "application/json",
                         ".log": "text/plain", ".txt": "text/plain", ".yaml": "text/plain",
                         ".conf": "text/plain"}.get(f.suffix, "application/octet-stream")
                hdr = {"Content-Disposition": f'attachment; filename="{f.name}"'} if q.get("download") else {}
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8", hdr)
        if method == "GET" and parts == ["compare"]:
            from ..compare import compare_data
            try:
                return self._json(compare_data(q.get("a", ""), q.get("b", "")))
            except SystemExit as e:
                raise LookupError(str(e))
        if parts[:1] == ["matrices"]:
            md = app.matrices_dir()
            if method == "GET" and len(parts) == 1:
                return self._json(sorted(p.name for p in md.glob("*.y*ml")))
            if method == "GET" and parts == ["matrices", "kinds"]:  # which devices can run each matrix
                from ..config import scenario_kinds
                return self._json({"matrices": {p.name: scenario_kinds(p) for p in sorted(md.glob("*.y*ml"))},
                                   "devices": {k: sorted(v) for k, v in app.devices_mod.RUNS.items()}})
            if len(parts) == 2:
                if not MATRIX_NAME.match(parts[1]):
                    raise ValueError("matrix names: letters, digits, . _ - and .yaml")
                f = md / parts[1]
                if method == "GET":
                    if not f.exists():
                        raise LookupError("no such matrix")
                    return self._json({"name": f.name, "text": f.read_text()})
                if method == "PUT":
                    text = self._body().get("text", "")
                    if not isinstance(text, str) or len(text) > 200_000:
                        raise ValueError("bad matrix text")
                    md.mkdir(exist_ok=True)
                    tmp = f.with_suffix(f.suffix + ".tmp")
                    tmp.write_text(text)
                    tmp.replace(f)
                    return self._json({"saved": f.name})
            if method == "POST" and len(parts) == 3 and parts[2] == "check":
                if not MATRIX_NAME.match(parts[1]) or not (md / parts[1]).exists():
                    raise LookupError("no such matrix")
                dev = self._body().get("device")
                pre = ["--device", app.device(dev)["id"]] if (dev and app.remote_mode) else []
                r = app.bench(*pre, "run", str(md / parts[1]), "--tag", "check", "--dry-run", timeout=90)
                txt = r.stdout.strip()
                start = txt.find("{")
                if r.returncode != 0 or start < 0:
                    return self._json({"ok": False, "error": (r.stderr or r.stdout).strip()[-2000:]})
                try:
                    return self._json({"ok": True, "matrix": json.loads(txt[start:])})
                except ValueError:
                    return self._json({"ok": False, "error": txt[-2000:]})
        if parts[:1] == ["jobs"]:
            if method == "GET" and len(parts) == 1:
                return self._json({"running": app.jobs.running(), "recent": app.jobs.list()})
            if method == "POST" and len(parts) == 1:
                return self._json(self._start_job(self._body()))
            if len(parts) >= 2:
                meta = app.jobs.meta(parts[1])
                if not meta:
                    raise LookupError("no such job")
                if method == "GET" and len(parts) == 2:
                    return self._json(meta)
                if method == "GET" and parts[2:] == ["log"]:
                    return self._stream_log(parts[1])
                if method == "POST" and parts[2:] == ["cancel"]:
                    app.jobs.cancel(parts[1])
                    return self._json({"cancelled": parts[1]})
        if parts[:1] == ["devices"]:
            dm = app.devices_mod
            if method == "GET" and len(parts) == 1:
                return self._json(app.device_list())
            if method == "POST" and len(parts) == 1:
                b = self._body()
                try:
                    d = dm.add(b, allow_ssh_opts=False)
                except dm.DeviceError as e:
                    raise ValueError(str(e))
                return self._json({"device": d["id"], "probe": app.remote_for(d["id"]).probe()})
            if len(parts) >= 2:
                dev = app.device(parts[1])
                if method == "PUT" and len(parts) == 2:
                    b = dict(self._body(), id=dev["id"])
                    try:
                        dm.add(b, allow_ssh_opts=False, replace=True)
                    except dm.DeviceError as e:
                        raise ValueError(str(e))
                    return self._json({"saved": dev["id"]})
                if method == "DELETE" and len(parts) == 2:
                    if any(m.get("device") == dev["id"] for m in app.jobs.running()):
                        raise RuntimeError("that device has a job running")
                    dm.remove(dev["id"])
                    return self._json({"removed": dev["id"]})
                if method == "POST" and parts[2:] == ["default"]:
                    dm.set_default(dev["id"])
                    return self._json({"default": dev["id"]})
                if method == "POST" and parts[2:] == ["probe"]:
                    return self._json(app.remote_for(dev["id"]).probe())
                if method == "GET" and parts[2:] == ["status"]:
                    r = app.remote_for(dev["id"])
                    probe = r.probe()
                    if not probe["ok"]:
                        return self._json({"device": dev["id"], "online": False, "probe": probe})
                    return self._json({"device": dev["id"], "online": True, "probe": probe,
                                       "running": r.running_units(), "unpulled": r.remote_sessions()})
                if method == "GET" and parts[2:] == ["snapshot"]:
                    r = app.bench("--device", dev["id"], "snapshot", timeout=90)
                    if r.returncode != 0:
                        raise RuntimeError((r.stderr or r.stdout).strip()[-500:] or "snapshot failed")
                    return self._json(json.loads(r.stdout[r.stdout.find("{"):]))
                if method == "GET" and parts[2:] == ["sample"]:
                    return self._stream_sample(float(q.get("seconds", 300)), dev["id"])
        if parts[:1] == ["local"] and method == "GET":   # web app running on the device itself
            if parts[1:] == ["snapshot"]:
                r = app.bench("snapshot", timeout=90)
                return self._json(json.loads(r.stdout[r.stdout.find("{"):]))
            if parts[1:] == ["sample"]:
                return self._stream_sample(float(q.get("seconds", 300)), None)
        return self._err(404, "no such endpoint")

    def _start_job(self, b: dict) -> dict:
        kind = b.get("kind")
        app = self.app
        dev = None
        if app.remote_mode:
            dev = app.device(b.get("device"))["id"]
        elif kind in ("attach", "stop", "pull", "deploy"):
            raise ValueError(f"{kind} needs a registered device")
        where = f" on {dev}" if dev else ""
        if kind == "run":
            name = b.get("matrix", "")
            tag = (b.get("tag") or "").strip()
            if not MATRIX_NAME.match(name) or not (app.matrices_dir() / name).exists():
                raise ValueError("pick a matrix")
            if not TAG.match(tag):
                raise ValueError("tag: letters, digits, space . _ - (max 60)")
            from ..config import scenario_kinds
            kinds = scenario_kinds(app.matrices_dir() / name)
            d = app.device(dev) if dev else None
            if not app.devices_mod.can_run(d, kinds):
                where_ = d["name"] if d else "this device"
                raise ValueError(f"{name} has {', '.join(kinds) or 'no'} scenarios, which {where_} cannot run")
            args = ["run", f"matrices/{name}", "--tag", tag]
            for k in ("runs", "warmup"):
                if b.get(k) not in (None, ""):
                    v = int(b[k])
                    if not 0 <= v <= 50:
                        raise ValueError(f"{k} out of range")
                    args += [f"--{k}", str(v)]
            only = [s for s in (b.get("only") or []) if isinstance(s, str) and re.match(r"^[\w.-]+$", s)]
            if only:
                args += ["--only", *only]
            return app.jobs.start("run", args, f"run {name} as '{tag}'{where}", dev)
        simple = {"attach": (["attach"], "follow the device run"),
                  "stop": (["stop"], "stop the device run"),
                  "pull": (["pull"], "fetch finished sessions"),
                  "deploy": (["deploy"], "update the harness"),
                  "setup": (["setup"], "install vkmark")}
        if kind in simple:
            args, label = simple[kind]
            return app.jobs.start(kind, args, label + where, dev)
        raise ValueError("unknown job kind")

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse(self, event: str, data) -> None:
        payload = data if isinstance(data, str) else json.dumps(data)
        lines = "".join(f"data: {l}\n" for l in payload.split("\n"))
        self.wfile.write(f"event: {event}\n{lines}\n".encode())
        self.wfile.flush()

    def _stream_log(self, jid):
        path = self.app.jobs.log_path(jid)
        self._sse_start()
        off, idle = 0, 0
        try:
            while True:
                chunk = b""
                if path.exists():
                    with open(path, "rb") as f:
                        f.seek(off)
                        chunk = f.read()
                meta = self.app.jobs.meta(jid) or {}
                if chunk:
                    # whole lines while running; everything once the job is over
                    cut = len(chunk) if meta.get("state") != "running" else chunk.rfind(b"\n") + 1
                    if cut:
                        self._sse("log", chunk[:cut].decode(errors="replace").rstrip("\n"))
                        off += cut
                    chunk = chunk[:cut]
                if meta.get("state") != "running" and not chunk:
                    self._sse("end", meta)
                    return
                idle = idle + 1 if not chunk else 0
                if idle % 30 == 29:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_sample(self, seconds: float, device: str | None):
        seconds = max(5.0, min(seconds, 3600.0))
        pre = ["--device", device] if device else []
        p = subprocess.Popen([sys.executable, str(self.app.root / "bench"), *pre, "sample", str(seconds), "--json"],
                             cwd=self.app.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                             start_new_session=True)
        self._sse_start()
        try:
            for line in p.stdout:
                line = line.decode(errors="replace").strip()
                if line.startswith("{"):
                    self._sse("sample", line)
            self._sse("end", {})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    tok_file = Path(__file__).resolve().parent.parent.parent / ".bbt-web" / "token"
    tok_file.parent.mkdir(parents=True, exist_ok=True)
    if tok_file.exists():
        token = tok_file.read_text().strip()
    else:
        token = secrets.token_urlsafe(24)
        tok_file.write_text(token)
        os.chmod(tok_file, 0o600)
    Handler.app = App(token)
    srv = Server((host, port), Handler)
    shown = "localhost" if host in ("127.0.0.1", "localhost") else (host if host not in ("0.0.0.0", "::") else _lan_ip())
    url = f"http://{shown}:{srv.server_address[1]}/?token={token}"
    print(f"Barry Benchmark Tools web app: {url}", flush=True)
    if host in ("0.0.0.0", "::"):
        print("Listening on all interfaces: anyone on the network with this link can start runs.", flush=True)
    print("Ctrl-C to quit (a run on the device keeps going).", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"
