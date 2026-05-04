"""file_tunnel — Bambu printer file-list browser over TCP/TLS:6000.

Parallel to lvl_local.py (which streams the chamber camera). Uses the
same TLS+access-code auth handshake but sends a LIST_INFO request after
auth instead of waiting for a JPEG stream. Returns the SD card's
timelapse / video / 3MF file lists as a Python list.

Reverse-engineered from BambuStudio bs-bionic/src/slic3r/GUI/Printer/
PrinterFileSystem.cpp:1434-1461 (SendRequest framing) and lines
162-234 (ListAllFiles flow).

Wire format after auth:
    Request:
        [u32 cmdtype = 0x3001]              # CTRL_TYPE prefix
        [u32 op = 0x0001]                   # LIST_INFO
        [u32 seq]                           # sequence ID
        [u32 body_len]                      # length of JSON body
        body                                # UTF-8 JSON
    Response:
        [u32 cmdtype]                       # echo of CTRL_TYPE
        [u32 op]                            # echo of opcode
        [u32 seq]                           # echo of sequence
        [u32 body_len]
        body                                # UTF-8 JSON with `file_lists`

JSON request body (per BambuStudio source):
    {"req": {"type": "timelapse"|"video"|"model",
             "storage": "<optional>",
             "api_version": 2,
             "notify": "DETAIL"}}

JSON response body:
    {"file_lists": [
        {"name": "...",
         "path": "...",
         "time": 1700000000,    # mtime in seconds since epoch
         "size": 12345},
        ...
    ]}
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

from .lvl_local import (
    AUTH_BLOB_SIZE,
    DEFAULT_PORT,
    LVLLocalError,
    _build_auth_blob,
    _make_ctx,
    _recv_exact,
)

# BambuStudio PrinterFileSystem.h:34-45 opcodes
CTRL_TYPE = 0x3001
OP_LIST_INFO = 0x0001
OP_SUB_FILE = 0x0002
OP_FILE_DEL = 0x0003
OP_FILE_DOWNLOAD = 0x0004
OP_FILE_UPLOAD = 0x0005
OP_REQUEST_MEDIA_ABILITY = 0x0007

REQUEST_HEADER_SIZE = 16  # 4×u32: cmdtype + op + seq + body_len


class FileTunnelError(LVLLocalError):
    """Distinct exception subclass so callers can tell file-tunnel errors
    apart from camera-stream errors."""
    pass


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    time: int   # seconds since epoch
    size: int   # bytes

    @classmethod
    def from_dict(cls, d: dict) -> "FileEntry":
        return cls(
            name=str(d.get("name", "")),
            path=str(d.get("path", "")),
            time=int(d.get("time", 0)),
            size=int(d.get("size", 0)),
        )

    def __str__(self) -> str:
        # Roughly `ls -l`-shaped — readable + parseable.
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.time))
        kib = self.size / 1024.0
        return f"{ts}  {kib:>9.1f} KiB  {self.path}"


def _frame_request(op: int, seq: int, body: dict) -> bytes:
    """Build the 16-byte header + UTF-8 JSON body."""
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    header = struct.pack("<IIII", CTRL_TYPE, op, seq, len(body_bytes))
    return header + body_bytes


def _read_response(sock: ssl.SSLSocket, expected_seq: int) -> dict:
    """Read one response frame and parse the JSON body.

    Caller is responsible for managing socket-level timeouts. Will
    raise FileTunnelError on framing mismatch or JSON parse failure.
    """
    hdr = _recv_exact(sock, REQUEST_HEADER_SIZE)
    cmdtype, op, seq, body_len = struct.unpack("<IIII", hdr)
    if cmdtype != CTRL_TYPE:
        raise FileTunnelError(
            f"unexpected cmdtype 0x{cmdtype:08x} (expected 0x{CTRL_TYPE:08x})")
    if seq != expected_seq:
        raise FileTunnelError(
            f"sequence mismatch (sent {expected_seq}, got {seq})")
    if body_len > 16 * 1024 * 1024:  # 16 MB cap
        raise FileTunnelError(f"body_len too large: {body_len}")
    body_raw = _recv_exact(sock, body_len) if body_len > 0 else b""
    try:
        return json.loads(body_raw.decode("utf-8")) if body_raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FileTunnelError(f"failed to parse response body: {e}") from e


class FileTunnelClient:
    """Single-shot file-tunnel client. Connect → request → close.

    Per-request reconnect because the printer's tunnel is quirky
    about idle connections — keeping it open between requests
    sometimes triggers 0x0003013f auth-rejected on the second op.

    Usage:
        with FileTunnelClient("192.168.0.190", "12345678") as cli:
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
        self._seq = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self) -> None:
        raw = socket.create_connection(
            (self.ip, self.port), timeout=self.connect_timeout)
        ctx = _make_ctx()
        self._sock = ctx.wrap_socket(raw, server_hostname=self.ip)
        self._sock.sendall(_build_auth_blob(self.username, self.access_code))
        self._sock.settimeout(self.request_timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    def list_files(
        self,
        kind: str = "timelapse",
        storage: Optional[str] = None,
    ) -> List[FileEntry]:
        """Send LIST_INFO and return the parsed file_lists.

        kind ∈ {"timelapse", "video", "model"}. storage is the SD-card
        partition name; default (None) lets the printer pick the
        current default storage.
        """
        if kind not in ("timelapse", "video", "model"):
            raise FileTunnelError(
                f"kind must be timelapse/video/model, got: {kind}")
        if self._sock is None:
            raise FileTunnelError("not connected; call connect() first")

        body: dict = {
            "req": {
                "type": kind,
                "api_version": 2,
                "notify": "DETAIL",
            }
        }
        if storage is not None:
            body["req"]["storage"] = storage

        seq = self._next_seq()
        self._sock.sendall(_frame_request(OP_LIST_INFO, seq, body))
        response = _read_response(self._sock, seq)
        files = response.get("file_lists", [])
        return [FileEntry.from_dict(f) for f in files]


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
    # CLI: python -m runtime.network_shim.file_tunnel <ip> <code> [kind]
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("ip")
    p.add_argument("access_code")
    p.add_argument("kind", nargs="?", default="timelapse",
                   choices=["timelapse", "video", "model"])
    p.add_argument("--storage", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--json", action="store_true",
                   help="emit JSON to stdout instead of human-readable")
    args = p.parse_args()

    try:
        files = list_files(
            args.ip, args.access_code, args.kind,
            port=args.port, storage=args.storage,
        )
    except FileTunnelError as e:
        print(f"file_tunnel: {e}", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"file_tunnel: socket error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(
            [{"name": f.name, "path": f.path, "time": f.time, "size": f.size}
             for f in files], indent=2,
        ))
    else:
        if not files:
            print(f"(no {args.kind} files)")
        for f in files:
            print(f)
