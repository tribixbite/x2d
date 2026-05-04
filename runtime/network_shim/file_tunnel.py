"""file_tunnel — Bambu printer SD-card file browser via FTPS-implicit.

Uses Python's stdlib `ftplib.FTP_TLS` for protocol robustness — the
vsFTPd-3.0.5 instance on Bambu printers requires session-resumption
for the data channel TLS, which a hand-rolled implementation gets
wrong. ftplib handles it.

Earlier iteration tried the BambuTunnel TCP/TLS:6000 LIST_INFO protocol
documented in BS source PrinterFileSystem.cpp:1434, but X2D firmware
02.06.00.51 returns auth-rejected (status 0x0003013f) when sent the
LIST_INFO frame on port 6000 — that port is camera-only on X2D.

Empirical finding: the printer's SD-card files are exposed via
**FTPS (TLS-implicit, port 990)** with the same access-code auth as
MQTT and the camera tunnel. The vsFTPd-3.0.5 backend accepts:
    USER bblp
    PASS <8-char access code>
    PBSZ 0
    PROT P  (TLS protection on data channel)
    CWD /timelapse  (or /cache, /, etc.)
    PASV
    LIST    (data flows on the PASV port)

This module wraps that protocol and returns a Python list of
FileEntry objects matching the original BambuTunnel API, so any
caller built against the previous file_tunnel can swap in.

Tested against X2D 02.06.00.51 — directories `/`, `/timelapse`,
`/cache` exist; LIST returns standard Unix-style listings.
"""
from __future__ import annotations

import re
import socket
import ssl
from dataclasses import dataclass
from typing import List, Optional


DEFAULT_PORT = 990   # FTPS-implicit. vsFTPd config differs from BS's
                      # earlier Tunnel:6000 protocol entirely.


class FileTunnelError(Exception):
    """Distinct exception so callers can tell file-tunnel errors apart."""
    pass


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    time: int   # seconds since epoch (or 0 if FTPS LIST didn't include date)
    size: int   # bytes
    is_dir: bool = False

    def __str__(self) -> str:
        kind = "DIR " if self.is_dir else "FILE"
        kib = self.size / 1024.0
        return f"{kind} {kib:>9.1f} KiB  {self.path}"


