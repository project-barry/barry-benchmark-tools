"""adb transport for Android devices (driven from this machine, no harness on the device).

A device is registered by its hardware serial (`getprop ro.serialno`, the part
after "adb-" in a wireless-debugging name). Wireless debugging picks a new port
whenever it restarts, so the serial is looked up among the connected devices
each time; when it is not connected, the device's mDNS service is tried.

Everything here runs as adb's `shell` user: no root needed, and nothing is
written outside /data/local/tmp/bbt and /data/misc/perfetto-traces.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{4,64}$")
MISSING = "\x01"  # marks a file that could not be read


class AdbError(RuntimeError):
    pass


def adb_bin() -> str:
    for p in (shutil.which("adb"), "/opt/homebrew/bin/adb", "/usr/local/bin/adb",
              str(Path.home() / "Library/Android/sdk/platform-tools/adb")):
        if p and Path(p).exists():
            return p
    raise AdbError("adb not found: brew install --cask android-platform-tools")


def _run(args: list[str], timeout: float = 30, input: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([adb_bin(), *args], capture_output=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb {' '.join(args[:3])} timed out after {timeout:.0f} s")


def connected() -> list[str]:
    """Transports in the 'device' state (USB serials, ip:port, mDNS names)."""
    out = _run(["devices"], timeout=15).stdout.decode(errors="replace")
    return [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("\tdevice")]


class Adb:
    def __init__(self, serial: str):
        if not SERIAL_RE.match(serial or ""):
            raise AdbError(f"bad adb serial {serial!r}")
        self.serial = serial
        self._t: str | None = None

    # -- finding the device ----------------------------------------------------------
    def transport(self) -> str:
        if self._t and self._t in connected():
            return self._t
        self._t = self._find() or self._mdns_connect()
        if not self._t:
            raise AdbError(f"device {self.serial} is not connected over adb: on the device, open Developer "
                           "options > Wireless debugging (on), then `adb connect IP:PORT` with the port shown there")
        return self._t

    def _find(self) -> str | None:
        ts = connected()
        for t in ts:  # cheap: the name already says which device it is
            if t == self.serial or t.startswith(f"adb-{self.serial}-"):
                return t
        for t in ts:
            p = _run(["-s", t, "shell", "getprop ro.serialno"], timeout=10)
            if p.stdout.decode(errors="replace").strip() == self.serial:
                return t
        return None

    def _mdns_connect(self) -> str | None:
        out = _run(["mdns", "services"], timeout=15).stdout.decode(errors="replace")
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 3 and f[0].startswith(f"adb-{self.serial}-") and "_adb-tls-connect" in f[1]:
                _run(["connect", f[2]], timeout=15)
                time.sleep(1)
                return self._find()
        return None

    # -- commands ------------------------------------------------------------------------
    def run(self, args: list[str], timeout: float = 30, input: bytes | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        p = _run(["-s", self.transport(), *args], timeout=timeout, input=input)
        if check and p.returncode != 0:
            err = (p.stderr or p.stdout).decode(errors="replace").strip()
            raise AdbError(f"adb {' '.join(args)[:80]} failed ({p.returncode}): {err[-300:]}")
        return p

    def sh(self, cmd: str, timeout: float = 30, check: bool = False) -> str:
        """Run a shell command on the device, return stdout ('' on failure unless check)."""
        p = self.run(["shell", cmd], timeout=timeout, check=False)
        if check and p.returncode != 0:
            raise AdbError(f"`{cmd[:80]}` failed ({p.returncode}): "
                           f"{(p.stderr or p.stdout).decode(errors='replace').strip()[-300:]}")
        return p.stdout.decode(errors="replace").rstrip("\n")

    def popen(self, cmd: str) -> subprocess.Popen:
        return subprocess.Popen([adb_bin(), "-s", self.transport(), "shell", cmd], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    def push_text(self, text: str, path: str) -> None:
        self.run(["shell", f"cat > {shlex.quote(path)}"], input=text.encode())

    def pull(self, remote: str, local: Path, timeout: float = 120) -> None:
        self.run(["pull", remote, str(local)], timeout=timeout)

    def getprops(self) -> dict[str, str]:
        return dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", self.sh("getprop", timeout=20), re.M))

    def cat(self, paths: list[str]) -> dict[str, str | None]:
        """First line of each file (None if unreadable), in one round trip.

        The shell's `read` gets only the first byte of /proc/sys files (they
        do not support reads at an offset), so those go through head."""
        if not paths:
            return {}
        script = ("for f in " + " ".join(shlex.quote(p) for p in paths) + "; do v=; case \"$f\" in "
                  "/proc/sys/*) v=$(head -n1 \"$f\" 2>/dev/null) || v=" + MISSING + ";; "
                  "*) IFS= read -r v < \"$f\" 2>/dev/null || [ -n \"$v\" ] || v=" + MISSING + ";; "
                  "esac; echo \"$v\"; done")
        lines = self.sh(script, timeout=30).split("\n")
        return {p: (None if i >= len(lines) or lines[i] == MISSING else lines[i].strip()) for i, p in enumerate(paths)}
