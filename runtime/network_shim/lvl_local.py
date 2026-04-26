"""lvl_local — Bambu printer chamber-camera LVL_Local TCP/6000 client.

The printer exposes its chamber stream on TLS-wrapped TCP/6000. The
proprietary `libBambuSource.so` (x86_64-only, the part of the Bambu
plug-in that's NOT open) normally consumes
`bambu:///local/<ip>?port=6000&user=bblp&passwd=<access-code>` URLs
to play this stream inside BambuStudio.

This module re-implements the part of that protocol we've been able
to verify against a live X2D — TLS connect, send the 80-byte auth
blob, receive a 16-byte length-prefixed frame header. JPEG payloads
get blitted into a `latest_frame` slot the same way the existing
RTSPS proxy does.

Live findings on X2D (firmware 02.06.00.51) with chamber liveview
DISABLED on the touchscreen:
- TCP/6000 IS reachable
- TLSv1.3 handshake succeeds (cipher TLS_AES_256_GCM_SHA384)
- Server accepts the 80-byte auth blob without immediate close
- Then sends a 16+8 byte response: header `[size=8][status=0x0003013f]`
  + payload `[0xFFFFFFFF, 0]` — interpreted as "auth rejected /
  feature unavailable; close" — and closes the TCP connection
- The same printer's RTSPS port 322 is also closed in this state

So this module reaches the right wire format but the printer denies
the stream until the user flips LAN-mode liveview ON via the
touchscreen (Settings → Network → Liveview). With the toggle on,
either RTSPS:322 OR LVL_Local:6000 should serve frames; this module
covers the second case which the existing `camera --proto rtsp`
code already handles for the first.

Wire format (reverse-engineered from pybambu's ChamberImage class +
this session's TLS probes):

    Auth handshake (client → server, 80 bytes, little-endian):
        uint32  magic    = 0x00000040
        uint32  version  = 0x00003000
        uint32  reserved = 0
        uint32  reserved = 0
        char    username[32]   = "bblp\0..."
        char    access_code[32] = "<8-char code>\0..."

    Per-frame from server (little-endian):
        uint32  payload_size
        uint32  status / opcode
        uint64  timestamp_us  (likely; not confirmed)
        uint8[] payload  -- JPEG bytes ([FF D8 ... FF D9])

    Auth-rejected response from this printer state:
        uint32  payload_size = 8
        uint32  status       = 0x0003013f  (interpret: "feature unavailable")
        uint64  reserved     = 0
        uint8[8] payload     = FF FF FF FF 00 00 00 00
        (followed by TCP FIN)
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
import time

log = logging.getLogger(__name__)


HEADER_SIZE = 16
AUTH_BLOB_SIZE = 80
DEFAULT_PORT = 6000
USERNAME_FIELD_SIZE = 32
PASSWORD_FIELD_SIZE = 32

# These status words have been observed live from the X2D and labelled
# from contextual behaviour; treat as a non-exhaustive lookup table.
STATUS_HINTS: dict[int, str] = {
    0x0003013f: "auth rejected or stream unavailable — verify LAN-mode "
                "liveview is enabled on the printer touchscreen "
                "(Settings → Network → Liveview)",
}


class LVLLocalError(RuntimeError):
    """Raised on TLS / handshake / framing failures. .status carries
    the printer's status word when known so callers can branch on
    documented codes."""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


class FrameHeader:
    __slots__ = ("size", "status", "timestamp")

    def __init__(self, size: int, status: int, timestamp: int):
        self.size = size
        self.status = status
        self.timestamp = timestamp

    @classmethod
    def parse(cls, raw: bytes) -> "FrameHeader":
        if len(raw) != HEADER_SIZE:
            raise LVLLocalError(f"header must be {HEADER_SIZE} bytes, got {len(raw)}")
        size, status, ts = struct.unpack("<IIQ", raw)
        return cls(size, status, ts)


def _build_auth_blob(username: str, access_code: str) -> bytes:
    """Compose the 80-byte auth handshake. Both fields are zero-padded
    fixed-width buffers; access_code is the 8-char value the user sees
    on the printer screen under Settings → Network → Access Code."""
    if len(username.encode("ascii")) > USERNAME_FIELD_SIZE:
        raise LVLLocalError(f"username too long ({len(username)} > {USERNAME_FIELD_SIZE})")
    if len(access_code.encode("ascii")) > PASSWORD_FIELD_SIZE:
        raise LVLLocalError(f"access_code too long ({len(access_code)} > {PASSWORD_FIELD_SIZE})")
    blob = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    blob += username.encode("ascii").ljust(USERNAME_FIELD_SIZE, b"\x00")
    blob += access_code.encode("ascii").ljust(PASSWORD_FIELD_SIZE, b"\x00")
    assert len(blob) == AUTH_BLOB_SIZE, f"auth blob size mismatch: {len(blob)}"
    return blob


def _make_ctx() -> ssl.SSLContext:
    """The printer's cert is self-signed and uses the BBL device PKI we
    don't have a CA bundle for — disable verification (same posture as
    the existing FTPS+MQTT clients in x2d_bridge.py)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    """Read exactly n bytes or raise. TLSSocket may return short reads."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise LVLLocalError(f"connection closed at {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


class LVLLocalClient:
    """Single-connection LVL_Local consumer.

    Usage:
        with LVLLocalClient(ip, code) as cli:
            for jpeg, ts in cli.frames():
                latest = jpeg
    """

    def __init__(self,
                 ip: str,
                 access_code: str,
                 *,
                 port: int = DEFAULT_PORT,
                 username: str = "bblp",
                 connect_timeout: float = 5.0,
                 frame_timeout: float = 10.0):
        self.ip = ip
        self.access_code = access_code
        self.port = port
        self.username = username
        self.connect_timeout = connect_timeout
        self.frame_timeout = frame_timeout
        self._sock: ssl.SSLSocket | None = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self) -> None:
        raw = socket.create_connection((self.ip, self.port),
                                       timeout=self.connect_timeout)
        ctx = _make_ctx()
        self._sock = ctx.wrap_socket(raw, server_hostname=self.ip)
        self._sock.sendall(_build_auth_blob(self.username, self.access_code))
        # Don't read the first header here — let frames() handle it so the
        # caller can distinguish "auth rejected" from "no frames yet".
        log.info("LVL_Local connected to %s:%d (TLS %s)",
                 self.ip, self.port, self._sock.version())

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def frames(self):
        """Generator yielding (jpeg_bytes, timestamp_us) tuples until
        the printer closes the connection. Raises LVLLocalError if
        the printer rejects auth or sends a malformed frame."""
        if self._sock is None:
            raise LVLLocalError("not connected; call connect() first")
        self._sock.settimeout(self.frame_timeout)

        while True:
            try:
                hdr_raw = _recv_exact(self._sock, HEADER_SIZE)
            except LVLLocalError as e:
                # Clean EOF — caller can try reconnecting.
                log.info("LVL_Local stream ended: %s", e)
                return
            hdr = FrameHeader.parse(hdr_raw)
            payload = _recv_exact(self._sock, hdr.size) if hdr.size > 0 else b""
            # Auth-rejected response shape — surface a useful error.
            if hdr.size == 8 and payload == b"\xff\xff\xff\xff\x00\x00\x00\x00":
                hint = STATUS_HINTS.get(hdr.status,
                                        f"unknown status 0x{hdr.status:08x}")
                raise LVLLocalError(
                    f"printer rejected stream (status 0x{hdr.status:08x}): {hint}",
                    status=hdr.status,
                )
            if payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9"):
                yield payload, hdr.timestamp
            else:
                log.warning("LVL_Local non-JPEG payload (sz=%d, status=0x%08x): %s",
                            hdr.size, hdr.status, payload[:16].hex())


def stream_frames(ip: str,
                  access_code: str,
                  *,
                  port: int = DEFAULT_PORT,
                  on_frame=None,
                  reconnect: bool = True,
                  reconnect_backoff: float = 1.0):
    """Long-running consumer suitable for the camera proxy. Calls
    on_frame(jpeg, ts) for each frame; reconnects on transient errors
    until the caller raises out via on_frame or the process exits."""
    while True:
        try:
            with LVLLocalClient(ip, access_code, port=port) as cli:
                for jpeg, ts in cli.frames():
                    if on_frame is not None:
                        on_frame(jpeg, ts)
        except LVLLocalError as e:
            log.warning("LVL_Local error: %s", e)
            if not reconnect:
                raise
        except OSError as e:
            log.warning("LVL_Local socket error: %s", e)
            if not reconnect:
                raise
        time.sleep(reconnect_backoff)
        reconnect_backoff = min(reconnect_backoff * 2.0, 30.0)


if __name__ == "__main__":
    # Quick CLI: `python -m lvl_local <ip> <code>` to test reachability.
    import argparse
    import sys

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ip")
    p.add_argument("code", help="8-char printer access code")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--frames", type=int, default=3,
                   help="Stop after N frames (default 3)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    n = 0
    try:
        with LVLLocalClient(args.ip, args.code, port=args.port) as cli:
            for jpeg, ts in cli.frames():
                n += 1
                print(f"frame {n}: {len(jpeg)} bytes, ts={ts}")
                if n >= args.frames:
                    break
    except LVLLocalError as e:
        print(f"ERR (status=0x{e.status:08x}): {e}", file=sys.stderr)
        sys.exit(1)