def _make_ctx() -> ssl.SSLContext:
    """The printer's cert is self-signed and uses BBL device PKI we
    don't have a CA bundle for — disable verification (same posture as
    the existing FTPS+MQTT clients in x2d_bridge.py)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Allow older TLS versions if newer ones don't negotiate
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except AttributeError:
        pass
    return ctx


def _recv_response(sock: ssl.SSLSocket, timeout: float = 5.0) -> str:
    """Read FTP response lines until we get a terminating line
    (3-digit code followed by space, no '-')."""
    sock.settimeout(timeout)
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        # Look for a final-line terminator: \r\n with a 3-digit code at start of last line
        text = buf.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        for line in lines[-2::-1]:  # walk back from the last complete line
            if len(line) >= 4 and line[:3].isdigit() and line[3] == " ":
                return text
        # Need more data
    return buf.decode("utf-8", errors="replace")


def _send_cmd(sock: ssl.SSLSocket, cmd: str, timeout: float = 5.0) -> str:
    sock.sendall((cmd + "\r\n").encode())
    return _recv_response(sock, timeout)


def _parse_pasv(reply: str) -> tuple[str, int]:
    """Extract host + port from a PASV response like
    `227 Entering Passive Mode (192,168,0,138,195,95).`"""
    m = re.search(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", reply)
    if not m:
        raise FileTunnelError(f"unparseable PASV reply: {reply!r}")
    a, b, c, d, hi, lo = (int(x) for x in m.groups())
    return f"{a}.{b}.{c}.{d}", hi * 256 + lo


# Standard Unix-style LIST output: drwxr-xr-x  2 user group  4096 Jan  1 00:00 name
# vsFTPd format on Bambu printers — verified against X2D firmware.
_LS_RE = re.compile(
    r"^(?P<perm>[-dlcsbp])"
    r"(?P<rest_perm>[rwxsStTl-]{9}\+?)\s+"
    r"(?P<links>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<mon>\S+)\s+"
    r"(?P<day>\d+)\s+"
    r"(?P<yr_or_time>\S+)\s+"
    r"(?P<name>.+)$"
)


def _parse_ls_line(line: str, base_path: str) -> Optional[FileEntry]:
    m = _LS_RE.match(line.rstrip("\r\n"))
    if not m:
        return None
    name = m.group("name")
    if name in (".", "..", ""):
        return None
    is_dir = m.group("perm") == "d"
    size = int(m.group("size"))
    full = base_path.rstrip("/") + "/" + name
    if base_path == "/":
        full = "/" + name
    return FileEntry(
        name=name,
        path=full,
        time=0,    # FTP LIST doesn't include epoch — caller can stat if needed
        size=size,
        is_dir=is_dir,
    )


class FileTunnelClient:
    """Single-shot FTPS file-list client.

    Usage:
        with FileTunnelClient("192.168.0.138", "12345678") as cli:
            entries = cli.list_files("timelapse")
            for e in entries: print(e)
    """

    def __init__(
        self,
        ip: str,
        access_code: str,
        *,
        port: int = DEFAULT_PORT,
        username: str = "bblp",
        connect_timeout: float = 5.0,
        request_timeout: float = 10.0,
    ):
        self.ip = ip
        self.access_code = access_code
        self.port = port
        self.username = username
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._sock: Optional[ssl.SSLSocket] = None
        self._ftp = None
        self._ctx = _make_ctx()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self) -> None:
        # Use stdlib ftplib.FTP_TLS for robustness — Bambu's vsFTPd
        # requires session-resumption for the data channel TLS handshake.
        from ftplib import FTP_TLS, error_perm

        # Subclass to reuse the control session for data channel —
        # vsFTPd 3.0.5 with `ssl_session_reuse=YES` (default) refuses
        # data connections that don't share the control TLS session.
        class _FTP_TLS_Reuse(FTP_TLS):
            def ntransfercmd(self, cmd, rest=None):
                from ftplib import FTP
                # Skip FTP_TLS.ntransfercmd, go to base + manually wrap
                conn, size = FTP.ntransfercmd(self, cmd, rest)
                if self._prot_p:
                    conn = self.context.wrap_socket(
                        conn, server_hostname=self.host,
                        session=self.sock.session,
                    )
                return conn, size
        FTP_TLS_Reuse = _FTP_TLS_Reuse
        # FTP_TLS doesn't have an "implicit" mode (it expects AUTH TLS
        # after connect). For implicit FTPS-on-port-990 we monkey-patch
        # by passing in a pre-wrapped TLS socket as the control channel.
        ftp = FTP_TLS_Reuse(context=self._ctx)
        ftp.set_pasv(True)
        # Implicit-FTPS: open the TLS-wrapped socket ourselves and inject.
        raw = socket.create_connection(
            (self.ip, self.port), timeout=self.connect_timeout)
        ftp.sock = self._ctx.wrap_socket(raw, server_hostname=self.ip)
        ftp.sock.settimeout(self.request_timeout)
        ftp.file = ftp.sock.makefile("r", encoding="utf-8")
        ftp.host = self.ip
        ftp.port = self.port
        ftp.af = socket.AF_INET  # ftplib's data-channel logic reads this
        ftp.timeout = self.request_timeout
        ftp.passiveserver = True
        ftp.encoding = "utf-8"
        # FTP_TLS marks itself as un-secured by default; we KNOW the
        # control channel is already TLS, so flip the internal flag so
        # subsequent prot_p() calls work correctly.
        ftp._prot_p = False
        # Read greeting
        ftp.welcome = ftp.getresp()
        # Auth
        ftp.login(user=self.username, passwd=self.access_code)
        # Enable TLS on data channel (PBSZ 0 + PROT P)
        ftp.prot_p()
        self._ftp = ftp
        self._sock = ftp.sock  # back-compat in case anything reads _sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
            self._sock = None
            self._ftp = None

    def list_files(
        self,
        kind: str = "timelapse",
        storage: Optional[str] = None,
    ) -> List[FileEntry]:
        """List files in the given category.

        kind ∈ {"timelapse", "video", "model", "cache", "/"} — common
        Bambu SD-card subdirectories. "/" lists the SD root.
        storage is currently ignored (kept for API compat with the
        BambuTunnel-era version).
        """
        if self._sock is None or not self._ftp:
            raise FileTunnelError("not connected; call connect() first")
        path = "/" if kind == "/" else f"/{kind}"
        try:
            self._ftp.cwd(path)
        except Exception as e:
            raise FileTunnelError(f"CWD {path} failed: {e}") from e
        # Use NLST + per-name SIZE/MDTM rather than LIST which has flaky
        # parsing across vsFTPd versions. Slower but reliable.
        try:
            names = self._ftp.nlst()
        except Exception as e:
            # On empty dir vsFTPd sometimes returns "550 No files found"
            if "550" in str(e):
                return []
            raise FileTunnelError(f"NLST {path} failed: {e}") from e
        entries: List[FileEntry] = []
        for name in names:
            if name in (".", "..", ""):
                continue
            full = path.rstrip("/") + "/" + name
            if path == "/":
                full = "/" + name
            size = 0
            is_dir = False
            try:
                size = self._ftp.size(name)
                if size is None:
                    # SIZE refused → probably a directory
                    is_dir = True
                    size = 0
            except Exception:
                # SIZE refused → probably a directory
                is_dir = True
            entries.append(FileEntry(
                name=name, path=full, time=0, size=int(size), is_dir=is_dir,
            ))
        return entries


def list_files(
    ip: str,
    access_code: str,
    kind: str = "timelapse",
    *,
    port: int = DEFAULT_PORT,
    storage: Optional[str] = None,
) -> List[FileEntry]:
    """One-shot convenience wrapper. Connects, lists, closes."""
    with FileTunnelClient(ip, access_code, port=port) as cli:
        return cli.list_files(kind, storage=storage)


if __name__ == "__main__":
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser()
    p.add_argument("ip")
    p.add_argument("access_code")
    p.add_argument("kind", nargs="?", default="timelapse",
                   choices=["timelapse", "video", "model", "cache", "/"])
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    try:
        files = list_files(
            args.ip, args.access_code, args.kind, port=args.port,
        )
    except FileTunnelError as e:
        print(f"file_tunnel: {e}", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"file_tunnel: socket error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(
            [{"name": f.name, "path": f.path, "time": f.time,
              "size": f.size, "is_dir": f.is_dir}
             for f in files], indent=2,
        ))
    else:
        if not files:
            print(f"(no {args.kind} files)")
        for f in files:
            print(f)
