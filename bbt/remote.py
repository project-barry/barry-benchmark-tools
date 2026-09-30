"""Remote mode: drive the harness on the device from another machine.

Active when a `remote.conf` sits next to `bench` (or BBT_TARGET is set). Then
`bench run` deploys the harness, starts the session on the device as a
systemd user unit (it survives SSH drops and a local Ctrl-C), streams its log,
and pulls the finished session folder into the local results directory.
The device copy is deleted only after the pulled copy checks out (same file
count and bytes). compare and list work on the local results; the other
commands run on the device.

remote.conf (key=value, # comments):
    target=steamos@<device>          # ssh destination (the user that runs Steam)
    ssh_opts=-o SomeOption=value     # extra ssh options (optional)
    results=results                  # local results folder, relative to bench (optional)
    remote_dir=bench                 # harness folder on the device, relative to ~ (optional)
"""
from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from .util import log

ROOT = Path(__file__).resolve().parent.parent          # the folder holding `bench`
DEPLOY = ["bench", "bbt", "bin", "matrices", "README.md"]
SKIP = {"__pycache__", ".DS_Store"}


def load_conf() -> dict | None:
    conf = {}
    f = ROOT / "remote.conf"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    if os.environ.get("BBT_TARGET"):
        conf["target"] = os.environ["BBT_TARGET"]
    if os.environ.get("BBT_SSH_OPTS"):
        conf["ssh_opts"] = os.environ["BBT_SSH_OPTS"]
    return conf if conf.get("target") else None


def local_results(conf: dict) -> Path:
    p = Path(os.path.expanduser(os.environ.get("BBT_RESULTS") or conf.get("results", "results")))
    return p if p.is_absolute() else ROOT / p


