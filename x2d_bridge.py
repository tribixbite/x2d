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
from dataclasses import dataclass
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
    name: str = ""   # which [printer:NAME] section we came from (if any)

    @staticmethod
    def list_names(ini_path: Path | None = None) -> list[str]:
        """Return all `[printer:NAME]` section names in the creds file,
        in declaration order. The plain `[printer]` is reported as ''."""
        if ini_path is None:
            ini_path = Path.home() / ".x2d" / "credentials"
        if not ini_path.exists():
            return []
        cp = configparser.ConfigParser()
        cp.read(ini_path)
        names: list[str] = []
        for sec in cp.sections():
            if sec == "printer":
                names.append("")
            elif sec.startswith("printer:"):
                names.append(sec.split(":", 1)[1])
        return names

    @classmethod
    def resolve(cls, args: argparse.Namespace) -> "Creds":
        env_ip = os.environ.get("X2D_IP", "")
        env_code = os.environ.get("X2D_CODE", "")
        env_serial = os.environ.get("X2D_SERIAL", "")

        ini_ip = ini_code = ini_serial = ""
        chosen_name = ""
        ini_path = Path.home() / ".x2d" / "credentials"
        if ini_path.exists():
            cp = configparser.ConfigParser()
            cp.read(ini_path)
            requested = getattr(args, "printer", None) or os.environ.get("X2D_PRINTER", "")
            named_sections = [s for s in cp.sections() if s.startswith("printer:")]
            if requested:
                target = f"printer:{requested}"
                if not cp.has_section(target):
                    sys.exit(
                        f"no [{target}] section in {ini_path}.\n"
                        f"available: {', '.join(named_sections) or '(none)'}"
                    )
                section = target
                chosen_name = requested
            elif cp.has_section("printer"):
                section = "printer"
            elif len(named_sections) == 1:
                section = named_sections[0]
                chosen_name = section.split(":", 1)[1]
            elif len(named_sections) > 1:
                sys.exit(
                    "multiple [printer:NAME] sections found and no --printer/X2D_PRINTER set; "
                    f"choose one of: {', '.join(s.split(':',1)[1] for s in named_sections)}"
                )
            else:
                section = "printer"  # will fall through to "missing" below
            if cp.has_section(section):
                ini_ip = cp.get(section, "ip", fallback="")
                ini_code = cp.get(section, "code", fallback="")
                ini_serial = cp.get(section, "serial", fallback="")

        ip = args.ip or env_ip or ini_ip
        code = args.code or env_code or ini_code
        serial = args.serial or env_serial or ini_serial
        if not (ip and code and serial):
            sys.exit(
                "credentials missing — provide --ip --code --serial, or set\n"
                "  X2D_IP / X2D_CODE / X2D_SERIAL env vars, or write\n"
                "  ~/.x2d/credentials\n\n"
                "  # default printer\n"
                "  [printer]\n  ip = 192.168.x.y\n  code = 12345678\n  serial = 03ABC...\n"
                "\n"
                "  # OR multiple printers, selected via --printer NAME or X2D_PRINTER\n"
                "  [printer:studio]\n  ip = …\n  code = …\n  serial = …\n"
            )
        return cls(ip=ip, code=code, serial=serial, name=chosen_name)


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

# ---------------------------------------------------------------------------
# Per-printer metrics counters (item #38). Module-level so multiple
# X2DClients in the same process share state. Key is the printer serial.
# Reset to 0 on process start; not persisted across restarts.
# ---------------------------------------------------------------------------
import collections as _collections
import threading as _threading

_metrics_counters: dict[str, dict[str, int]] = _collections.defaultdict(
    lambda: {"messages_total": 0, "mqtt_disconnects_total": 0,
             "mqtt_connects_total": 0})
_metrics_global: dict[str, int] = {"ssdp_notifies_total": 0}
_metrics_lock = _threading.Lock()


