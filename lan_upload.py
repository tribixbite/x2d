#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lan_upload.py — FTPS-only upload of a .gcode.3mf to a Bambu Lab printer
on the local network. Sends ZERO MQTT messages, so the printer will NOT
auto-start the print. The file becomes visible in the printer's
on-device Files browser; the user starts the print manually from the
touchscreen.

Tested target: Bambu Lab X2D in LAN-only / dev mode. Should also work
unchanged for X1, P1, A1 series since they all share the bblp@:990
implicit-FTPS contract.

Protocol notes:
- Implicit FTPS on TCP/990 (NOT explicit AUTH-TLS). Python's stdlib
  ftplib.FTP_TLS speaks explicit-TLS only, so we subclass it and SSL-wrap
  the control socket immediately on connect (same trick used by
  bambulabs_api.PrinterFTPClient).
- Username is the literal string "bblp"; password is the 8-digit Access
  Code shown on the printer's screen (Settings → WLAN → Access Code, or
  in the printer's LAN-mode info panel).
- After login the working directory is the printer's storage root.
  Bambu Studio uploads with a bare filename (STOR <filename>) and the
  printer happily lists it on the Files screen; that's what we do here.
  Some community scripts upload to /cache/<file> instead — both work,
  but bare-filename matches what BambuStudio itself does and what
  bambulabs_api.Printer.upload_file does, and the start_print MQTT call
  references "ftp:///<filename>" (no cache prefix), so for symmetry we
  stick with bare filename.
- The TLS handshake on the data channel reuses the control session, and
  some firmware versions hang on the post-STOR close handshake unless
  we shut the SSL layer down ourselves; we honour that with the same
  unwrap-on-close pattern bambulabs_api uses.

Usage:
    python3 lan_upload.py \\
        --ip 192.168.1.42 \\
        --access-code 12345678 \\
        --serial 0123456789ABCDEF \\
        --file /path/to/huntrx_frame.gcode.3mf

