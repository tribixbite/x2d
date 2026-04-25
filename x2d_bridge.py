#!/data/data/com.termux/files/usr/bin/env python3.12
"""x2d_bridge — local LAN client / status daemon for Bambu Lab X2D, P2S,
and other Bambu printers that require RSA-SHA256 signed MQTT messages
(Jan-2025+ firmware).

Purpose
-------
The Bambu Network Plugin .so (which BambuStudio dlopens to talk to printers)
is x86_64 / arm64-mac only — there's no aarch64 Linux build, so on Termux
the GUI's "connect" / "AMS sync" / "print" actions don't work.

This script gives you a working LAN client without the plugin:

    x2d_bridge.py status                    # one-shot device state pull
    x2d_bridge.py upload  out.gcode.3mf     # FTPS:990 implicit-TLS push
    x2d_bridge.py print   out.gcode.3mf     # upload + start print w/ AMS slot
    x2d_bridge.py daemon                    # long-running monitor on stdout
    x2d_bridge.py daemon --http :8765       # status JSON at /state, etc.

Authentication
--------------
Three values are required. They are read from (in order):
  1. CLI flags                  --ip / --code / --serial
  2. `~/.x2d/credentials`       INI file with [printer] ip=… code=… serial=…
  3. environment variables      X2D_IP, X2D_CODE, X2D_SERIAL

The three values are: the printer's LAN IP, its 8-character access code
(visible on the printer screen under Settings → Network), and the printer's
serial number (printed on the device sticker / Settings → About).

The MQTT signing certificate is the publicly-leaked Bambu Connect cert
embedded in `BAMBU_CERT_PEM` below (cert_id GLOF3813734089-…). Without it,
recent firmware rejects every command with `err_code 84033543 "mqtt
message verify failed"`.

Side notes
----------
* No Bambu cloud calls. No telemetry. Talks only to the LAN IP you give.
* `bambulabs_api` is NOT a dependency — it ships unsigned MQTT and gets
  rejected on signed-only firmwares.
* `paho-mqtt` and `cryptography` are required (`pip install paho-mqtt
  cryptography`). On Termux: `pkg install python-cryptography &&
  pip install paho-mqtt`.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass, field
from ftplib import FTP_TLS
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


BAMBU_CERT_ID = "GLOF3813734089-524a37c80000c6a6a274a47b3281"
# Intentionally NOT inlined here — the signing private key is the publicly
# leaked Bambu Connect global cert. See `bambu_cert.py` for the verbatim
# blob. Keeping it in a sibling file makes it easier to swap for a different
# cert if Bambu ever rotates and the leaked one stops being accepted.
try:
    from bambu_cert import BAMBU_PRIVATE_KEY_PEM
except ModuleNotFoundError:
    BAMBU_PRIVATE_KEY_PEM = None


# ---------------------------------------------------------------------------
# Credentials resolution
# ---------------------------------------------------------------------------

@dataclass
class Creds:
    ip: str
    code: str
    serial: str

    @classmethod
    def resolve(cls, args: argparse.Namespace) -> "Creds":
        env_ip = os.environ.get("X2D_IP", "")
        env_code = os.environ.get("X2D_CODE", "")
        env_serial = os.environ.get("X2D_SERIAL", "")

        ini_ip = ini_code = ini_serial = ""
        ini_path = Path.home() / ".x2d" / "credentials"
        if ini_path.exists():
            cp = configparser.ConfigParser()
            cp.read(ini_path)
            if cp.has_section("printer"):
                ini_ip = cp.get("printer", "ip", fallback="")
                ini_code = cp.get("printer", "code", fallback="")
                ini_serial = cp.get("printer", "serial", fallback="")

        ip = args.ip or env_ip or ini_ip
        code = args.code or env_code or ini_code
        serial = args.serial or env_serial or ini_serial
        if not (ip and code and serial):
            sys.exit(
                "credentials missing — provide --ip --code --serial, or set\n"
                "  X2D_IP / X2D_CODE / X2D_SERIAL env vars, or write\n"
                "  ~/.x2d/credentials\n\n"
                "  [printer]\n  ip = 192.168.x.y\n  code = 12345678\n  serial = 03ABC..."
            )
        return cls(ip=ip, code=code, serial=serial)


# ---------------------------------------------------------------------------
# Message signing — RSA-SHA256 over compact-JSON of the un-headered payload.
# Signature lives in a top-level `header` object the firmware reads first.
# ---------------------------------------------------------------------------

def _signing_key():
    if BAMBU_PRIVATE_KEY_PEM is None:
        sys.exit(
            "Bambu signing cert missing. Place the PEM-encoded private key in\n"
            "  bambu_cert.py:BAMBU_PRIVATE_KEY_PEM\n"
            "next to this script. The cert is the publicly-leaked Bambu\n"
            "Connect global cert — search public references for "
            f"`{BAMBU_CERT_ID}` if you need a copy."
        )
    return serialization.load_pem_private_key(
        BAMBU_PRIVATE_KEY_PEM.encode(), password=None
    )


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a Bambu MQTT payload with the `header` block the X2D / H2D /
    refreshed P1+X1 firmware require. The signature is computed against the
    compact-JSON of the *un-headered* dict, then the header is added on top.
    """
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = _signing_key().sign(body, padding.PKCS1v15(), hashes.SHA256())
    payload = dict(payload)
    payload["header"] = {
        "sign_ver": "v1.0",
        "sign_alg": "RSA_SHA256",
        "sign_string": sig.hex(),
        "cert_id": BAMBU_CERT_ID,
        "payload_len": len(body),
    }
    return payload