class Remote:
    def __init__(self, conf: dict):
        self.target = conf["target"]
        self.rdir = conf.get("remote_dir", "bench").strip("/")
        ctl = Path.home() / ".ssh" / "bbt-%C"
        self.ssh_base = ["ssh", *shlex.split(os.path.expanduser(conf.get("ssh_opts", ""))),
                         "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                         "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                         "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}", "-o", "ControlPersist=120"]
        self.results = local_results(conf)

    # -- plumbing ------------------------------------------------------------------
    def ssh(self, cmd: str, *, capture=True, check=True, input=None, timeout=None, tty=False):
        args = self.ssh_base + (["-t"] if tty else []) + [self.target, cmd]
        p = subprocess.run(args, capture_output=capture, input=input, timeout=timeout)
        if check and p.returncode != 0:
            err = (p.stderr or b"").decode(errors="replace").strip() if capture else ""
            raise SystemExit(f"ssh {self.target} failed ({p.returncode}): {err or cmd}")
        return p

    def out(self, cmd: str, **kw) -> str:
        return self.ssh(cmd, **kw).stdout.decode(errors="replace").strip()

    def home_cmd(self, cmd: str) -> str:
        return f"cd ~/{self.rdir} && {cmd}"

    # -- deploy --------------------------------------------------------------------
    def deploy(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            def filt(ti: tarfile.TarInfo):
                if any(part in SKIP for part in Path(ti.name).parts):
                    return None
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                ti.pax_headers = {}  # no macOS xattrs / provenance on the device
                return ti
            for name in DEPLOY:
                if (ROOT / name).exists():
                    tf.add(ROOT / name, arcname=name, filter=filt)
        self.ssh(f"mkdir -p ~/{self.rdir} && tar -xf - -C ~/{self.rdir} --no-same-owner && "
                 f"chmod +x ~/{self.rdir}/bench ~/{self.rdir}/bin/*", input=buf.getvalue())
        log(f"deployed to {self.target}:~/{self.rdir}")

    # -- sessions on the device ------------------------------------------------------
    def running_units(self) -> list[str]:
        txt = self.out("systemctl --user list-units --plain --no-legend 'bbt-run-*' 2>/dev/null || true")
        return [l.split()[0].removesuffix(".service") for l in txt.splitlines() if l.strip() and "running" in l]

    def remote_sessions(self) -> list[str]:
        """Finished sessions (summary.json written) still on the device."""
        txt = self.out(f"cd ~/{self.rdir}/results 2>/dev/null && ls -1d */summary.json 2>/dev/null || true")
        return sorted(l.split("/")[0] for l in txt.splitlines() if l.strip())

    def pull(self, name: str, keep: bool = False) -> Path:
        self.results.mkdir(parents=True, exist_ok=True)
        rq = shlex.quote(name)
        stats = self.out(f"cd ~/{self.rdir}/results && find {rq} -type f -printf '%s\\n' | "
                         f"awk '{{n++; b+=$1}} END {{print n+0, b+0}}'")
        want_n, want_b = map(int, stats.split())
        p = self.ssh(f"tar -C ~/{self.rdir}/results -cf - {rq}")
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
            tf.extractall(self.results, filter="data")
        dst = self.results / name
        files = [f for f in dst.rglob("*") if f.is_file()]
        got_n, got_b = len(files), sum(f.stat().st_size for f in files)
        if (got_n, got_b) != (want_n, want_b):
            raise SystemExit(f"pulled copy of {name} does not match the device "
                             f"({got_n} files / {got_b} B vs {want_n} / {want_b}); device copy kept")
        if not keep:
            self.ssh(f"rm -rf ~/{self.rdir}/results/{rq}")
        log(f"pulled {name} -> {dst} ({got_n} files){'' if keep else ', removed from the device'}")
        return dst

    # -- run -----------------------------------------------------------------------
    def start(self, matrix: Path, tag: str, extra: list[str]) -> tuple[str, str]:
        if self.running_units():
            raise SystemExit(f"a session is already running on the device: {self.running_units()} "
                             "(bench attach / bench stop)")
        unit = f"bbt-run-{time.strftime('%Y%m%d-%H%M%S')}"
        rmatrix = f"state/matrices/{matrix.name}"
        self.ssh(self.home_cmd(f"mkdir -p state/matrices state/runs && cat > {shlex.quote(rmatrix)}"),
                 input=matrix.read_bytes())
        logf = f"state/runs/{unit}.log"
        inner = " ".join(shlex.quote(a) for a in
                         ["./bench", "run", rmatrix, "--tag", tag, "--matrix-label", str(matrix.name), *extra])
        script = f"{inner} > {logf} 2>&1; echo $? > {logf}.rc"
        self.ssh(f"systemd-run --user --quiet --unit={unit} --working-directory=\"$HOME/{self.rdir}\" "
                 f"/bin/bash -c {shlex.quote(script)}")
        log(f"started {unit} on {self.target}")
        return unit, logf

    def attach(self, unit: str, logf: str) -> int | None:
        """Stream the log until the unit ends; returns the exit code (None on detach)."""
        off, fails = 0, 0
        rlog = f"~/{self.rdir}/{logf}"
        try:
            while True:
                try:
                    p = self.ssh(f"tail -c +{off + 1} {rlog} 2>/dev/null; echo; echo __BBT__ "
                                 f"$(systemctl --user is-active {unit} 2>/dev/null) $(cat {rlog}.rc 2>/dev/null)",
                                 check=False, timeout=60)
                except subprocess.TimeoutExpired:
                    p = None
                if p is None or p.returncode == 255:
                    fails += 1
                    log(f"lost the connection ({fails}); the run continues on the device, retrying")
                    time.sleep(min(60, 5 * fails))
                    continue
                fails = 0
                text = p.stdout.decode(errors="replace")
                body, _, status = text.rpartition("__BBT__")
                body = body[:-1] if body.endswith("\n") else body   # the echo's newline
                if body:
                    sys.stderr.write(body)
                    sys.stderr.flush()
                    off += len(body.encode())
                parts = status.split()
                state = parts[0] if parts else ""
                if state not in ("active", "activating") and len(parts) > 1:
                    return int(parts[1])
                if state not in ("active", "activating") and not parts[1:]:
                    time.sleep(2)  # unit gone, rc not written yet or run killed
                    rc = self.out(f"cat {rlog}.rc 2>/dev/null || true")
                    if rc.strip().lstrip("-").isdigit():
                        return int(rc)
                    log(f"{unit} ended without an exit code; its journal:")
                    sys.stderr.write(self.out(f"journalctl --user -u {unit} --no-pager -n 15 -o cat || true") + "\n")
                    return -1
                time.sleep(3)
        except KeyboardInterrupt:
            log(f"detached; the run continues on the device. `bench attach` to follow it, `bench stop` to end it")
            return None

    def session_of(self, logf: str) -> str | None:
        txt = self.out(f"grep -m1 -oE 'session [^ :]+' ~/{self.rdir}/{logf} 2>/dev/null || true")
        return txt.split()[1] if txt.startswith("session ") else None

    def latest_unit(self) -> tuple[str, str] | None:
        running = self.running_units()
        if running:
            return running[0], f"state/runs/{running[0]}.log"
        name = self.out(f"ls -1t ~/{self.rdir}/state/runs/*.log 2>/dev/null | head -1")
        if name:
            unit = Path(name).name[:-4]
            return unit, f"state/runs/{unit}.log"
        return None


# ---- command handlers (called from cli in remote mode) ------------------------------

def finish(r: Remote, unit: str, logf: str, rc: int | None, keep: bool) -> None:
    if rc is None:
        return
    name = r.session_of(logf)
    if rc != 0:
        log(f"remote run exited {rc}")
    if name and name in r.remote_sessions():
        dst = r.pull(name, keep=keep)
        # the run's console log goes with the results; nothing is left behind
        (dst / "remote-run.log").write_bytes(r.ssh(f"cat ~/{r.rdir}/{logf}").stdout)
        r.ssh(f"rm -f ~/{r.rdir}/{logf} ~/{r.rdir}/{logf}.rc")
        for rep in sorted(dst.glob("bbt_*.md")):
            print(rep)
    elif name:
        log(f"session {name} has no summary on the device (run failed early?); "
            f"log kept on the device: ~/{r.rdir}/{logf}")
    else:
        log(f"the run did not start a session; log kept on the device: ~/{r.rdir}/{logf}")


def cmd_run(r: Remote, a) -> None:
    matrix = Path(a.matrix)
    if not matrix.exists():
        raise SystemExit(f"{matrix} not found")
    if not a.no_deploy:
        r.deploy()
    extra = []
    if a.only:
        extra += ["--only", *a.only]
    if a.runs is not None:
        extra += ["--runs", str(a.runs)]
    if a.warmup is not None:
        extra += ["--warmup", str(a.warmup)]
    if a.dry_run:
        extra.append("--dry-run")
        rm = f"state/matrices/{matrix.name}"
        r.ssh(r.home_cmd(f"mkdir -p state/matrices && cat > {shlex.quote(rm)}"), input=matrix.read_bytes())
        r.ssh(r.home_cmd(" ".join(shlex.quote(x) for x in ["./bench", "run", rm, "--tag", a.tag, *extra])),
              capture=False)
        return
    unit, logf = r.start(matrix, a.tag, extra)
    finish(r, unit, logf, r.attach(unit, logf), a.keep_remote)


def cmd_attach(r: Remote, a) -> None:
    lu = r.latest_unit()
    if not lu:
        raise SystemExit("no remote run found")
    finish(r, *lu, r.attach(*lu), a.keep_remote)


def cmd_stop(r: Remote, a) -> None:
    units = r.running_units()
    if not units:
        print("nothing running on the device")
        return
    for u in units:
        r.ssh(f"systemctl --user stop {u}")  # SIGTERM: the session writes what it measured
        log(f"stopped {u}")
    cmd_pull(r, a)


def cmd_pull(r: Remote, a) -> None:
    names = r.remote_sessions()
    if not names:
        print("no finished sessions on the device")
    for n in names:
        r.pull(n, keep=a.keep_remote)


def cmd_status(r: Remote, a) -> None:
    print(f"device:   {r.target}:~/{r.rdir}")
    print(f"running:  {', '.join(r.running_units()) or 'nothing'}")
    print(f"on device (not pulled yet): {', '.join(r.remote_sessions()) or 'none'}")
    print(f"local results: {r.results}")


def passthrough(r: Remote, argv: list[str], tty: bool = False) -> None:
    cmd = r.home_cmd(" ".join(shlex.quote(x) for x in ["./bench", *argv]))
    rc = r.ssh(cmd, capture=False, check=False, tty=tty).returncode
    if rc:
        raise SystemExit(rc)