The --serial flag is accepted but unused for upload-only; it is captured
so the same CLI signature works with a future start-print mode.
"""

from __future__ import annotations

import argparse
import ftplib
import logging
import socket
import ssl
import sys
from pathlib import Path
from typing import BinaryIO, Optional


log = logging.getLogger("lan_upload")


class ImplicitFTPTLS(ftplib.FTP_TLS):
    """
    FTP_TLS variant that performs implicit TLS (TLS handshake before
    any FTP commands), which is what Bambu Lab firmware expects on
    port 990.

    Stdlib ftplib.FTP_TLS only supports explicit AUTH TLS, where the
    socket starts plaintext and is upgraded by sending "AUTH TLS". The
    Bambu firmware never replies to that — we have to be wrapped in TLS
    from the very first byte. We achieve that by overriding the `sock`
    setter so any socket assigned to us is immediately ssl-wrapped, and
    by overriding storbinary to also wrap (and shut down) the data
    connection cleanly.
    """

    def __init__(self, *args, unwrap_on_close: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Defer socket creation; FTP.connect() will assign self.sock and
        # the setter below will TLS-wrap it.
        self._sock: Optional[socket.socket] = None
        self._unwrap_on_close = unwrap_on_close

    @property  # type: ignore[override]
    def sock(self) -> Optional[socket.socket]:
        return self._sock

    @sock.setter
    def sock(self, value: Optional[socket.socket]) -> None:
        # Wrap any plaintext socket assigned to us. Already-SSL sockets
        # (e.g. data-connection sockets we've wrapped ourselves) are
        # passed through untouched.
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    # PASV data connections need TLS too; reuse the control session for
    # session resumption (some Bambu firmwares require it).
    def ntransfercmd(self, cmd: str, rest=None):  # type: ignore[override]
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,  # type: ignore[union-attr]
            )
        return conn, size

    def storbinary(  # type: ignore[override]
        self,
        cmd: str,
        fp: BinaryIO,
        blocksize: int = 32768,
        callback=None,
        rest=None,
    ) -> str:
        # Mirror stdlib but unwrap the SSL layer before close to avoid
        # the documented "hangs forever after STOR" bug on Bambu boxes.
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            if (
                self._unwrap_on_close
                and isinstance(conn, ssl.SSLSocket)
            ):
                try:
                    conn.unwrap()
                except (OSError, ssl.SSLError):
                    # Some firmwares just slam the connection shut; not
                    # fatal, the upload itself has already succeeded.
                    pass
        finally:
            conn.close()
        return self.voidresp()


def upload_file_ftps(
    ip: str,
    access_code: str,
    file_path: Path,
    remote_name: Optional[str] = None,
    user: str = "bblp",
    port: int = 990,
    timeout: float = 30.0,
) -> str:
    """
    Upload `file_path` to a Bambu printer over implicit FTPS. Returns
    the server's response line (typically '226 Transfer complete.').
    No MQTT message is sent, so the printer will NOT auto-start a print.

    Args:
        ip:           printer's LAN IP address
        access_code:  the 8-digit code printed on the printer's screen
        file_path:    local path to the .gcode.3mf to upload
        remote_name:  optional override for the on-printer filename
                      (defaults to file_path.name)
        user:         FTP username; always "bblp" on Bambu firmware
        port:         FTPS port; always 990 on Bambu firmware
        timeout:      socket timeout in seconds for the control channel
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"upload source does not exist: {file_path}")

    name = remote_name or file_path.name
    size = file_path.stat().st_size
    log.info("Uploading %s (%d bytes) to ftps://%s:%d/%s",
             file_path, size, ip, port, name)

    # Bambu's self-signed cert is per-printer and not in any trust store,
    # so we have to disable certificate verification. The TLS layer is
    # still doing real encryption — we just can't authenticate the peer
    # without out-of-band cert pinning, which the printer doesn't expose.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftps = ImplicitFTPTLS(context=ctx, timeout=timeout)
    try:
        ftps.connect(host=ip, port=port)
        ftps.login(user=user, passwd=access_code)
        # Switch the data channel to encrypted as well; without this the
        # firmware refuses to open a PASV port.
        ftps.prot_p()

        with file_path.open("rb") as fp:
            response = ftps.storbinary(
                f"STOR {name}",
                fp,
                blocksize=32768,
                callback=lambda chunk: log.debug("uploaded %d bytes", len(chunk)),
            )
        log.info("Server response: %s", response.strip())
        return response
    finally:
        # quit() sends QUIT and closes both ends cleanly; close() is the
        # hard fallback if QUIT fails (some firmwares time out on it).
        try:
            ftps.quit()
        except Exception:  # noqa: BLE001 — best-effort teardown
            try:
                ftps.close()
            except Exception:  # noqa: BLE001
                pass


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lan_upload",
        description=(
            "Upload a sliced .gcode.3mf to a Bambu Lab printer over "
            "FTPS. Does NOT send any MQTT message, so the print will "
            "NOT auto-start — the file just appears on the printer's "
            "Files screen, ready to be launched manually."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ip", required=True,
                        help="Printer LAN IP address, e.g. 192.168.1.42")
    parser.add_argument("--access-code", required=True,
                        help="8-digit Access Code shown on printer screen")
    parser.add_argument("--serial", required=True,
                        help=("Printer serial (e.g. 0123456789ABCDEF). "
                              "Not used for upload-only mode but kept "
                              "in the CLI for symmetry with future "
                              "start-print modes."))
    parser.add_argument("--file", required=True, type=Path,
                        help="Path to the .gcode.3mf file to upload")
    parser.add_argument("--remote-name", default=None,
                        help=("Filename to use on the printer. Defaults "
                              "to the local basename."))
    parser.add_argument("--port", type=int, default=990,
                        help="FTPS port (Bambu firmware uses 990)")
    parser.add_argument("--user", default="bblp",
                        help="FTP username (Bambu firmware uses 'bblp')")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Socket timeout in seconds")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG logging")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # TODO: optionally verify file_path looks like a real Bambu 3MF
    # (zip with Metadata/plate_1.gcode + plate_1.gcode.md5) before
    # uploading, to fail fast on obviously-bad inputs.
    _ = args.serial  # explicitly unused; see argparse help text

    try:
        upload_file_ftps(
            ip=args.ip,
            access_code=args.access_code,
            file_path=args.file,
            remote_name=args.remote_name,
            user=args.user,
            port=args.port,
            timeout=args.timeout,
        )
    except Exception as e:  # noqa: BLE001 — top-level CLI handler
        log.error("Upload failed: %s", e)
        return 1
    log.info("Upload complete. The printer will NOT auto-start; launch "
             "manually from the Files screen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