# ---------------------------------------------------------------------------
# MQTT client — thin wrapper that handles TLS + auth + signing.
# ---------------------------------------------------------------------------

class X2DClient:
    PORT = 8883

    def __init__(self, creds: Creds, on_state: Callable[[dict], None] | None = None):
        self.creds = creds
        self.on_state = on_state
        self._connected = Event()
        self._got_state = Event()
        self._latest_state: dict | None = None

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"x2d-bridge-{os.getpid()}",
            protocol=mqtt.MQTTv311,
        )
        # No CA verification — Bambu uses a self-signed device cert
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        self.client.tls_set_context(ssl_ctx)
        self.client.username_pw_set("bblp", creds.code)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # --- callbacks --------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            client.subscribe(f"device/{self.creds.serial}/report")
            self._connected.set()
        else:
            print(f"[x2d-bridge] MQTT connect failed: rc={rc}", file=sys.stderr)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        self._latest_state = payload
        self._got_state.set()
        if self.on_state:
            try:
                self.on_state(payload)
            except Exception as e:  # don't kill the listener loop
                print(f"[x2d-bridge] on_state callback raised: {e}", file=sys.stderr)

    # --- public API -------------------------------------------------------

    def connect(self, timeout: float = 8.0) -> None:
        self.client.connect(self.creds.ip, self.PORT, keepalive=60)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            raise TimeoutError(f"MQTT did not connect to {self.creds.ip}:{self.PORT} within {timeout}s")

    def request_state(self, timeout: float = 8.0) -> dict:
        """Send signed pushall and return the next state report."""
        self._got_state.clear()
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
        if not self._got_state.wait(timeout):
            raise TimeoutError("no state report received from printer")
        return self._latest_state

    def publish(self, payload: dict, qos: int = 1) -> None:
        signed = sign_payload(payload)
        topic = f"device/{self.creds.serial}/request"
        info = self.client.publish(topic, json.dumps(signed, separators=(",", ":")), qos=qos)
        info.wait_for_publish(timeout=5)

    def disconnect(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


# ---------------------------------------------------------------------------
# FTPS upload (port 990 implicit TLS, anon-NULL cert acceptance)
# ---------------------------------------------------------------------------

class _ImplicitFTPTLS(FTP_TLS):
    """FTPS implicit TLS over port 990 — Bambu's protocol of choice."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value


def upload_file(creds: Creds, local_path: Path, remote_name: str | None = None) -> None:
    if not local_path.is_file():
        sys.exit(f"file not found: {local_path}")
    if remote_name is None:
        remote_name = local_path.name
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ftp = _ImplicitFTPTLS(context=ssl_ctx)
    ftp.connect(creds.ip, 990, timeout=15)
    ftp.login(user="bblp", passwd=creds.code)
    ftp.prot_p()  # TLS-encrypt the data channel as well
    with local_path.open("rb") as f:
        ftp.storbinary(f"STOR {remote_name}", f)
    ftp.quit()


# ---------------------------------------------------------------------------
# Print start
# ---------------------------------------------------------------------------

def start_print(client: X2DClient, gcode_filename: str, *,
                use_ams: bool = True, ams_slot: int = 0,
                bed_levelling: bool = True, flow_cali: bool = False,
                timelapse: bool = False, vibration_cali: bool = False) -> None:
    payload = {
        "print": {
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "subtask_name": gcode_filename,
            "url": f"file:///mnt/sdcard/{gcode_filename}",
            "md5": "",  # firmware re-derives if blank
            "timelapse": timelapse,
            "bed_leveling": bed_levelling,
            "flow_cali": flow_cali,
            "vibration_cali": vibration_cali,
            "layer_inspect": False,
            "use_ams": use_ams,
            "ams_mapping": [ams_slot] if use_ams else [],
            "profile_id": "0",
            "project_id": "0",
            "subtask_id": "0",
            "task_id": "0",
        }
    }
    client.publish(payload)


# ---------------------------------------------------------------------------
# Optional HTTP status endpoint (so other tools can poll a JSON URL)
# ---------------------------------------------------------------------------

def _serve_http(bind: str, get_state: Callable[[], dict | None]) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    host, _, port = bind.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # silence default access log
            return

        def do_GET(self):
            if self.path == "/state":
                state = get_state()
                body = json.dumps(state or {}, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[x2d-bridge] HTTP listening on http://{host}:{port}/state",
          file=sys.stderr)
    server.serve_forever()


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    state = cli.request_state(timeout=args.timeout)
    cli.disconnect()
    print(json.dumps(state, indent=2))
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    creds = Creds.resolve(args)
    upload_file(creds, Path(args.file), remote_name=args.remote)
    print(f"uploaded {args.file} -> {creds.ip}:/{args.remote or Path(args.file).name}")
    return 0


def cmd_print(args: argparse.Namespace) -> int:
    creds = Creds.resolve(args)
    if not args.no_upload:
        upload_file(creds, Path(args.file), remote_name=args.remote)
    cli = X2DClient(creds)
    cli.connect()
    name = args.remote or Path(args.file).name
    start_print(cli, name,
                use_ams=not args.no_ams, ams_slot=args.slot,
                bed_levelling=not args.no_bed_level,
                flow_cali=args.flow_cali,
                timelapse=args.timelapse,
                vibration_cali=args.vib_cali)
    print(f"start_print queued: {name} (slot={args.slot}, ams={not args.no_ams})")
    cli.disconnect()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    creds = Creds.resolve(args)
    latest_state: dict | None = None

    def on_state(state: dict) -> None:
        nonlocal latest_state
        latest_state = state
        if not args.quiet:
            print(json.dumps({"ts": time.time(), "state": state}), flush=True)

    cli = X2DClient(creds, on_state=on_state)
    cli.connect()
    cli.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    if args.http:
        Thread(target=_serve_http, args=(args.http, lambda: latest_state),
               daemon=True).start()

    period = max(1, int(args.interval))
    print(f"[x2d-bridge] daemon up; polling every {period}s. Ctrl-C to quit.",
          file=sys.stderr)
    try:
        while True:
            time.sleep(period)
            cli.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
    except KeyboardInterrupt:
        pass
    cli.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", help="Printer LAN IP (overrides env / file)")
    p.add_argument("--code", help="Printer 8-char access code (overrides env / file)")
    p.add_argument("--serial", help="Printer serial (overrides env / file)")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="One-shot signed pushall + dump state")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(fn=cmd_status)

    u = sub.add_parser("upload", help="FTPS-implicit-TLS upload .gcode.3mf")
    u.add_argument("file", help="Local file to upload")
    u.add_argument("--remote", help="Remote filename (default: basename(local))")
    u.set_defaults(fn=cmd_upload)

    pr = sub.add_parser("print", help="Upload + start_print")
    pr.add_argument("file")
    pr.add_argument("--remote")
    pr.add_argument("--slot", type=int, default=0,
                    help="AMS global slot (AMS_index*4 + tray_in_ams), 0..15")
    pr.add_argument("--no-upload", action="store_true",
                    help="Skip upload — file is already on the printer")
    pr.add_argument("--no-ams", action="store_true")
    pr.add_argument("--no-bed-level", action="store_true")
    pr.add_argument("--flow-cali", action="store_true")
    pr.add_argument("--timelapse", action="store_true")
    pr.add_argument("--vib-cali", action="store_true")
    pr.set_defaults(fn=cmd_print)

    d = sub.add_parser("daemon", help="Long-running monitor; emits state to stdout")
    d.add_argument("--interval", default=5,
                   help="Seconds between forced state polls (default 5)")
    d.add_argument("--http", default="",
                   help="Bind addr for status HTTP server, e.g. ':8765' or '127.0.0.1:8765'")
    d.add_argument("--quiet", action="store_true",
                   help="Only emit on the HTTP endpoint, not stdout")
    d.set_defaults(fn=cmd_daemon)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