def _metric_inc(serial: str, name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics_counters[serial][name] = (
            _metrics_counters[serial].get(name, 0) + delta)


def _metric_global_inc(name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics_global[name] = _metrics_global.get(name, 0) + delta


def _metrics_snapshot() -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    with _metrics_lock:
        return ({k: dict(v) for k, v in _metrics_counters.items()},
                dict(_metrics_global))


class X2DClient:
    PORT = 8883
    # Persistent last-message timestamp (items #19, #37). Lets /healthz
    # report a meaningful age immediately after a daemon restart instead
    # of always-stale until the first new push arrives. Per-printer file
    # so multi-printer setups don't smash each other's timestamps.
    _TS_DIR = Path.home() / ".x2d"

    @classmethod
    def _ts_path_for(cls, serial: str) -> Path:
        # Persist under ~/.x2d/last_message_ts_<serial>. Hash-fall-back
        # if serial contains chars unsafe for a filename. Empty serial
        # (shouldn't happen for a real X2DClient but guard anyway) falls
        # back to the legacy single-file name.
        if not serial:
            return cls._TS_DIR / "last_message_ts"
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in serial)
        return cls._TS_DIR / f"last_message_ts_{safe}"

    def __init__(self, creds: Creds, on_state: Callable[[dict], None] | None = None):
        self.creds = creds
        self.on_state = on_state
        self._connected = Event()
        self._got_state = Event()
        self._latest_state: dict | None = None
        # Per-printer persist file path keyed by serial.
        self._ts_path = self._ts_path_for(creds.serial)
        # Restore last-known ts from disk so /healthz works on first
        # request post-restart. If the file is missing, malformed, or
        # claims a future time, fall through to "0" (treated as
        # "no message ever received").
        self._last_message_ts = 0.0
        try:
            ts = float(self._ts_path.read_text().strip())
            if 0 < ts <= time.time():
                self._last_message_ts = ts
        except (FileNotFoundError, ValueError, OSError):
            pass
        # One-time migration: if the legacy single-file path exists and
        # this serial has no per-printer file yet, inherit its timestamp.
        if self._last_message_ts == 0.0 and creds.serial:
            legacy = self._TS_DIR / "last_message_ts"
            if legacy.exists():
                try:
                    ts = float(legacy.read_text().strip())
                    if 0 < ts <= time.time():
                        self._last_message_ts = ts
                except (ValueError, OSError):
                    pass

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
            _metric_inc(self.creds.serial, "mqtt_connects_total")
        else:
            print(f"[x2d-bridge] MQTT connect failed: rc={rc}", file=sys.stderr)
            _metric_inc(self.creds.serial, "mqtt_disconnects_total")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        self._latest_state = payload
        now = time.time()
        self._last_message_ts = now
        _metric_inc(self.creds.serial, "messages_total")
        # Persist atomically (items #19 + #37 per-printer). Cheap — even
        # at 10 Hz this is a few hundred bytes/s of writeback. Keep the
        # parent dir mode exclusive (creds live there too).
        try:
            self._ts_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._ts_path.with_suffix(self._ts_path.suffix + ".tmp")
            tmp.write_text(f"{now}\n")
            os.replace(tmp, self._ts_path)
        except OSError as e:
            # Don't let a transient FS error kill the listener — log once.
            if not getattr(self, "_ts_persist_warned", False):
                print(f"[x2d-bridge] last_msg_ts persist failed: {e}", file=sys.stderr)
                self._ts_persist_warned = True
        self._got_state.set()
        if self.on_state:
            try:
                self.on_state(payload)
            except Exception as e:  # don't kill the listener loop
                print(f"[x2d-bridge] on_state callback raised: {e}", file=sys.stderr)

    @property
    def last_message_ts(self) -> float:
        """Wall-clock time of the most recent state push from the
        printer, or 0.0 if none received yet. Used by the daemon's
        /healthz endpoint to flag a silent disconnect."""
        return getattr(self, "_last_message_ts", 0.0)

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

def _is_loopback(host: str) -> bool:
    """True if the host is a loopback address (auth not required).
    Anything else (LAN IP, 0.0.0.0) is treated as exposed and gates
    on bearer-token auth when one is configured."""
    return host in {"127.0.0.1", "::1", "localhost", ""}


def _format_prometheus_metrics(states: dict[str, dict | None],
                               last_ts_by_name: dict[str, float]) -> bytes:
    """Render counters + per-printer gauges in Prometheus text exposition
    format (item #38). Stateless render — pulls counters from
    _metrics_snapshot and gauges from the live state cache."""
    counters, glob = _metrics_snapshot()
    lines: list[str] = []

    # Global counters (no printer label)
    lines.append("# HELP x2d_ssdp_notifies_total Total SSDP NOTIFY broadcasts received")
    lines.append("# TYPE x2d_ssdp_notifies_total counter")
    lines.append(f"x2d_ssdp_notifies_total {glob.get('ssdp_notifies_total', 0)}")

    # Per-printer counters
    counter_help = {
        "messages_total":         ("counter", "MQTT state push messages received"),
        "mqtt_connects_total":    ("counter", "MQTT connect successes"),
        "mqtt_disconnects_total": ("counter", "MQTT connect failures (rc!=0)"),
    }
    for cname, (ctype, chelp) in counter_help.items():
        lines.append(f"# HELP x2d_{cname} {chelp}")
        lines.append(f"# TYPE x2d_{cname} {ctype}")
        for serial, kvs in counters.items():
            v = kvs.get(cname, 0)
            lines.append(f'x2d_{cname}{{serial="{serial}"}} {v}')

    # Per-printer last_message_ts as a gauge
    lines.append("# HELP x2d_last_message_ts Unix-epoch seconds of last printer push")
    lines.append("# TYPE x2d_last_message_ts gauge")
    for name, ts in last_ts_by_name.items():
        lines.append(f'x2d_last_message_ts{{printer="{name}"}} {ts}')

    # Per-printer gauges from latest state
    gauge_paths = [
        ("bed_temp",          ("print", "bed_temper")),
        ("bed_temp_target",   ("print", "bed_target_temper")),
        ("nozzle_temp",       ("print", "nozzle_temper")),
        ("nozzle_temp_target",("print", "nozzle_target_temper")),
        ("mc_percent",        ("print", "mc_percent")),
        ("mc_remaining_min",  ("print", "mc_remaining_time")),
        ("layer_num",         ("print", "layer_num")),
        ("total_layer_num",   ("print", "total_layer_num")),
    ]
    for gname, path in gauge_paths:
        lines.append(f"# HELP x2d_{gname} Printer state field")
        lines.append(f"# TYPE x2d_{gname} gauge")
        for printer, state in states.items():
            if not state:
                continue
            v = state
            for key in path:
                if not isinstance(v, dict) or key not in v:
                    v = None
                    break
                v = v[key]
            if v is None or not isinstance(v, (int, float)):
                continue
            lines.append(f'x2d_{gname}{{printer="{printer}"}} {v}')

    # AMS slot humidity (per slot) — common scrape target
    lines.append("# HELP x2d_ams_humidity AMS slot humidity rating (0=dry, 5=wet)")
    lines.append("# TYPE x2d_ams_humidity gauge")
    for printer, state in states.items():
        if not state:
            continue
        ams_list = (state.get("print", {}).get("ams", {}).get("ams") or [])
        for ams in ams_list:
            try:
                ams_id = ams.get("id", "?")
                hum = float(ams.get("humidity", 0))
                lines.append(
                    f'x2d_ams_humidity{{printer="{printer}",ams_id="{ams_id}"}} {hum}')
            except (ValueError, TypeError, AttributeError):
                continue

    body = "\n".join(lines) + "\n"
    return body.encode("utf-8")


_ACCESS_LOG_PATH = Path.home() / ".x2d" / "access.log"
_ACCESS_LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
_access_log_lock = _threading.Lock()


def _write_access_log(record: dict) -> None:
    """Append one JSON line to ~/.x2d/access.log; rotate to access.log.1
    when the active file exceeds 1 MiB. Single rotation slot — older
    rotated logs are overwritten. Match the bridge.log rotation scheme
    used by run_gui_clean.sh so operators see the same shape everywhere.
    """
    path = _ACCESS_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with _access_log_lock:
        try:
            if path.exists() and path.stat().st_size + len(line) > _ACCESS_LOG_MAX_BYTES:
                rotated = path.with_suffix(path.suffix + ".1")
                try:
                    if rotated.exists():
                        rotated.unlink()
                except OSError:
                    pass
                try:
                    path.rename(rotated)
                except OSError:
                    pass
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


_AUTH_COOKIE_NAME = "x2d_token"


def _parse_cookie(header: str, name: str) -> str:
    """Extract a single cookie value by name from a Cookie: header.
    Returns "" if not present. Tolerant of quotes and surrounding spaces."""
    if not header:
        return ""
    for part in header.split(";"):
        kv = part.strip().split("=", 1)
        if len(kv) == 2 and kv[0].strip() == name:
            v = kv[1].strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            return v
    return ""


def _check_bearer(handler, expected: str | None, host: str) -> bool:
    """Return True if the request is authorized. Loopback binds with
    no token configured stay open (single-user local case). Any
    non-loopback bind requires a token; missing/wrong token → 401
    with WWW-Authenticate. Sends the response on rejection so the
    caller just returns.

    Token may be presented in EITHER `Authorization: Bearer <token>` OR
    a `x2d_token=<token>` cookie. The cookie path is what the in-browser
    web UI (#48) uses so SSE/EventSource works (EventSource doesn't
    allow custom headers from JS). Static asset routes that don't need
    auth (login page bootstrap) bypass this check via the `bypass_auth`
    handler attr — see `do_GET`.
    """
    if not expected:
        if not _is_loopback(host):
            handler.send_response(401)
            handler.send_header("WWW-Authenticate", 'Bearer realm="x2d", '
                                'error="invalid_request", '
                                'error_description="--auth-token required for non-loopback binds"')
            handler.end_headers()
            return False
        return True
    presented = ""
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        presented = auth[len("Bearer "):].strip()
    if not presented:
        cookie_hdr = handler.headers.get("Cookie", "")
        presented = _parse_cookie(cookie_hdr, _AUTH_COOKIE_NAME)
    if not presented:
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Bearer realm="x2d"')
        handler.end_headers()
        return False
    # Constant-time compare so we don't leak token length via timing.
    import hmac
    if not hmac.compare_digest(presented, expected):
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Bearer realm="x2d", '
                            'error="invalid_token"')
        handler.end_headers()
        return False
    return True


_WEB_DIR_DEFAULT = Path(__file__).resolve().parent / "web"


