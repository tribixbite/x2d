#!/usr/bin/env python3
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
import base64
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
        # Base64 matches the canonical Bambu Connect plugin format. Hex
        # also works on current firmware but base64 is forward-safer.
        "sign_string": base64.b64encode(sig).decode("ascii"),
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

_SEQ_COUNTER = 0
def _next_seq() -> str:
    global _SEQ_COUNTER
    _SEQ_COUNTER += 1
    return str(_SEQ_COUNTER)


def start_print(client: X2DClient, gcode_filename: str, *,
                use_ams: bool = True, ams_slot: int = 0,
                bed_levelling: bool = True, flow_cali: bool = False,
                timelapse: bool = False, vibration_cali: bool = False,
                bed_type: str = "textured_plate") -> None:
    """Submit a project_file print command to the printer.

    `bed_type` is the build-plate identifier the printer expects when its
    own selector doesn't match the slice — newer firmware on H2D and some
    refreshed P1S have rejected payloads without this field. X2D appears to
    default fine, but we set it for forward-safety. Common values:
    `textured_plate`, `cool_plate`, `engineering_plate`, `high_temp_plate`.
    """
    payload = {
        "print": {
            "sequence_id": _next_seq(),
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "subtask_name": gcode_filename,
            "url": f"file:///mnt/sdcard/{gcode_filename}",
            "md5": "",  # firmware re-derives if blank
            "bed_type": bed_type,
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
                vibration_cali=args.vib_cali,
                bed_type=args.bed_type)
    print(f"start_print queued: {name} (slot={args.slot}, ams={not args.no_ams})")
    cli.disconnect()
    return 0


# ---------------------------------------------------------------------------
# `serve` mode — Unix-domain socket server that the libbambu_networking.so
# shim talks to. See runtime/network_shim/PROTOCOL.md for the wire format.
#
# One ServeServer process accepts many shim connections (one per
# bambu-studio instance). Each connection runs in its own reader thread.
# Printer-side MQTT clients are shared globally keyed by dev_id, so two
# shims pointing at the same printer don't double-subscribe.
# ---------------------------------------------------------------------------

ABI_VERSION = 1
SHIM_VERSION = "0.1.0"


class _OpError(Exception):
    """Op handler failure — surfaces as `{ok:false, error:{code, message}}`."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _PrinterSession:
    """One live MQTT connection to a single printer plus a fan-out of state
    pushes to every shim that asked for it. Reference-counted so the
    underlying X2DClient closes only when the last shim disconnects."""

    def __init__(self, dev_id: str, dev_ip: str, code: str):
        from threading import Lock as _Lock
        self.dev_id = dev_id
        self.dev_ip = dev_ip
        self.code = code
        self._refcount = 0
        self._lock = _Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._connect_listeners: list[Callable[[int, str, str], None]] = []
        self.client = X2DClient(
            Creds(ip=dev_ip, code=code, serial=dev_id),
            on_state=self._dispatch_state,
        )

    def _dispatch_state(self, payload: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(payload)
            except Exception as e:  # one bad subscriber shouldn't poison others
                print(f"[serve] state listener raised: {e}", file=sys.stderr)

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    def add_connect_listener(self, fn: Callable[[int, str, str], None]) -> None:
        with self._lock:
            self._connect_listeners.append(fn)

    def remove_connect_listener(self, fn: Callable[[int, str, str], None]) -> None:
        with self._lock:
            try:
                self._connect_listeners.remove(fn)
            except ValueError:
                pass

    def _emit_connect(self, status: int, msg: str = "") -> None:
        with self._lock:
            listeners = list(self._connect_listeners)
        for fn in listeners:
            try:
                fn(status, self.dev_id, msg)
            except Exception as e:
                print(f"[serve] connect listener raised: {e}", file=sys.stderr)

    def acquire(self) -> None:
        with self._lock:
            first = self._refcount == 0
            self._refcount += 1
        if first:
            try:
                self.client.connect(timeout=8.0)
                self._emit_connect(0, "connected")  # ConnectStatusOk
                self.client.publish(
                    {"pushing": {"sequence_id": _next_seq(), "command": "pushall"}}
                )
            except Exception as e:
                self._emit_connect(1, str(e))  # ConnectStatusFailed
                raise _OpError(-2, f"connect failed: {e}") from e

    def release(self) -> None:
        with self._lock:
            self._refcount -= 1
            now_zero = self._refcount <= 0
        if now_zero:
            try:
                self.client.disconnect()
            finally:
                self._emit_connect(2, "lost")  # ConnectStatusLost


class ServeServer:
    def __init__(self, sock_path: Path):
        self.sock_path = sock_path
        self._printers: dict[str, _PrinterSession] = {}
        self._printers_lock = __import__("threading").Lock()
        self._stop = Event()

    # --- printer registry ---------------------------------------------

    def get_or_open_printer(self, dev_id: str, dev_ip: str, code: str) -> _PrinterSession:
        with self._printers_lock:
            sess = self._printers.get(dev_id)
            if sess is None:
                sess = _PrinterSession(dev_id, dev_ip, code)
                self._printers[dev_id] = sess
            elif sess.dev_ip != dev_ip or sess.code != code:
                # IP/code changed under us — close old, open new.
                try:
                    sess.client.disconnect()
                except Exception:
                    pass
                sess = _PrinterSession(dev_id, dev_ip, code)
                self._printers[dev_id] = sess
        sess.acquire()
        return sess

    def release_printer(self, dev_id: str) -> None:
        with self._printers_lock:
            sess = self._printers.get(dev_id)
        if sess is not None:
            sess.release()

    # --- main loop ----------------------------------------------------

    def serve_forever(self) -> int:
        import socket
        from threading import Thread as _Thread

        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(str(self.sock_path))
        os.chmod(str(self.sock_path), 0o600)
        srv.listen(8)
        srv.settimeout(0.5)

        import signal as _signal
        def _stop_handler(signum, frame):  # noqa: ARG001
            self._stop.set()
        _signal.signal(_signal.SIGINT, _stop_handler)
        _signal.signal(_signal.SIGTERM, _stop_handler)

        print(f"[serve] listening on {self.sock_path}", file=sys.stderr)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            handler = _ConnHandler(self, conn)
            t = _Thread(target=handler.run, name=f"shim-{handler.id}", daemon=True)
            t.start()

        srv.close()
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        # Disconnect every active printer cleanly.
        with self._printers_lock:
            for sess in self._printers.values():
                try:
                    sess.client.disconnect()
                except Exception:
                    pass
        print("[serve] stopped cleanly", file=sys.stderr)
        return 0


_conn_id = 0


class _ConnHandler:
    """One shim connection. Owns its socket; spawns no extra threads."""

    def __init__(self, server: ServeServer, sock):
        global _conn_id
        _conn_id += 1
        self.id = _conn_id
        self.server = server
        self.sock = sock
        self._write_lock = __import__("threading").Lock()
        self._subscribed: set[str] = set()
        self._state_cb: Callable[[dict], None] | None = None
        self._connect_cb: Callable[[int, str, str], None] | None = None

    # --- I/O primitives ----------------------------------------------

    def _send(self, obj: dict) -> None:
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            try:
                self.sock.sendall(line)
            except (BrokenPipeError, OSError):
                pass

    def _read_lines(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except (ConnectionResetError, OSError):
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line

    # --- callbacks injected into _PrinterSession ---------------------

    def _emit_local_message(self, dev_id: str, payload: dict) -> None:
        self._send({
            "kind": "evt",
            "name": "local_message",
            "data": {
                "dev_id": dev_id,
                "msg": json.dumps(payload, separators=(",", ":")),
            },
        })

    def _emit_local_connect(self, status: int, dev_id: str, msg: str) -> None:
        self._send({
            "kind": "evt",
            "name": "local_connect",
            "data": {"status": status, "dev_id": dev_id, "msg": msg},
        })

    # --- main loop ----------------------------------------------------

    def run(self) -> None:
        try:
            for raw in self._read_lines():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"[serve] bad json from shim: {e}", file=sys.stderr)
                    continue
                if msg.get("kind") != "req":
                    continue
                self._handle_request(msg)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        # Drop our subscriptions and release each printer ref.
        for dev_id in list(self._subscribed):
            sess = self.server._printers.get(dev_id)
            if sess is not None:
                if self._state_cb:
                    sess.remove_listener(
                        lambda p, dev=dev_id: self._emit_local_message(dev, p)
                    )
                if self._connect_cb:
                    sess.remove_connect_listener(self._connect_cb)
                sess.release()
        try:
            self.sock.close()
        except OSError:
            pass

    def _handle_request(self, req: dict) -> None:
        op = req.get("op", "")
        args = req.get("args") or {}
        rid = req.get("id")
        handler = _OPS.get(op)
        if handler is None:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": -1, "message": f"unknown op: {op}"},
            })
            return
        try:
            result = handler(self, args)
            self._send({"kind": "rsp", "id": rid, "ok": True, "result": result})
        except _OpError as e:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": e.code, "message": str(e)},
            })
        except Exception as e:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": -128, "message": f"{type(e).__name__}: {e}"},
            })


# ---------------------------------------------------------------------------
# Op handlers — small, one per `op` in PROTOCOL.md
# ---------------------------------------------------------------------------

def _op_hello(h: _ConnHandler, args: dict) -> dict:
    abi = int(args.get("abi", 0))
    if abi != ABI_VERSION:
        raise _OpError(-100, f"abi mismatch: shim {abi}, bridge {ABI_VERSION}")
    return {"bridge_version": SHIM_VERSION, "abi": ABI_VERSION,
            "default_printer": None}


def _op_connect_printer(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", args.get("code", "")))
    if not (dev_id and dev_ip and code):
        raise _OpError(-1, "missing dev_id/dev_ip/password")
    sess = h.server.get_or_open_printer(dev_id, dev_ip, code)
    h._subscribed.add(dev_id)
    listener = (lambda p, dev=dev_id: h._emit_local_message(dev, p))
    sess.add_listener(listener)
    sess.add_connect_listener(h._emit_local_connect)
    h._state_cb = listener
    h._connect_cb = h._emit_local_connect
    return {}


def _op_disconnect_printer(h: _ConnHandler, args: dict) -> dict:
    for dev_id in list(h._subscribed):
        sess = h.server._printers.get(dev_id)
        if sess is not None:
            if h._state_cb:
                sess.remove_listener(h._state_cb)
            if h._connect_cb:
                sess.remove_connect_listener(h._connect_cb)
            sess.release()
        h._subscribed.discard(dev_id)
    h._state_cb = None
    h._connect_cb = None
    return {}


def _op_send_message_to_printer(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    payload_json = args.get("json", "")
    if not (dev_id and payload_json):
        raise _OpError(-1, "missing dev_id/json")
    sess = h.server._printers.get(dev_id)
    if sess is None:
        raise _OpError(-1, "printer not connected")
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except json.JSONDecodeError as e:
        raise _OpError(-19, f"invalid json payload: {e}") from e
    try:
        sess.client.publish(payload, qos=int(args.get("qos", 1)))
    except Exception as e:
        raise _OpError(-4, f"publish failed: {e}") from e
    return {}


def _op_start_local_print(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", ""))
    filename = str(args.get("filename", ""))
    if not (dev_id and dev_ip and code and filename):
        raise _OpError(-1, "missing dev_id/dev_ip/password/filename")
    creds = Creds(ip=dev_ip, code=code, serial=dev_id)
    local = Path(filename)
    if not local.is_file():
        raise _OpError(-14, f"file not found: {filename}")
    remote = local.name
    try:
        upload_file(creds, local, remote_name=remote)
    except Exception as e:
        raise _OpError(-20, f"FTPS upload failed: {e}") from e
    sess = h.server._printers.get(dev_id)
    if sess is None:
        # Auto-connect for fire-and-forget print flows.
        sess = h.server.get_or_open_printer(dev_id, dev_ip, code)
    ams_mapping_str = args.get("ams_mapping") or "[0]"
    try:
        ams_mapping = json.loads(ams_mapping_str) if isinstance(ams_mapping_str, str) else ams_mapping_str
    except json.JSONDecodeError:
        ams_mapping = [0]
    use_ams = bool(args.get("task_use_ams", True))
    try:
        start_print(
            sess.client, remote,
            use_ams=use_ams,
            ams_slot=(ams_mapping[0] if ams_mapping else 0),
            bed_levelling=bool(args.get("task_bed_leveling", True)),
            flow_cali=bool(args.get("task_flow_cali", False)),
            timelapse=bool(args.get("task_record_timelapse", False)),
            vibration_cali=bool(args.get("task_vibration_cali", False)),
            bed_type=str(args.get("task_bed_type", "textured_plate")),
        )
    except Exception as e:
        raise _OpError(-4030, f"start_print MQTT failed: {e}") from e
    return {}


def _op_start_send_gcode_to_sdcard(h: _ConnHandler, args: dict) -> dict:
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", ""))
    filename = str(args.get("filename", ""))
    if not (dev_ip and code and filename):
        raise _OpError(-1, "missing dev_ip/password/filename")
    creds = Creds(ip=dev_ip, code=code, serial=str(args.get("dev_id", "")))
    local = Path(filename)
    if not local.is_file():
        raise _OpError(-14, f"file not found: {filename}")
    try:
        upload_file(creds, local, remote_name=local.name)
    except Exception as e:
        raise _OpError(-5010, f"FTPS upload failed: {e}") from e
    return {}


def _op_subscribe_local(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    interval = int(args.get("interval_s", 5))
    enable = bool(args.get("enable", True))
    sess = h.server._printers.get(dev_id) if dev_id else None
    if sess is None:
        raise _OpError(-1, "printer not connected")
    if enable:
        # The X2DClient already listens for state pushes once subscribed
        # in connect; we just kick a fresh pushall here.
        try:
            sess.client.publish(
                {"pushing": {"sequence_id": _next_seq(),
                             "command": "pushall"}},
            )
        except Exception as e:
            raise _OpError(-4, f"pushall publish failed: {e}") from e
    return {"interval_s": interval, "enable": enable}


def _op_get_version(h: _ConnHandler, args: dict) -> dict:
    return {"version": "02.06.00.50"}  # matches BAMBU_NETWORK_AGENT_VERSION


def _op_noop_ok(h: _ConnHandler, args: dict) -> dict:
    """Cloud-only entry points return success-with-empty so the GUI's
    paint paths don't choke on missing data."""
    return {}


def _op_login_status(h: _ConnHandler, args: dict) -> dict:
    return {"logged_in": False}


def _op_user_id(h: _ConnHandler, args: dict) -> dict:
    return {"id": ""}


def _op_user_presets(h: _ConnHandler, args: dict) -> dict:
    return {"presets": {}}


def _op_user_tasks(h: _ConnHandler, args: dict) -> dict:
    return {"tasks": []}


_OPS: dict[str, Callable[[_ConnHandler, dict], dict]] = {
    "hello":                       _op_hello,
    "get_version":                 _op_get_version,
    "connect_printer":             _op_connect_printer,
    "disconnect_printer":          _op_disconnect_printer,
    "send_message_to_printer":     _op_send_message_to_printer,
    "start_local_print":           _op_start_local_print,
    "start_local_print_with_record": _op_start_local_print,
    "start_send_gcode_to_sdcard":  _op_start_send_gcode_to_sdcard,
    "subscribe_local":             _op_subscribe_local,
    # cloud / catalog stubs
    "connect_server":              _op_noop_ok,
    "is_user_login":               _op_login_status,
    "get_user_id":                 _op_user_id,
    "get_user_presets":            _op_user_presets,
    "get_user_tasks":              _op_user_tasks,
    "start_print":                 _op_start_local_print,  # cloud → LAN
}


# ---------------------------------------------------------------------------
# Print-control verbs — direct signed-MQTT publishes for the most common
# operator actions. Payload schemas reverse-engineered from
# bs-bionic/src/slic3r/GUI/DeviceManager.cpp::MachineObject::command_*
# (see comments next to each).
# ---------------------------------------------------------------------------

def _publish_one(args: argparse.Namespace, payload: dict) -> int:
    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    try:
        cli.publish(payload)
    finally:
        cli.disconnect()
    print(json.dumps(payload, indent=2))
    return 0


def _print_cmd(command: str, **extra) -> dict:
    """Build a `{"print": {"command":..., "sequence_id":..., **extra}}`."""
    body = {"command": command, "sequence_id": _next_seq(), **extra}
    return {"print": body}


def _system_cmd(command: str, **extra) -> dict:
    body = {"command": command, "sequence_id": _next_seq(), **extra}
    return {"system": body}


def cmd_pause(args: argparse.Namespace) -> int:
    # MachineObject::command_task_pause — DeviceManager.cpp:1337
    return _publish_one(args, _print_cmd("pause", param=""))


def cmd_resume(args: argparse.Namespace) -> int:
    # MachineObject::command_task_resume — DeviceManager.cpp:1347
    return _publish_one(args, _print_cmd("resume", param=""))


def cmd_stop(args: argparse.Namespace) -> int:
    # MachineObject::command_task_abort — DeviceManager.cpp:1316
    return _publish_one(args, _print_cmd("stop", param=""))


def cmd_gcode(args: argparse.Namespace) -> int:
    # MachineObject::publish_gcode — DeviceManager.cpp:3645
    gcode = args.gcode if args.gcode.endswith("\n") else args.gcode + "\n"
    return _publish_one(args, _print_cmd("gcode_line", param=gcode))


def cmd_home(args: argparse.Namespace) -> int:
    return _publish_one(args, _print_cmd("gcode_line", param="G28\n"))


def cmd_level(args: argparse.Namespace) -> int:
    # G29 = auto bed leveling on most G-code dialects; the X-series
    # firmwares accept it as the canonical "level the bed now" command.
    return _publish_one(args, _print_cmd("gcode_line", param="G29\n"))


def cmd_set_temp(args: argparse.Namespace) -> int:
    if args.target == "bed":
        # MachineObject::command_set_bed (mqtt path) — DeviceManager.cpp:1474
        return _publish_one(args, _print_cmd("set_bed_temp", temp=int(args.value)))
    elif args.target == "nozzle":
        # MachineObject::command_set_nozzle_new — DeviceManager.cpp:1509
        return _publish_one(args, _print_cmd(
            "set_nozzle_temp",
            extruder_index=int(args.idx),
            target_temp=int(args.value),
        ))
    elif args.target == "chamber":
        # No mqtt verb in the source — fall back to gcode M141.
        return _publish_one(args, _print_cmd(
            "gcode_line", param=f"M141 S{int(args.value)}\n"
        ))
    else:
        sys.exit(f"unknown set-temp target: {args.target}")


def cmd_chamber_light(args: argparse.Namespace) -> int:
    # DevLamp::command_set_chamber_light — DeviceCore/DevLampCtrl.cpp:36
    state = args.state.lower()
    if state not in ("on", "off", "flashing"):
        sys.exit(f"chamber-light state must be on/off/flashing, got: {state}")
    payload = _system_cmd(
        "ledctrl",
        led_node="chamber_light",
        led_mode=state,
        led_on_time=int(args.on_time),
        led_off_time=int(args.off_time),
        loop_times=int(args.loops),
        interval_time=int(args.interval),
    )
    return _publish_one(args, payload)


def cmd_ams_unload(args: argparse.Namespace) -> int:
    # MachineObject::command_ams_change_filament with !load — DeviceManager.cpp:1537
    payload = _print_cmd(
        "ams_change_filament",
        curr_temp=int(args.curr_temp),
        tar_temp=int(args.tar_temp),
        ams_id=int(args.ams),
        target=255,    # 255 == unload sentinel
        slot_id=255,
    )
    return _publish_one(args, payload)


def cmd_ams_load(args: argparse.Namespace) -> int:
    # MachineObject::command_ams_change_filament with load — DeviceManager.cpp:1537
    ams_id = int(args.ams)
    slot_id = int(args.slot)
    tray_id = ams_id * 4 + slot_id
    target = ams_id if tray_id == 0 else tray_id
    payload = _print_cmd(
        "ams_change_filament",
        curr_temp=int(args.curr_temp),
        tar_temp=int(args.tar_temp),
        ams_id=ams_id,
        target=target,
        slot_id=slot_id,
    )
    return _publish_one(args, payload)


def cmd_jog(args: argparse.Namespace) -> int:
    # Relative move via standard G91/G1/G90 sequence — works on every
    # firmware that accepts arbitrary gcode.
    axis = args.axis.upper()
    if axis not in ("X", "Y", "Z", "E"):
        sys.exit(f"jog axis must be one of X/Y/Z/E, got: {args.axis}")
    feed = int(args.feed)
    distance = float(args.distance)
    gcode = (
        "G91\n"
        f"G1 {axis}{distance:g} F{feed}\n"
        "G90\n"
    )
    return _publish_one(args, _print_cmd("gcode_line", param=gcode))


def cmd_serve(args: argparse.Namespace) -> int:
    sock_path = Path(args.sock).expanduser()
    server = ServeServer(sock_path)
    return server.serve_forever()


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

    def _safe_pushall() -> None:
        try:
            cli.publish({"pushing": {"sequence_id": _next_seq(), "command": "pushall"}})
        except Exception as e:  # network blip — log and keep the loop alive
            print(f"[x2d-bridge] pushall publish failed: {e}", file=sys.stderr)

    _safe_pushall()

    if args.http:
        Thread(target=_serve_http, args=(args.http, lambda: latest_state),
               daemon=True).start()

    period = max(1, int(args.interval))
    print(f"[x2d-bridge] daemon up; polling every {period}s. Ctrl-C / SIGTERM to quit.",
          file=sys.stderr)

    import signal as _signal
    stop = Event()

    def _handle_sig(signum, frame):  # noqa: ARG001
        stop.set()

    _signal.signal(_signal.SIGINT, _handle_sig)
    _signal.signal(_signal.SIGTERM, _handle_sig)

    while not stop.is_set():
        if stop.wait(period):
            break
        _safe_pushall()
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
    pr.add_argument("--bed-type", default="textured_plate",
                    help="Build plate id sent to firmware "
                         "(textured_plate / cool_plate / engineering_plate / high_temp_plate)")
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

    # ----- print-control verbs -----------------------------------------
    pa = sub.add_parser("pause", help="Signed MQTT publish: pause current print")
    pa.set_defaults(fn=cmd_pause)

    re_ = sub.add_parser("resume", help="Signed MQTT publish: resume current print")
    re_.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("stop", help="Signed MQTT publish: abort current print")
    sp.set_defaults(fn=cmd_stop)

    gc = sub.add_parser("gcode", help="Send a literal G-code line as a signed MQTT publish")
    gc.add_argument("gcode", help="The G-code line (a trailing newline is added if missing)")
    gc.set_defaults(fn=cmd_gcode)

    hm = sub.add_parser("home", help="Home all axes (G28)")
    hm.set_defaults(fn=cmd_home)

    lv = sub.add_parser("level", help="Auto-level the bed (G29)")
    lv.set_defaults(fn=cmd_level)

    st = sub.add_parser("set-temp", help="Set target temperature (bed/nozzle/chamber)")
    st.add_argument("target", choices=["bed", "nozzle", "chamber"])
    st.add_argument("value", type=int, help="Target temperature in °C")
    st.add_argument("--idx", type=int, default=0,
                    help="Nozzle index (0=left/main, 1=right) — only used for target=nozzle")
    st.set_defaults(fn=cmd_set_temp)

    cl = sub.add_parser("chamber-light", help="Set chamber LED state")
    cl.add_argument("state", choices=["on", "off", "flashing"])
    cl.add_argument("--on-time",   type=int, default=500)
    cl.add_argument("--off-time",  type=int, default=500)
    cl.add_argument("--loops",     type=int, default=0)
    cl.add_argument("--interval",  type=int, default=0)
    cl.set_defaults(fn=cmd_chamber_light)

    au = sub.add_parser("ams-unload", help="Unload filament from an AMS bay")
    au.add_argument("ams", type=int, help="AMS index (0..N)")
    au.add_argument("--curr-temp", type=int, default=215,
                    help="Current nozzle temperature for the unload heat soak")
    au.add_argument("--tar-temp",  type=int, default=215,
                    help="Target temperature to hit before retract")
    au.set_defaults(fn=cmd_ams_unload)

    al = sub.add_parser("ams-load", help="Load filament from an AMS slot")
    al.add_argument("ams", type=int, help="AMS index (0..N)")
    al.add_argument("slot", type=int, help="Slot within the AMS (0..3)")
    al.add_argument("--curr-temp", type=int, default=215)
    al.add_argument("--tar-temp",  type=int, default=215)
    al.set_defaults(fn=cmd_ams_load)

    jg = sub.add_parser("jog", help="Relative axis jog via G91/G1/G90")
    jg.add_argument("axis", help="X / Y / Z / E")
    jg.add_argument("distance", type=float, help="mm to move (negative for reverse)")
    jg.add_argument("--feed", type=int, default=1500, help="Feedrate in mm/min")
    jg.set_defaults(fn=cmd_jog)

    sv = sub.add_parser(
        "serve",
        help="Run a Unix-socket RPC server for libbambu_networking.so "
             "(see runtime/network_shim/PROTOCOL.md)",
    )
    sv.add_argument(
        "--sock",
        default=os.environ.get("X2D_BRIDGE_SOCK",
                               str(Path.home() / ".x2d" / "bridge.sock")),
        help="Unix socket path (default $X2D_BRIDGE_SOCK or "
             "~/.x2d/bridge.sock)",
    )
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
