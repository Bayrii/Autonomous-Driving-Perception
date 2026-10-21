"""Public HTTPS address for the PC server, so the phone does not need the same Wi-Fi.

Uses an SSH reverse tunnel to localhost.run (Windows 10/11, macOS and Linux ship
`ssh`; nothing to install, no account). The PC only makes an OUTGOING connection,
so firewalls and Wi-Fi client isolation do not matter, and the phone can be on
mobile data. The service terminates TLS with a valid certificate, so the browser
treats the page as a secure context (camera + motion sensors allowed).

Privacy: traffic, including camera frames, passes through the tunnel provider.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger("pc_app.tunnel")

# localhost.run prints e.g. "abc123.lhr.life tunneled with tls termination, https://abc123.lhr.life"
_URL_RE = re.compile(r"https://([a-z0-9-]+\.)+(lhr\.life|lhrtunnel\.link)\b", re.I)


class SshTunnel:
    def __init__(self, local_port: int, host: str = "localhost.run"):
        self.local_port = local_port
        self.host = host
        self.url: str | None = None
        self.proc: subprocess.Popen | None = None
        self._found = threading.Event()
        self.output: list[str] = []

    def start(self, timeout_s: float = 25.0) -> str:
        ssh = shutil.which("ssh")
        if ssh is None:
            raise RuntimeError("`ssh` not found (Windows: Settings > Optional features > OpenSSH Client)")
        cmd = [ssh, "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=20",
               # 127.0.0.1, not "localhost": ssh may resolve that to ::1 while the server listens on IPv4
               "-o", "ExitOnForwardFailure=yes", "-R", f"80:127.0.0.1:{self.local_port}", f"nokey@{self.host}"]
        log.info("opening tunnel: %s", " ".join(cmd[1:]))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, name="tunnel-output", daemon=True).start()
        if not self._found.wait(timeout_s):
            self.stop()
            tail = "\n".join(self.output[-10:])
            raise RuntimeError(f"tunnel did not report a public URL within {timeout_s:.0f} s:\n{tail}")
        return self.url

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip()
            self.output.append(line)
            m = _URL_RE.search(line)
            if m and not self._found.is_set():
                self.url = m.group(0)
                self._found.set()
        if self._found.is_set():
            log.warning("tunnel closed (ssh exited); restart `python -m pc_app live --tunnel` for a new link")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        time.sleep(0.1)