def _serve_http(bind: str,
                get_state: Callable[[str], dict | None],
                get_last_ts: Callable[[str], float] | None = None,
                max_staleness: float = 30.0,
                auth_token: str | None = None,
                printer_names: list[str] | None = None,
                clients: dict | None = None,
                web_dir: Path | None = None) -> None:
    """Multi-printer HTTP server (item #36).

    `get_state` and `get_last_ts` now take a printer name (empty string
    for the default plain `[printer]` section). The HTTP layer parses
    `?printer=NAME` from the query string and forwards it. Routes:

      GET  /printers          → list of configured printer names (JSON)
      GET  /state             → state of default printer
      GET  /state?printer=lab → state of named "lab" printer
      GET  /healthz           → health of default printer
      GET  /healthz?printer=lab → health of named "lab" printer
      GET  /metrics           → Prometheus exposition (#38)
      GET  /                  → web UI (#46) — serves web/index.html
      GET  /index.html        → ditto
      GET  /index.js          → web UI client script
      GET  /index.css         → web UI styles
      GET  /state.events      → SSE: state JSON pushed every 1s (#46)
      POST /control/pause     → MQTT publish pause (#46)
      POST /control/resume    → MQTT publish resume (#46)
      POST /control/stop      → MQTT publish stop (#46)
      POST /control/light     → {"state":"on|off|flashing"} (#46)
      POST /control/temp      → {"target":"bed|nozzle|chamber","value":int,"idx":int?} (#46)
      POST /control/ams_load  → {"slot":int} (#46)

    `clients` (optional) maps printer name → live X2DClient so the
    POST /control/* routes can publish without re-dialing MQTT each time.
    Without it the control routes return 503.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse

    host_part, _, port_part = bind.rpartition(":")
    host = host_part or "127.0.0.1"
    port = int(port_part)
    names = list(printer_names) if printer_names else [""]
    web_dir = web_dir or _WEB_DIR_DEFAULT
    clients = clients or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # silence default stderr access log
            return

        def log_request(self, code='-', size='-'):
            # Item #39: emit one JSON line per request to
            # ~/.x2d/access.log with 1 MiB rotation. Replaces wsgi-style
            # apache combined-log; structured logs are easier to grep
            # and feed into log aggregators.
            try:
                _write_access_log({
                    "ts":          time.time(),
                    "method":      self.command or "?",
                    "path":        self.path,
                    "status":      int(code) if str(code).isdigit() else 0,
                    "size":        int(size) if str(size).isdigit() else None,
                    "duration_ms": round((time.time() - getattr(self, "_x2d_start", time.time())) * 1000, 2),
                    "printer":     getattr(self, "_x2d_printer", None),
                    "authed":      getattr(self, "_x2d_authed", None),
                    "client":      self.client_address[0] if self.client_address else None,
                })
            except Exception:
                # Never let logging take down the response.
                pass

        def _parse_printer(self) -> str:
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            return (qs.get("printer", [""])[0] or "")

        # ---- web UI helpers (#46) ---------------------------------
        _STATIC_MIME = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".svg":  "image/svg+xml",
            ".png":  "image/png",
            ".ico":  "image/x-icon",
        }
        _WEB_ALLOWED = {
            "/":           "index.html",
            "/index.html": "index.html",
            "/index.js":   "index.js",
            "/index.css":  "index.css",
            "/login.html": "login.html",
            "/login.js":   "login.js",
        }
        # The login flow needs to render BEFORE the user has a token,
        # so we serve these without the bearer/cookie check. Same for
        # /auth/info which the JS uses to detect "auth disabled" mode
        # (loopback + no token configured) and skip the login redirect.
        _AUTH_BYPASS_PATHS = {"/login.html", "/login.js", "/auth/info"}

        def _serve_static(self, fname: str) -> None:
            path = (web_dir / fname).resolve()
            try:
                # Refuse traversal beyond the web dir.
                path.relative_to(web_dir.resolve())
            except ValueError:
                self.send_response(403); self.end_headers(); return
            if not path.exists() or not path.is_file():
                self.send_response(404); self.end_headers(); return
            data = path.read_bytes()
            ctype = self._STATIC_MIME.get(path.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _serve_state_events(self, printer: str) -> None:
            """Server-Sent Events stream pushing the printer's state JSON
            every 1s and a `: ping\\n\\n` keepalive every 15s."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                last_sent: str | None = None
                ticks_since_send = 0
                while True:
                    state = get_state(printer)
                    body = json.dumps({"printer": printer,
                                        "state":   state or {},
                                        "ts":      time.time()},
                                       separators=(",", ":"))
                    if body != last_sent or ticks_since_send >= 15:
                        line = f"data: {body}\n\n".encode("utf-8")
                        self.wfile.write(line)
                        self.wfile.flush()
                        last_sent = body
                        ticks_since_send = 0
                    else:
                        ticks_since_send += 1
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Client disconnected or socket failed — exit cleanly so
                # the worker thread terminates.
                return

        def do_GET(self):
            self._x2d_start = time.time()
            self._x2d_printer = None
            cookie_token = _parse_cookie(self.headers.get("Cookie", ""),
                                          _AUTH_COOKIE_NAME)
            self._x2d_authed = (auth_token is not None) and (
                self.headers.get("Authorization", "").startswith("Bearer ")
                or bool(cookie_token))
            url = urllib.parse.urlparse(self.path)
            path = url.path
            # Item #48: /auth/info is a public probe so the JS can tell
            # whether the daemon is open (loopback + no token) or gated.
            # /login.html + /login.js are served WITHOUT the gate so the
            # user can reach the password prompt before having a token.
            if path == "/auth/info":
                payload = {
                    "auth_required": auth_token is not None,
                    "cookie_name":   _AUTH_COOKIE_NAME,
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path in self._AUTH_BYPASS_PATHS \
                    and path in self._WEB_ALLOWED:
                self._serve_static(self._WEB_ALLOWED[path])
                return
            if not _check_bearer(self, auth_token, host):
                return
            # /auth/check: token validated above; report success so the
            # login page knows it can persist + redirect.
            if path == "/auth/check":
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Web UI static assets (#46) — open once the bearer/cookie
            # check above passes.
            if path in self._WEB_ALLOWED:
                self._serve_static(self._WEB_ALLOWED[path])
                return
            if path == "/state.events":
                printer = self._parse_printer()
                self._x2d_printer = printer
                if printer not in names:
                    self.send_response(404)
                    self.end_headers()
                    return
                self._serve_state_events(printer)
                return
            if path == "/printers":
                body = json.dumps({"printers": names}, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/metrics":
                # Item #38: Prometheus text exposition format.
                states_snap = {n: get_state(n) for n in names}
                last_ts_snap = {n: (get_last_ts(n) if get_last_ts else 0.0)
                                for n in names}
                body = _format_prometheus_metrics(states_snap, last_ts_snap)
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            printer = self._parse_printer()
            self._x2d_printer = printer
            if printer not in names:
                err = json.dumps({"error": f"unknown printer {printer!r}",
                                  "available": names}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            if path == "/state":
                state = get_state(printer)
                body = json.dumps(state or {}, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/healthz":
                # 200 if we've heard from the printer recently;
                # 503 if MQTT silently disconnected. Used as a Home
                # Assistant binary_sensor or a uptime-monitor poll
                # target. JSON body for diagnostics.
                last = get_last_ts(printer) if get_last_ts else 0.0
                age = time.time() - last if last else float("inf")
                healthy = age <= max_staleness
                payload = {
                    "printer":           printer,
                    "healthy":           healthy,
                    "last_message_ts":   last,
                    "last_message_age_s": None if last == 0.0 else round(age, 2),
                    "max_staleness_s":   max_staleness,
                }
                body = json.dumps(payload, indent=2).encode()
                self.send_response(200 if healthy else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def _read_body_json(self) -> dict | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > 64 * 1024:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _publish_via_client(self, printer: str, payload: dict) -> tuple[int, dict]:
            cli = clients.get(printer)
            if cli is None:
                return 503, {"error": "no live MQTT client for printer "
                              f"{printer!r}; run with --http on the daemon"}
            try:
                cli.publish(payload)
            except Exception as e:
                return 502, {"error": f"publish failed: {e}",
                              "payload": payload}
            return 200, {"ok": True, "printer": printer, "payload": payload}

        def do_POST(self):
            self._x2d_start = time.time()
            self._x2d_printer = None
            self._x2d_authed = (auth_token is not None) and bool(
                self.headers.get("Authorization", "").startswith("Bearer "))
            if not _check_bearer(self, auth_token, host):
                return
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if not path.startswith("/control/"):
                self.send_response(404); self.end_headers(); return
            verb = path[len("/control/"):]
            printer = self._parse_printer()
            self._x2d_printer = printer
            if printer not in names:
                self._send_json({"error": f"unknown printer {printer!r}",
                                  "available": names}, status=404)
                return
            body = self._read_body_json()
            if body is None:
                self._send_json({"error": "body must be JSON ≤64 KiB"},
                                  status=400)
                return
            if verb == "pause":
                payload = _print_cmd("pause", param="")
            elif verb == "resume":
                payload = _print_cmd("resume", param="")
            elif verb == "stop":
                payload = _print_cmd("stop", param="")
            elif verb == "light":
                state = (body or {}).get("state", "")
                if state not in ("on", "off", "flashing"):
                    self._send_json({"error":
                        "state must be on/off/flashing"}, status=400)
                    return
                payload = _system_cmd(
                    "ledctrl", led_node="chamber_light", led_mode=state,
                    led_on_time=int(body.get("on_time", 500)),
                    led_off_time=int(body.get("off_time", 500)),
                    loop_times=int(body.get("loops", 0)),
                    interval_time=int(body.get("interval", 0)))
            elif verb == "temp":
                target = (body or {}).get("target", "")
                value = body.get("value")
                if target not in ("bed", "nozzle", "chamber") \
                        or not isinstance(value, (int, float)):
                    self._send_json({"error":
                        "expected target=bed|nozzle|chamber + value=int"},
                                      status=400)
                    return
                if target == "bed":
                    payload = _print_cmd("set_bed_temp", temp=int(value))
                elif target == "nozzle":
                    payload = _print_cmd(
                        "set_nozzle_temp",
                        extruder_index=int(body.get("idx", 0)),
                        target_temp=int(value))
                else:  # chamber
                    payload = _print_cmd(
                        "gcode_line", param=f"M141 S{int(value)}\n")
            elif verb == "ams_load":
                slot = body.get("slot")
                if not isinstance(slot, int) or not 1 <= slot <= 16:
                    self._send_json({"error":
                        "slot must be int 1..16"}, status=400)
                    return
                # MachineObject::command_ams_change_filament — DeviceManager.cpp:1700
                payload = _print_cmd(
                    "ams_change_filament",
                    target=int(slot) - 1,         # 0-indexed in mqtt
                    curr_temp=int(body.get("curr_temp", 215)),
                    tar_temp=int(body.get("tar_temp", 215)))
            elif verb == "gcode":
                line = (body or {}).get("line", "")
                if not isinstance(line, str) or not line.strip():
                    self._send_json({"error":
                        "expected {\"line\": \"<g-code>\"}"}, status=400)
                    return
                payload = _print_cmd(
                    "gcode_line",
                    param=line if line.endswith("\n") else line + "\n")
            else:
                self._send_json({"error": f"unknown control verb {verb!r}",
                                  "supported": ["pause", "resume", "stop",
                                                "light", "temp", "ams_load",
                                                "gcode"]},
                                  status=404)
                return
            status, resp = self._publish_via_client(printer, payload)
            self._send_json(resp, status=status)

    server = ThreadingHTTPServer((host, port), Handler)
    auth_state = "auth required" if auth_token else \
                 ("OPEN — loopback only" if _is_loopback(host) else "OPEN — exposed; pass --auth-token to require Bearer")
    print(f"[x2d-bridge] HTTP listening on http://{host}:{port}/state "
          f"(+ /healthz + /printers, max-staleness {max_staleness}s; "
          f"{auth_state}; printers={names})",
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
        # Item #29: cache the most recent state push so a fresh shim
        # subscriber can replay it immediately and DeviceManager populates
        # MachineObject (AMS, temps, lights, etc.) without waiting up to
        # 30s for the next push.
        self._latest_state: dict | None = None
        self.client = X2DClient(
            Creds(ip=dev_ip, code=code, serial=dev_id),
            on_state=self._dispatch_state,
        )

    def _dispatch_state(self, payload: dict) -> None:
        with self._lock:
            self._latest_state = payload
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(payload)
            except Exception as e:  # one bad subscriber shouldn't poison others
                print(f"[serve] state listener raised: {e}", file=sys.stderr)

    def latest_state(self) -> dict | None:
        with self._lock:
            return self._latest_state

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
        self._ssdp_listeners: list[Callable[[dict], None]] = []
        self._ssdp_lock = __import__("threading").Lock()
        # Cache of {dev_id: parsed_dict} so we can re-emit the most-recent
        # SSDP notify to a newly-connecting shim without waiting for the
        # printer's next 30-second broadcast.
        self._ssdp_cache: dict[str, dict] = {}
        self._ssdp_thread: Thread | None = None
        # Item #40: serial → (code, name) map loaded from ~/.x2d/credentials
        # so the SSDP loop can recognise our own printers when their NOTIFY
        # arrives and open the MQTT subscription proactively.
        self._known_creds: dict[str, tuple[str, str]] = self._load_known_creds()
        # Refcount holder: any session opened proactively from SSDP (item #40)
        # gets one persistent acquire() so the connection survives across
        # shim subscribe/unsubscribe cycles. Released on serve_forever exit.
        self._proactive_sessions: dict[str, _PrinterSession] = {}

    @staticmethod
    def _load_known_creds() -> dict[str, tuple[str, str]]:
        """Read every [printer] / [printer:NAME] section in
        ~/.x2d/credentials and return {serial: (code, name)}. Quietly
        returns {} if the file is missing or malformed — the bridge stays
        usable for unrecognised printers via the lazy shim path."""
        path = Path.home() / ".x2d" / "credentials"
        if not path.exists():
            return {}
        cp = configparser.ConfigParser()
        try:
            cp.read(path)
        except configparser.Error:
            return {}
        out: dict[str, tuple[str, str]] = {}
        for section in cp.sections():
            if section == "printer":
                name = ""
            elif section.startswith("printer:"):
                name = section.split(":", 1)[1]
            else:
                continue
            serial = cp.get(section, "serial", fallback="").strip()
            code = cp.get(section, "code", fallback="").strip()
            if serial and code:
                out[serial] = (code, name)
        return out

    # --- SSDP discovery -----------------------------------------------

    def add_ssdp_listener(self, fn: Callable[[dict], None]) -> None:
        with self._ssdp_lock:
            self._ssdp_listeners.append(fn)
            cache = list(self._ssdp_cache.values())
        # Replay the cache so a fresh shim sees existing devices immediately.
        for parsed in cache:
            try:
                fn(parsed)
            except Exception as e:
                print(f"[serve] ssdp replay raised: {e}", file=sys.stderr)

    def remove_ssdp_listener(self, fn: Callable[[dict], None]) -> None:
        with self._ssdp_lock:
            try:
                self._ssdp_listeners.remove(fn)
            except ValueError:
                pass

    def _ensure_ssdp_thread(self) -> None:
        if self._ssdp_thread and self._ssdp_thread.is_alive():
            return
        t = Thread(target=self._ssdp_loop, name="ssdp", daemon=True)
        t.start()
        self._ssdp_thread = t

    def _seed_appconfig_for_ssdp(self, parsed: dict) -> None:
        """Item #17: when we see the FIRST SSDP NOTIFY of the bridge's
        lifetime, ensure the user's BambuStudio.conf has a Bambu vendor
        preset selected. Without this, freshly-installed users land on
        the missing_connection.html fallback even though their printer
        is broadcasting itself.

        Idempotent: a marker file at ~/.x2d/.ssdp_seeded prevents
        re-patching across bridge restarts. Atomic write so a crash
        mid-write doesn't corrupt the user's AppConfig."""
        import os as _os
        marker = Path.home() / ".x2d" / ".ssdp_seeded"
        if marker.exists():
            return
        appconf = Path.home() / ".config" / "BambuStudioInternal" / "BambuStudio.conf"
        if not appconf.exists() or appconf.stat().st_size == 0:
            # No AppConfig yet — install.sh will seed it on next install
            # run. We can't sensibly create one out of nothing here.
            return
        try:
            data = json.loads(appconf.read_text())
        except (json.JSONDecodeError, OSError):
            return  # Don't touch a config we can't parse.
        presets = data.setdefault("presets", {})
        current = presets.get("printer", "")
        # If already on a Bambu vendor preset, leave alone.
        if current.lower().startswith("bambu lab"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            return
        # Patch the same gate keys install.sh #11 sets — defaults to the
        # X2D since that's what this toolkit is for. The upstream BBL
        # profile catalogue ships full X2D variants (machine, filaments,
        # 0.20mm Standard process), so the GUI lands directly on the
        # right model without the user having to pick.
        data.setdefault("vendors", {})["BBL"] = "1"
        models = data.get("models") or []
        if not any(m.get("vendor") == "BBL" for m in models):
            models.append({
                "vendor": "BBL",
                "model": "Bambu Lab X2D",
                "nozzle_diameter": '"0.4"',
            })
            data["models"] = models
        presets["printer"]   = "Bambu Lab X2D 0.4 nozzle"
        presets["filament"]  = "Bambu PLA Basic @BBL X2D"
        presets.setdefault("print", "0.20mm Standard @BBL X2D")
        if not isinstance(presets.get("filaments"), list) or not presets["filaments"]:
            presets["filaments"] = ["Bambu PLA Basic @BBL X2D"]
        # Atomic write
        tmp = appconf.with_suffix(appconf.suffix + ".tmp-x2d")
        tmp.write_text(json.dumps(data, indent=4))
        _os.replace(tmp, appconf)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        print(f"[serve] ssdp seed: patched {appconf} (printer→{presets['printer']}, "
              f"triggered by {parsed.get('dev_name', '?')} @ {parsed.get('dev_ip', '?')})",
              file=sys.stderr)

    def _ssdp_loop(self) -> None:
        """Listen for Bambu's multicast NOTIFY broadcasts on UDP 2021
        and convert each into the JSON shape BambuStudio's
        DeviceManager::on_machine_alive expects."""
        import socket as _socket
        import struct as _struct
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM, _socket.IPPROTO_UDP)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            sock.bind(("", 2021))
            sock.setsockopt(_socket.IPPROTO_IP, _socket.IP_ADD_MEMBERSHIP,
                            _struct.pack("4sl",
                                         _socket.inet_aton("239.255.255.250"),
                                         _socket.INADDR_ANY))
            sock.settimeout(1.0)
        except OSError as e:
            print(f"[serve] ssdp bind failed: {e}", file=sys.stderr)
            return
        print("[serve] ssdp listening on udp/2021 (239.255.255.250)", file=sys.stderr)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except (_socket.timeout, BlockingIOError):
                continue
            except OSError:
                break
            parsed = self._parse_ssdp(data, addr[0])
            if parsed is None:
                continue
            with self._ssdp_lock:
                self._ssdp_cache[parsed["dev_id"]] = parsed
            _metric_global_inc("ssdp_notifies_total")
            with self._ssdp_lock:
                listeners = list(self._ssdp_listeners)
            # Item #40: proactive auto-connect. If this NOTIFY's USN
            # matches a credentials section's serial, open the MQTT
            # subscription before any shim asks. _PrinterSession is
            # refcounted, so a persistent acquire() here keeps the
            # connection live across shim subscribe/unsubscribe cycles
            # — and the cached state replay (#29) means the GUI's
            # StatusPanel populates within milliseconds of subscribe.
            try:
                self._maybe_auto_connect(parsed)
            except Exception as e:
                print(f"[serve] ssdp auto-connect failed: {e}", file=sys.stderr)
            # Fire-and-forget: ensure the AppConfig has a Bambu preset
            # so the GUI's Device tab works on first launch (#17).
            try:
                self._seed_appconfig_for_ssdp(parsed)
            except Exception as e:
                print(f"[serve] ssdp seed failed: {e}", file=sys.stderr)
            for fn in listeners:
                try:
                    fn(parsed)
                except Exception as e:
                    print(f"[serve] ssdp listener raised: {e}", file=sys.stderr)

    @staticmethod
    def _parse_ssdp(data: bytes, src_ip: str) -> dict | None:
        """Extract the on_machine_alive fields from a Bambu NOTIFY.
        Format example:
            NOTIFY * HTTP/1.1\r\n
            Location: 192.168.x.y\r\n
            USN: <serial>\r\n
            DevModel.bambu.com: N6\r\n
            DevName.bambu.com: x2d\r\n
            DevConnect.bambu.com: cloud|lan\r\n
            DevBind.bambu.com: free|occupied\r\n
            Devseclink.bambu.com: secure\r\n
            DevVersion.bambu.com: 01.01.00.00\r\n
        """
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return None
        if not text.startswith("NOTIFY "):
            return None
        headers: dict[str, str] = {}
        for line in text.split("\r\n")[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        usn = headers.get("usn", "")
        if not usn:
            return None
        dev_ip = headers.get("location", src_ip) or src_ip
        connect_type = headers.get("devconnect.bambu.com", "lan").lower()
        if connect_type == "cloud":
            # The bridge replaces what the cloud plug-in would do, so
            # tell the host this is reachable as a LAN device.
            connect_type = "lan"
        return {
            "dev_name":       headers.get("devname.bambu.com", ""),
            "dev_id":         usn,
            "dev_ip":         dev_ip,
            "dev_type":       headers.get("devmodel.bambu.com", ""),
            "dev_signal":     "",  # Bambu doesn't advertise signal strength in SSDP
            "connect_type":   connect_type,
            "bind_state":     headers.get("devbind.bambu.com", "free").lower(),
            "sec_link":       headers.get("devseclink.bambu.com", ""),
            "ssdp_version":   headers.get("devversion.bambu.com", ""),
            "connection_name": "",
        }

    def _maybe_auto_connect(self, parsed: dict) -> None:
        """Item #40: open MQTT proactively when an SSDP NOTIFY matches a
        known credentials section. Idempotent — only one persistent
        acquire() per serial, so repeated NOTIFYs (every ~30s) don't
        rack up the refcount. IP changes are tolerated because
        get_or_open_printer rebuilds the session on mismatch."""
        dev_id = parsed.get("dev_id", "")
        dev_ip = parsed.get("dev_ip", "")
        if not dev_id or not dev_ip:
            return
        creds = self._known_creds.get(dev_id)
        if creds is None:
            return
        code, _name = creds
        with self._printers_lock:
            existing = self._proactive_sessions.get(dev_id)
            existing_ip = existing.dev_ip if existing else None
        # If we already hold a proactive ref AND IP is unchanged → done.
        if existing is not None and existing_ip == dev_ip:
            return
        # Either fresh or IP changed; acquire (will rebuild on IP mismatch).
        try:
            sess = self.get_or_open_printer(dev_id, dev_ip, code)
        except _OpError as e:
            print(f"[serve] auto-connect {dev_id}@{dev_ip} failed: {e}",
                  file=sys.stderr)
            return
        with self._printers_lock:
            stale = self._proactive_sessions.get(dev_id)
            self._proactive_sessions[dev_id] = sess
        # Drop the previous proactive ref now that the new one is in place.
        if stale is not None and stale is not sess:
            try:
                stale.release()
            except Exception:
                pass
        print(f"[serve] auto-connect {dev_id}@{dev_ip} (proactive, "
              f"matched creds section {_name or '<default>'!r})",
              file=sys.stderr)

    def _release_proactive_sessions(self) -> None:
        """Drop the persistent SSDP-driven refs at shutdown so MQTT
        connections close cleanly."""
        with self._printers_lock:
            sessions = list(self._proactive_sessions.values())
            self._proactive_sessions.clear()
        for sess in sessions:
            try:
                sess.release()
            except Exception:
                pass

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

        # Start SSDP discovery up-front so the AppConfig auto-pop (#17)
        # fires even when no shim has connected yet (e.g. when run_gui.sh's
        # watchdog brought us up before bambu-studio's plug-in load).
        self._ensure_ssdp_thread()

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
        # Drop SSDP-driven proactive refs (#40) before the bulk close
        # so refcounts don't underflow when we hit the disconnect loop.
        self._release_proactive_sessions()
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
        self._ssdp_cb: Callable[[dict], None] | None = None

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
                    sess.remove_listener(self._state_cb)
                if self._connect_cb:
                    sess.remove_connect_listener(self._connect_cb)
                sess.release()
        if self._ssdp_cb is not None:
            self.server.remove_ssdp_listener(self._ssdp_cb)
            self._ssdp_cb = None
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

    def listener(p: dict) -> None:
        h._emit_local_message(dev_id, p)

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


def _op_start_discovery(h: _ConnHandler, args: dict) -> dict:
    """Begin (or stop) SSDP listener; pipe each parsed device to this
    shim as `evt:ssdp_msg`. Idempotent — re-arming twice doesn't
    duplicate listeners."""
    enable = bool(args.get("start", True))
    if not enable:
        # Tear down this shim's listener.
        if h._ssdp_cb is not None:
            h.server.remove_ssdp_listener(h._ssdp_cb)
            h._ssdp_cb = None
        return {}

    h.server._ensure_ssdp_thread()
    if h._ssdp_cb is None:
        def emit(parsed: dict) -> None:
            h._send({
                "kind": "evt",
                "name": "ssdp_msg",
                "data": {"json": json.dumps(parsed, separators=(",", ":"))},
            })
        h._ssdp_cb = emit
        h.server.add_ssdp_listener(emit)
    return {}


def _op_subscribe_local(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    interval = int(args.get("interval_s", 5))
    enable = bool(args.get("enable", True))
    sess = h.server._printers.get(dev_id) if dev_id else None
    if sess is None:
        raise _OpError(-1, "printer not connected")
    if enable:
        # Item #29: replay cached state immediately so DeviceManager
        # populates MachineObject (AMS slots, temps, lights, etc.)
        # without waiting up to 30s for the next live push. The cached
        # state was set by _PrinterSession._dispatch_state from a prior
        # MQTT push (typically the initial pushall after connect).
        cached = sess.latest_state()
        if cached is not None:
            try:
                h._emit_local_message(dev_id, cached)
            except Exception as e:
                print(f"[serve] state replay raised: {e}", file=sys.stderr)
        # The X2DClient already listens for state pushes once subscribed
        # in connect; kick a fresh pushall here for good measure.
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


def _cloud_client():
    """Lazy-load the cloud_client module + session. Returns None if the
    module isn't importable (older install without the file) so the
    bridge stays alive even when cloud is broken."""
    try:
        import cloud_client  # noqa: WPS433 — intentional lazy import
        return cloud_client.CloudClient.load_or_anonymous()
    except Exception as e:
        print(f"[serve] cloud_client unavailable: {e}", file=sys.stderr)
        return None


def _op_login_status(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    return {"logged_in": bool(cli and cli.is_logged_in())}


def _op_user_id(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            return {"id": cli.get_user_id()}
        except Exception as e:
            print(f"[serve] get_user_id failed: {e}", file=sys.stderr)
    return {"id": ""}


def _op_user_presets(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            return {"presets": cli.get_user_presets()}
        except Exception as e:
            print(f"[serve] get_user_presets failed: {e}", file=sys.stderr)
    # Anonymous fallback: load the BBL filament JSONs that ship with
    # bambu-studio plus a small community-curated set, so the GUI's
    # AMS spool dropdown isn't empty for users who haven't signed in.
    return {"presets": _load_local_presets()}


def _stringify_preset_values(d: dict) -> dict:
    """PresetBundle::load_user_presets expects every value as a string
    (or list of strings, which it joins). Re-encode any non-string
    leaf values so the dict round-trips correctly."""
    out: dict[str, str] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, list):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, (int, float, bool)):
            out[k] = str(v).lower() if isinstance(v, bool) else str(v)
        elif v is None:
            out[k] = ""
        else:
            out[k] = json.dumps(v)
    return out


def _x2d_search_roots() -> list[Path]:
    """Candidate roots for shipped data files. Try (in order):
    - the script's own directory (dev tree, x2d_bridge.py at repo root)
    - the parent (dist tree, x2d_bridge.py at <root>/helpers/)
    so the same code finds files in either layout."""
    here = Path(__file__).parent
    return [here, here.parent]


def _local_preset_dirs() -> list[Path]:
    """Where to look for shipped BBL filament profiles. The first
    candidate that exists wins; the rest are silently skipped so this
    works in both the dev tree (bs-bionic/...) and the unpacked
    tarball (resources/...)."""
    dirs: list[Path] = []
    for root in _x2d_search_roots():
        dirs.append(root / "resources" / "profiles" / "BBL" / "filament")
        dirs.append(root / "bs-bionic" / "resources" / "profiles" / "BBL" / "filament")
    return dirs


def _load_local_presets() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}

    # Community-curated presets — small JSON shipped under runtime/.
    # Same dev-vs-dist multi-root lookup as the BBL profile dirs.
    community = None
    for root in _x2d_search_roots():
        cand = root / "runtime" / "network_shim" / "data" / "community_filaments.json"
        if cand.exists():
            community = cand
            break
    if community is not None:
        try:
            blob = json.loads(community.read_text())
            for name, raw in blob.items():
                if name.startswith("_"):  # comment keys
                    continue
                if not isinstance(raw, dict):
                    continue
                out[name] = _stringify_preset_values(raw)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[serve] local presets: bad community json: {e}", file=sys.stderr)

    # Vendor-shipped BBL filaments — every "instantiation":"true" entry.
    for d in _local_preset_dirs():
        if not d.is_dir():
            continue
        for jf in sorted(d.glob("*.json")):
            try:
                raw = json.loads(jf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if raw.get("instantiation") != "true":
                continue
            name = raw.get("name") or jf.stem
            # Already loaded? Community version wins.
            if name in out:
                continue
            out[name] = _stringify_preset_values(raw)
        break  # only the first directory that exists

    return out


def _op_user_tasks(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            limit = int(args.get("limit", 20))
            return {"tasks": cli.get_user_tasks(limit=limit)}
        except Exception as e:
            print(f"[serve] get_user_tasks failed: {e}", file=sys.stderr)
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
    "start_discovery":             _op_start_discovery,
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


# ---------------------------------------------------------------------------
# `camera` subcommand — RTSPS-to-MJPEG proxy.
#
# Bambu's GUI streams the printer's chamber camera via either:
#   * `rtsps://bblp:<code>@<ip>:322/streaming/live/1`
#     — standard RTSPS, works iff the printer's own "LAN-mode liveview"
#     toggle is enabled (Settings → Network → Liveview on the touchscreen,
#     OR the `ipcam.rtsp_url` field comes back as a real URL instead of
#     "disable" in the printer's pushed state).
#   * The closed proprietary "LVL_Local" protocol on TCP port 6000, only
#     speakable through the x86_64-only libBambuSource.so. Not usable on
#     aarch64 until that protocol is reverse-engineered.
#
# This subcommand wraps the RTSPS path with ffmpeg → MJPEG-over-HTTP so a
# phone browser at http://127.0.0.1:8766/cam.mjpeg sees the stream live.
# Multiple browser clients tee off the same single ffmpeg subprocess.
# Surfaces a clear error when the printer reports rtsp_url=disable.
# ---------------------------------------------------------------------------

def cmd_camera(args: argparse.Namespace) -> int:
    import http.server
    import shutil
    import signal as _signal
    import socketserver
    import subprocess as _sp
    from threading import Lock as _Lock

    creds = Creds.resolve(args)

    # Pre-flight: poke the printer's state to confirm RTSP is enabled.
    if not args.skip_check:
        try:
            cli = X2DClient(creds)
            cli.connect(timeout=8.0)
            state = cli.request_state(timeout=8.0)
            cli.disconnect()
            ipcam = state.get("print", {}).get("ipcam", {})
            rtsp_url = ipcam.get("rtsp_url", "disable")
            if rtsp_url == "disable":
                print(
                    "[camera] printer reports ipcam.rtsp_url=\"disable\".\n"
                    "         Enable LAN-mode liveview on the printer's\n"
                    "         touchscreen (Settings → Network → Liveview)\n"
                    "         and re-run. Or pass --skip-check to try anyway.",
                    file=sys.stderr,
                )
                return 2
            elif rtsp_url and not rtsp_url.startswith(("rtsp://", "rtsps://")):
                print(f"[camera] unexpected ipcam.rtsp_url: {rtsp_url}",
                      file=sys.stderr)
                return 2
            print(f"[camera] printer rtsp_url=ok ({rtsp_url[:40]}...)",
                  file=sys.stderr)
        except Exception as e:
            print(f"[camera] state-pre-flight failed: {e} — continuing anyway",
                  file=sys.stderr)

    if shutil.which("ffmpeg") is None:
        print("[camera] ffmpeg not installed. `pkg install ffmpeg` first.",
              file=sys.stderr)
        return 2

    rtsp_url_full = (
        f"rtsps://bblp:{creds.code}@{creds.ip}:{args.port}/streaming/live/1"
    )

    # Single shared frame buffer + cv. ffmpeg writes JPEG frames here;
    # every HTTP client reads the latest. We never queue history — old
    # frames are dropped, viewers see live.
    state_lock = _Lock()
    latest_frame = {"data": b"", "ts": 0.0}
    stop_event = Event()

    # HLS output dir (item #20). Each segment is ~2s of mpegts; we keep
    # a sliding window of 6 (12s of buffer) and let ffmpeg auto-delete
    # older ones via -hls_flags delete_segments. Cleaned up at exit.
    import tempfile as _tempfile
    hls_dir = Path(_tempfile.mkdtemp(prefix="x2d-hls-"))
    hls_playlist = hls_dir / "cam.m3u8"
    hls_segment_pattern = hls_dir / "cam%04d.ts"

    def ffmpeg_pump():
        backoff = 1.0
        while not stop_event.is_set():
            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url_full,
                # Output 1: MJPEG-on-stdout, consumed by the JPEG buffer
                # below for /cam.mjpeg + /cam.jpg.
                "-map", "0:v",
                "-an",
                "-c:v", "mjpeg",
                "-q:v", "5",
                "-f", "image2pipe",
                "-update", "1",
                "pipe:1",
                # Output 2: HLS segments + playlist for /cam.m3u8.
                # -c:v copy when the input is already H.264 (the X2D's
                # RTSPS stream); ffmpeg falls back to re-encode if not.
                "-map", "0:v",
                "-an",
                "-c:v", "copy",
                "-f", "hls",
                "-hls_time", "2",
                "-hls_list_size", "6",
                "-hls_flags", "delete_segments+append_list+omit_endlist",
                "-hls_segment_filename", str(hls_segment_pattern),
                str(hls_playlist),
            ]
            print(f"[camera] spawning ffmpeg (port {args.port})", file=sys.stderr)
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE,
                             close_fds=True)
            try:
                jpeg_buf = b""
                while not stop_event.is_set():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        err = proc.stderr.read().decode(errors="replace")[-400:]
                        print(f"[camera] ffmpeg eof; stderr tail: {err}",
                              file=sys.stderr)
                        break
                    jpeg_buf += chunk
                    # MJPEG single-image output writes back-to-back JPEGs.
                    # Split on SOI marker (0xFFD8) — keep the most-recent
                    # complete frame.
                    while True:
                        idx = jpeg_buf.find(b"\xff\xd8", 1)
                        if idx == -1:
                            break
                        frame, jpeg_buf = jpeg_buf[:idx], jpeg_buf[idx:]
                        if frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"):
                            with state_lock:
                                latest_frame["data"] = frame
                                latest_frame["ts"]   = time.time()
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
            if stop_event.is_set():
                break
            print(f"[camera] reconnecting in {backoff:.1f}s", file=sys.stderr)
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def lvl_local_pump():
        # Push module path so a repo-checkout install also imports it.
        # Same dev-vs-dist multi-root lookup as _x2d_search_roots().
        for root in _x2d_search_roots():
            cand = root / "runtime" / "network_shim"
            if (cand / "lvl_local.py").exists():
                sys.path.insert(0, str(cand))
                break
        try:
            import lvl_local
        except ImportError as e:
            print(f"[camera] lvl_local module unavailable: {e}", file=sys.stderr)
            return

        def _store(jpeg, ts):
            if stop_event.is_set():
                raise SystemExit
            with state_lock:
                latest_frame["data"] = jpeg
                latest_frame["ts"] = time.time()

        try:
            lvl_local.stream_frames(creds.ip, creds.code, on_frame=_store)
        except SystemExit:
            pass
        except lvl_local.LVLLocalError as e:
            # Fatal vs transient is hard to know — surface and let the
            # outer reconnect logic in stream_frames handle the retry
            # (which it does until it gets a non-LVLLocalError).
            print(f"[camera] LVL_Local fatal: {e}", file=sys.stderr)

    if args.proto == "local":
        print("[camera] proto=local — using TLS:6000 LVL_Local stream", file=sys.stderr)
        pump = Thread(target=lvl_local_pump, name="camera-pump-local", daemon=True)
    else:
        pump = Thread(target=ffmpeg_pump, name="camera-pump", daemon=True)
    pump.start()

    # Tiny HTTP server. Two endpoints:
    #   /cam.mjpeg  → multipart/x-mixed-replace (browser-renderable)
    #   /cam.jpg    → single latest JPEG
    class CameraHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): return
        def do_GET(self):  # noqa: N802
            if not _check_bearer(self, args.auth_token or None, host):
                return
            if self.path in ("/cam.mjpeg", "/"):
                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                last_ts = 0.0
                try:
                    while not stop_event.is_set():
                        with state_lock:
                            frame = latest_frame["data"]
                            ts    = latest_frame["ts"]
                        if frame and ts > last_ts:
                            last_ts = ts
                            self.wfile.write(b"--frame\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(
                                f"Content-Length: {len(frame)}\r\n\r\n".encode())
                            self.wfile.write(frame)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                        else:
                            time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    return
            elif self.path == "/cam.jpg":
                with state_lock:
                    frame = latest_frame["data"]
                if not frame:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
            elif self.path == "/cam.m3u8":
                # HLS playlist (item #20). 503 until ffmpeg has emitted
                # at least one segment and the playlist file exists.
                if not hls_playlist.exists():
                    self.send_response(503)
                    self.end_headers()
                    return
                try:
                    body = hls_playlist.read_bytes()
                except OSError:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/cam") and self.path.endswith(".ts"):
                # HLS segment. Validate the filename to prevent path
                # traversal (only `cam<digits>.ts` shape allowed).
                seg_name = self.path[1:]  # strip leading slash
                import re as _re
                if not _re.fullmatch(r"cam\d+\.ts", seg_name):
                    self.send_response(404)
                    self.end_headers()
                    return
                seg = hls_dir / seg_name
                if not seg.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    body = seg.read_bytes()
                except OSError:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "max-age=10")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    host, _, port = args.bind.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)
    server = ThreadingServer((host, port), CameraHandler)

    def _stop(signum, frame):  # noqa: ARG001
        stop_event.set()
        server.shutdown()
    _signal.signal(_signal.SIGINT,  _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    print(f"[camera] streaming at http://{host}:{port}/cam.mjpeg "
          f"(JPEG snapshot at /cam.jpg, HLS at /cam.m3u8). Ctrl-C to quit.",
          file=sys.stderr)
    print(f"[camera] HLS segments → {hls_dir}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        # HLS cleanup — best-effort, don't propagate errors.
        import shutil as _shutil
        try:
            _shutil.rmtree(hls_dir, ignore_errors=True)
        except Exception:
            pass
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    sock_path = Path(args.sock).expanduser()
    server = ServeServer(sock_path)
    return server.serve_forever()


def cmd_daemon(args: argparse.Namespace) -> int:
    """Multi-printer daemon (item #36).

    Spawns one X2DClient per printer section in ~/.x2d/credentials. If
    --printer is passed, only that one is started. State, last_message_ts
    and pushall polling are tracked per printer. The HTTP server routes
    `?printer=NAME` to the matching client. Connection failures are
    isolated: a single unreachable printer doesn't take down the others.
    """
    # Determine the set of printers to drive.
    if args.printer:
        names_to_run: list[str] = [args.printer]
    else:
        names = Creds.list_names()
        names_to_run = names if names else [""]
    # Per-printer state cache + clients.
    states: dict[str, dict | None] = {n: None for n in names_to_run}

    def make_on_state(name: str):
        def on_state(state: dict) -> None:
            states[name] = state
            if not args.quiet:
                print(json.dumps({"ts": time.time(),
                                  "printer": name,
                                  "state": state}), flush=True)
        return on_state

    clients: dict[str, X2DClient] = {}
    failed: list[tuple[str, str]] = []
    for name in names_to_run:
        try:
            ns = argparse.Namespace(ip=None, code=None, serial=None,
                                    printer=(name or None))
            creds = Creds.resolve(ns)
            cli = X2DClient(creds, on_state=make_on_state(name))
            cli.connect()
            clients[name] = cli
            print(f"[x2d-bridge] {name or '<default>'}: connected to {creds.ip}",
                  file=sys.stderr)
        except SystemExit as e:
            failed.append((name, f"creds resolve failed (exit {e.code})"))
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[x2d-bridge] {name or '<default>'}: connect failed: {e} "
                  f"— other printers continue", file=sys.stderr)
    if not clients:
        print(f"[x2d-bridge] no printers reachable: {failed}", file=sys.stderr)
        return 2

    def _safe_pushall(name: str, cli: X2DClient) -> None:
        try:
            cli.publish({"pushing": {"sequence_id": _next_seq(),
                                     "command": "pushall"}})
        except Exception as e:
            print(f"[x2d-bridge] {name or '<default>'}: pushall failed: {e}",
                  file=sys.stderr)

    for name, cli in clients.items():
        _safe_pushall(name, cli)

    if args.http:
        def get_state(printer: str):
            return states.get(printer)

        def get_last_ts(printer: str):
            cli = clients.get(printer)
            return cli.last_message_ts if cli else 0.0

        Thread(target=_serve_http,
               kwargs={"bind": args.http, "get_state": get_state,
                       "get_last_ts": get_last_ts,
                       "max_staleness": float(args.max_staleness),
                       "auth_token": args.auth_token or None,
                       "printer_names": list(clients.keys()),
                       "clients": clients,
                       "web_dir": _WEB_DIR_DEFAULT},
               daemon=True).start()

    period = max(1, int(args.interval))
    print(f"[x2d-bridge] daemon up; {len(clients)} printer(s); polling every "
          f"{period}s. Ctrl-C / SIGTERM to quit.", file=sys.stderr)

    import signal as _signal
    stop = Event()

    def _handle_sig(signum, frame):  # noqa: ARG001
        stop.set()

    _signal.signal(_signal.SIGINT, _handle_sig)
    _signal.signal(_signal.SIGTERM, _handle_sig)

    while not stop.is_set():
        if stop.wait(period):
            break
        for name, cli in clients.items():
            _safe_pushall(name, cli)
    for cli in clients.values():
        try:
            cli.disconnect()
        except Exception:
            pass
    return 0


def cmd_webrtc(args: argparse.Namespace) -> int:
    """Run the WebRTC video gateway (item #45). Pulls JPEG frames from
    a running camera daemon and re-publishes them as a live VP8/H.264
    track over WebRTC. Sub-second latency vs HLS's ~6-8 s.

    The signaling endpoint is POST /cam.webrtc/offer; the static viewer
    page is GET /cam.webrtc.html.
    """
    try:
        from runtime.webrtc.server import run as _run_webrtc
    except ImportError as e:
        print(f"[x2d-bridge] webrtc deps missing: {e}\n"
              f"  Install: python3.12 -m pip install --no-build-isolation "
              f"aiortc 'av==13.1.0' aiohttp\n"
              f"  See docs/MCP.md §2 for Termux-specific libsrtp build steps.",
              file=sys.stderr)
        return 2
    host_part, _, port_part = args.bind.rpartition(":")
    host = host_part or "127.0.0.1"
    port = int(port_part)
    stun = [s.strip() for s in args.stun.split(",") if s.strip()] \
        if args.stun else None
    return _run_webrtc(host=host, port=port,
                       camera_url=args.camera_url,
                       frame_hz=float(args.frame_hz),
                       stun_servers=stun)


def cmd_ha_publish(args: argparse.Namespace) -> int:
    """Bridge a running x2d_bridge.py daemon's state to a Home Assistant
    MQTT broker via the HA discovery protocol (item #50)."""
    try:
        from runtime.ha.publisher import run as _run_ha
    except ImportError as e:
        print(f"[x2d-bridge] HA publisher import failed: {e}\n"
              "  Required: paho-mqtt (already a bridge dep).",
              file=sys.stderr)
        return 2
    # Resolve serial from creds if --device-serial wasn't passed.
    device_serial = args.device_serial
    if not device_serial:
        try:
            ns = argparse.Namespace(ip=None, code=None, serial=None,
                                     printer=(args.printer or None))
            creds = Creds.resolve(ns)
            device_serial = creds.serial
        except SystemExit:
            device_serial = args.printer or "default"
    return _run_ha(
        broker=args.broker,
        daemon_url=args.daemon_url,
        printer=args.printer or "",
        device_serial=device_serial,
        device_model=args.device_model,
        discovery_prefix=args.discovery_prefix,
        broker_username=args.broker_username or None,
        broker_password=args.broker_password or None,
        daemon_token=args.daemon_token or None,
    )


def cmd_printers(_args: argparse.Namespace) -> int:
    """List every [printer] / [printer:NAME] section in ~/.x2d/credentials.
    Output is JSON: `{"printers": [{"name": "", "ip": "...", "serial": "..."}, …]}`
    so MCP / scripts can consume it without re-parsing INI."""
    ini_path = Path.home() / ".x2d" / "credentials"
    out: list[dict] = []
    if ini_path.exists():
        cp = configparser.ConfigParser()
        cp.read(ini_path)
        for section in cp.sections():
            if section == "printer":
                name = ""
            elif section.startswith("printer:"):
                name = section.split(":", 1)[1]
            else:
                continue
            out.append({
                "name":   name,
                "ip":     cp.get(section, "ip", fallback=""),
                "serial": cp.get(section, "serial", fallback=""),
            })
    print(json.dumps({"printers": out}, indent=2))
    return 0


def cmd_cloud_login(args: argparse.Namespace) -> int:
    import cloud_client
    if args.dry_run:
        # Probe-only mode: confirm the cloud endpoint is reachable
        # without sending credentials. Useful for CI and install-time
        # smoke tests against networks that may block Bambu's API.
        region = args.region or "us"
        result = cloud_client.CloudClient.dry_run_check(region=region)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    if not args.email or not args.password:
        print("--email and --password are required (omit them only with --dry-run)",
              file=sys.stderr)
        return 2
    cli = cloud_client.CloudClient.load_or_anonymous()
    try:
        cli.login(args.email, args.password, region=args.region)
    except cloud_client.CloudError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    print(f"logged in as user_id={cli.session.user_id or '?'} "
          f"(region={cli.session.region}, "
          f"expires_at={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cli.session.expires_at))})")
    return 0


def cmd_cloud_status(args: argparse.Namespace) -> int:
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in (no ~/.x2d/cloud_session.json)")
        return 0
    age_s = max(0, cli.session.expires_at - time.time())
    print(json.dumps({
        "logged_in":  True,
        "user_id":    cli.session.user_id,
        "region":     cli.session.region,
        "expired":    cli.session.expired,
        "expires_at": cli.session.expires_at,
        "expires_in_s": int(age_s),
    }, indent=2))
    return 0


def cmd_cloud_logout(args: argparse.Namespace) -> int:
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    cli.logout()
    print("session cleared")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", help="Printer LAN IP (overrides env / file)")
    p.add_argument("--code", help="Printer 8-char access code (overrides env / file)")
    p.add_argument("--serial", help="Printer serial (overrides env / file)")
    p.add_argument("--printer",
                   help="Pick a [printer:NAME] section from ~/.x2d/credentials. "
                        "Required when more than one named section exists and "
                        "no plain [printer] is present. Overrides $X2D_PRINTER.")

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
    d.add_argument("--max-staleness", type=float, default=30.0,
                   help="Seconds since last printer state push beyond which "
                        "/healthz returns 503 (default 30)")
    d.add_argument("--auth-token",
                   default=os.environ.get("X2D_AUTH_TOKEN", ""),
                   help="Bearer token required for HTTP requests when "
                        "--http binds non-loopback. Default $X2D_AUTH_TOKEN. "
                        "Loopback binds (127.0.0.1) stay open even without "
                        "a token (single-user local case).")
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

    cm = sub.add_parser(
        "camera",
        help="Spawn ffmpeg → MJPEG-over-HTTP proxy for the printer's chamber camera",
    )
    cm.add_argument("--bind", default="127.0.0.1:8766",
                    help="HTTP bind addr (default 127.0.0.1:8766)")
    cm.add_argument("--port", type=int, default=322,
                    help="Printer's RTSPS port (default 322)")
    cm.add_argument("--skip-check", action="store_true",
                    help="Skip the ipcam.rtsp_url pre-flight (useful when "
                         "MQTT can't reach the printer but RTSP is open)")
    cm.add_argument("--proto", choices=["rtsp", "local"], default="rtsp",
                    help="rtsp = RTSPS:322 via ffmpeg (default; needs "
                         "ipcam.rtsp_url != disable). "
                         "local = LVL_Local TLS:6000 (Bambu's proprietary "
                         "stream; same touchscreen LAN-mode liveview gate "
                         "applies — see runtime/network_shim/lvl_local.py)")
    cm.add_argument("--auth-token",
                    default=os.environ.get("X2D_AUTH_TOKEN", ""),
                    help="Bearer token required for HTTP requests when "
                         "--bind is non-loopback. Default $X2D_AUTH_TOKEN.")
    cm.set_defaults(fn=cmd_camera)

    cli_login = sub.add_parser(
        "cloud-login",
        help="Exchange Bambu cloud email + password for a session token. "
             "Stored at ~/.x2d/cloud_session.json (chmod 600). "
             "Subsequent shim calls (is_user_login / get_user_id / "
             "get_user_presets / get_user_tasks) start returning real data.",
    )
    cli_login.add_argument("--email",
                           help="Required unless --dry-run.")
    cli_login.add_argument("--password",
                           help="Required unless --dry-run. "
                                "Use a shell secret store; this lands in `ps`.")
    cli_login.add_argument("--region", choices=["us", "cn"],
                           help="Override region (default: 'cn' if email "
                                "ends with .cn, else 'us')")
    cli_login.add_argument("--dry-run", action="store_true",
                           help="Don't send credentials. Just verify the cloud "
                                "endpoint is reachable (DNS + TLS + HTTP). "
                                "Returns ok/status/region/endpoint JSON.")
    cli_login.set_defaults(fn=cmd_cloud_login)

    cli_status = sub.add_parser(
        "cloud-status",
        help="Show the cached cloud session: logged-in / user-id / token age.",
    )
    cli_status.set_defaults(fn=cmd_cloud_status)

    cli_logout = sub.add_parser(
        "cloud-logout",
        help="Wipe ~/.x2d/cloud_session.json.",
    )
    cli_logout.set_defaults(fn=cmd_cloud_logout)

    pl = sub.add_parser(
        "printers",
        help="List every [printer] / [printer:NAME] section in "
             "~/.x2d/credentials. The default section is reported as "
             "the empty string.",
    )
    pl.set_defaults(fn=cmd_printers)

    ha = sub.add_parser(
        "ha-publish",
        help="Bridge state from a running daemon to a Home Assistant "
             "MQTT broker via HA discovery (item #50). Forwards "
             "command topics back to /control/<verb> on the daemon.",
    )
    ha.add_argument("--broker", default="127.0.0.1:1883",
                    help="MQTT broker host:port (default 127.0.0.1:1883)")
    ha.add_argument("--broker-username", default=os.environ.get("X2D_HA_USER", ""),
                    help="MQTT broker username (or $X2D_HA_USER)")
    ha.add_argument("--broker-password", default=os.environ.get("X2D_HA_PASS", ""),
                    help="MQTT broker password (or $X2D_HA_PASS)")
    ha.add_argument("--daemon-url", default="http://127.0.0.1:8765",
                    help="x2d_bridge daemon HTTP base URL")
    ha.add_argument("--daemon-token", default=os.environ.get("X2D_AUTH_TOKEN", ""),
                    help="Bearer token for the daemon (--auth-token side)")
    ha.add_argument("--printer", default="",
                    help="Printer name (matches --printer on daemon)")
    ha.add_argument("--device-serial", default="",
                    help="HA device identifier (defaults to printer's serial)")
    ha.add_argument("--device-model", default="X2D",
                    help="Model string for HA device card (default X2D)")
    ha.add_argument("--discovery-prefix", default="homeassistant",
                    help="HA discovery topic prefix (default homeassistant)")
    ha.set_defaults(fn=cmd_ha_publish)

    wr = sub.add_parser(
        "webrtc",
        help="WebRTC gateway: pulls /cam.jpg from the camera daemon "
             "and re-publishes as a live VP8 track over WebRTC. "
             "Browser viewer at /cam.webrtc.html.",
    )
    wr.add_argument("--bind", default="127.0.0.1:8765",
                    help="HTTP signaling bind addr (default 127.0.0.1:8765)")
    wr.add_argument("--camera-url", default="http://127.0.0.1:8766",
                    help="Upstream camera daemon URL "
                         "(default http://127.0.0.1:8766)")
    wr.add_argument("--frame-hz", default=os.environ.get(
        "X2D_WEBRTC_FRAME_HZ", "30"),
                    help="JPEG poll rate from the camera daemon")
    wr.add_argument("--stun", default=os.environ.get(
        "X2D_WEBRTC_ICE_STUN", "stun:stun.l.google.com:19302"),
                    help="Comma-separated STUN URLs (empty disables)")
    wr.set_defaults(fn=cmd_webrtc)

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
