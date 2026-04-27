"""MCP stdio server for the X2D bridge.

Wraps every bridge CLI verb as an MCP tool, plus two resources:

* ``x2d://state``           — latest pushall state (JSON)
* ``x2d://camera/snapshot`` — most recent JPEG frame from the camera daemon

The server reads newline-delimited JSON-RPC 2.0 messages from stdin and
writes responses to stdout. Stderr is reserved for human-readable trace
so the host (Claude Desktop, etc.) can show errors without corrupting
the JSON-RPC channel.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Default to the on-disk x2d_bridge.py next to the repo root. Overrideable
# via $X2D_BRIDGE so a packaged install can point at a system path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = Path(os.environ.get("X2D_BRIDGE", _REPO_ROOT / "x2d_bridge.py"))
BRIDGE_PYTHON = os.environ.get("X2D_BRIDGE_PYTHON", sys.executable)
DAEMON_HTTP = os.environ.get("X2D_DAEMON_HTTP", "http://127.0.0.1:8765")
DAEMON_AUTH = os.environ.get("X2D_DAEMON_TOKEN", "")
CALL_TIMEOUT_S = float(os.environ.get("X2D_MCP_CALL_TIMEOUT", "30"))

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "x2d-bridge"
SERVER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

def _printer_arg(args: dict) -> list[str]:
    p = args.get("printer")
    return ["--printer", str(p)] if p else []


def _serial_arg(args: dict) -> list[str]:
    s = args.get("serial")
    return ["--serial", str(s)] if s else []


def _ip_arg(args: dict) -> list[str]:
    return ["--ip", str(args["ip"])] if args.get("ip") else []


def _code_arg(args: dict) -> list[str]:
    return ["--code", str(args["code"])] if args.get("code") else []


def _common_creds(args: dict) -> list[str]:
    return _printer_arg(args) + _ip_arg(args) + _code_arg(args) + _serial_arg(args)


_PRINTER_PROP = {
    "printer": {
        "type": "string",
        "description": "Optional printer name from ~/.x2d/credentials "
                       "([printer:NAME] section). If omitted, the default "
                       "[printer] section is used.",
    },
}


def _build(name: str,
           description: str,
           argv: Callable[[dict], list[str]],
           extra_props: dict | None = None,
           required: list[str] | None = None) -> dict:
    schema_props: dict[str, Any] = dict(_PRINTER_PROP)
    if extra_props:
        schema_props.update(extra_props)
    return {
        "name": name,
        "description": description,
        "argv": argv,
        "inputSchema": {
            "type": "object",
            "properties": schema_props,
            "required": list(required or []),
            "additionalProperties": False,
        },
    }


# Each entry's "argv" returns the args[] passed to the bridge CLI; the
# caller prepends BRIDGE_PYTHON + BRIDGE_PATH and forwards stdout/stderr.
TOOLS: list[dict] = [
    _build("status",
           "Fetch the latest printer state JSON (temps, AMS, print job, lights).",
           lambda a: ["status"] + _common_creds(a)),

    _build("list_printers",
           "List every [printer] / [printer:NAME] section configured in "
           "~/.x2d/credentials.",
           lambda _a: ["printers"]),

    _build("pause",
           "Pause the current print.",
           lambda a: ["pause"] + _common_creds(a)),

    _build("resume",
           "Resume a paused print.",
           lambda a: ["resume"] + _common_creds(a)),

    _build("stop",
           "Abort the current print (cannot be resumed).",
           lambda a: ["stop"] + _common_creds(a)),

    _build("home",
           "Home all axes (G28).",
           lambda a: ["home"] + _common_creds(a)),

    _build("level",
           "Run auto bed-level (G29).",
           lambda a: ["level"] + _common_creds(a)),

    _build("gcode",
           "Send a single arbitrary G-code line to the printer.",
           lambda a: ["gcode", a["line"]] + _common_creds(a),
           extra_props={
               "line": {"type": "string",
                        "description": "G-code line, e.g. 'M104 S210' or 'G1 X10'"},
           },
           required=["line"]),

    _build("set_temp",
           "Set a heater target temperature.",
           lambda a: (["set-temp", a["target"], str(int(a["value"]))]
                      + (["--idx", str(int(a["idx"]))] if "idx" in a else [])
                      + _common_creds(a)),
           extra_props={
               "target": {"type": "string",
                          "enum": ["bed", "nozzle", "chamber"],
                          "description": "Which heater to set."},
               "value": {"type": "integer",
                         "description": "Target temperature in degrees Celsius."},
               "idx": {"type": "integer",
                       "description": "Extruder index (only for target=nozzle)."},
           },
           required=["target", "value"]),

    _build("chamber_light",
           "Control the chamber LED (on / off / flashing).",
           lambda a: (["chamber-light", a["state"]]
                      + (["--on-time", str(int(a["on_time"]))] if "on_time" in a else [])
                      + (["--off-time", str(int(a["off_time"]))] if "off_time" in a else [])
                      + (["--loops", str(int(a["loops"]))] if "loops" in a else [])
                      + _common_creds(a)),
           extra_props={
               "state": {"type": "string",
                         "enum": ["on", "off", "flashing"]},
               "on_time": {"type": "integer", "description": "ms (flashing only)"},
               "off_time": {"type": "integer", "description": "ms (flashing only)"},
               "loops": {"type": "integer", "description": "flashing cycles, 0=forever"},
           },
           required=["state"]),

    _build("ams_unload",
           "Eject filament from the AMS feed-tube back to the spool.",
           lambda a: ["ams-unload"] + _common_creds(a)),

    _build("ams_load",
           "Load filament from a specific AMS slot.",
           lambda a: (["ams-load", "--slot", str(int(a["slot"]))]
                      + _common_creds(a)),
           extra_props={
               "slot": {"type": "integer",
                        "description": "AMS slot, 1-4 indexed."},
           },
           required=["slot"]),

    _build("jog",
           "Jog one axis by a relative distance (mm).",
           lambda a: (["jog", "--axis", a["axis"], "--distance", str(a["distance"])]
                      + _common_creds(a)),
           extra_props={
               "axis": {"type": "string",
                        "enum": ["x", "y", "z", "e"]},
               "distance": {"type": "number",
                            "description": "Millimetres; negative for reverse."},
           },
           required=["axis", "distance"]),

    _build("upload",
           "Upload a sliced .gcode.3mf to the printer's SD card "
           "(does NOT start the print).",
           lambda a: (["upload", a["path"]] + _common_creds(a)),
           extra_props={
               "path": {"type": "string",
                        "description": "Local path to .gcode.3mf"},
           },
           required=["path"]),

    _build("print",
           "Upload + start a print on the given AMS slot.",
           lambda a: (["print", a["path"], "--slot", str(int(a.get("slot", 1)))]
                      + _common_creds(a)),
           extra_props={
               "path": {"type": "string"},
               "slot": {"type": "integer", "description": "AMS slot, default 1"},
           },
           required=["path"]),

    _build("camera_snapshot",
           "Fetch the most recent JPEG frame from the running camera "
           "daemon (x2d_bridge.py camera). The frame is returned as a "
           "base64-encoded image content block. Errors if the camera "
           "daemon isn't running.",
           lambda _a: []),  # handled specially in _call_tool

    _build("healthz",
           "Hit the bridge daemon's /healthz endpoint and return the "
           "JSON. 200 + healthy=true means recent state arrived; 503 "
           "+ healthy=false means MQTT silently disconnected.",
           lambda _a: []),  # handled specially in _call_tool

    _build("metrics",
           "Fetch the bridge daemon's Prometheus /metrics body.",
           lambda _a: []),  # handled specially
]

TOOLS_BY_NAME: dict[str, dict] = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

RESOURCES = [
    {
        "uri": "x2d://state",
        "name": "Latest printer state",
        "description": "JSON pushall state from the X2D (temps, AMS, print "
                       "job, lights). Refreshed on every read by reaching "
                       "out to the daemon if running, else by querying the "
                       "printer directly.",
        "mimeType": "application/json",
    },
    {
        "uri": "x2d://camera/snapshot",
        "name": "Latest camera snapshot",
        "description": "Most recent JPEG frame from the camera daemon. "
                       "Requires `x2d_bridge.py camera` to be running.",
        "mimeType": "image/jpeg",
    },
]


# ---------------------------------------------------------------------------
# Helpers — bridge invocation
# ---------------------------------------------------------------------------

def _run_bridge(argv: list[str]) -> tuple[int, str, str]:
    """Spawn `BRIDGE_PYTHON BRIDGE_PATH …argv` and capture output.
    Returns (returncode, stdout, stderr). Times out at CALL_TIMEOUT_S.
    """
    if not BRIDGE_PATH.exists():
        return (127, "", f"x2d_bridge.py not found at {BRIDGE_PATH}")
    try:
        proc = subprocess.run(
            [BRIDGE_PYTHON, str(BRIDGE_PATH), *argv],
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_S,
            cwd=str(_REPO_ROOT),
        )
    except subprocess.TimeoutExpired as e:
        return (124, e.stdout or "", (e.stderr or "") +
                f"\n[mcp] timed out after {CALL_TIMEOUT_S}s")
    return (proc.returncode, proc.stdout, proc.stderr)


def _http_get(path: str, accept: str = "*/*") -> tuple[int, bytes, str]:
    """GET ${DAEMON_HTTP}{path}. Returns (status, body, content_type).
    A connection refusal returns (0, b"", ""). Bearer token honoured if
    $X2D_DAEMON_TOKEN is set.
    """
    url = f"{DAEMON_HTTP.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Accept": accept})
    if DAEMON_AUTH:
        req.add_header("Authorization", f"Bearer {DAEMON_AUTH}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return (resp.status, resp.read(),
                    resp.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return (e.code, body, e.headers.get("Content-Type", "") if e.headers else "")
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return (0, b"", "")


# ---------------------------------------------------------------------------
# JSON-RPC dispatcher
# ---------------------------------------------------------------------------

def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "logging": {},
        },
        "instructions":
            "X2D printer control bridge. Use the `status` tool to read "
            "current temps/AMS/print job; the `pause`/`resume`/`stop` "
            "verbs to control a running print; `print` to upload+start "
            "a sliced .gcode.3mf. Multi-printer setups: pass `printer` "
            "= the [printer:NAME] section name. The `x2d://state` "
            "resource always returns the freshest JSON state.",
    }


def _tools_list(_params: dict) -> dict:
    return {
        "tools": [
            {k: v for k, v in t.items() if k != "argv"}
            for t in TOOLS
        ],
    }


def _resources_list(_params: dict) -> dict:
    return {"resources": list(RESOURCES)}


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise _ToolError(f"arguments must be an object, got {type(arguments).__name__}")
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise _ToolError(f"unknown tool {name!r}; "
                         f"available: {sorted(TOOLS_BY_NAME)}")
    # Special-cased tools that hit the HTTP daemon directly.
    if name == "camera_snapshot":
        return _camera_snapshot()
    if name == "healthz":
        return _http_text_tool("/healthz")
    if name == "metrics":
        return _http_text_tool("/metrics", accept="text/plain")
    argv = tool["argv"](arguments)
    code, stdout, stderr = _run_bridge(argv)
    text = stdout if stdout.strip() else stderr
    return {
        "content": [{"type": "text", "text": text or "(no output)"}],
        "isError": code != 0,
    }


def _camera_snapshot() -> dict:
    status, body, ctype = _http_get("/cam.jpg")
    if status == 0:
        return {
            "content": [{"type": "text", "text":
                "Camera daemon unreachable at " + DAEMON_HTTP + "/cam.jpg.\n"
                "Run `x2d_bridge.py camera --bind 127.0.0.1:8765` first.\n"
                "Override the URL via $X2D_DAEMON_HTTP."}],
            "isError": True,
        }
    if status != 200 or not body or "image" not in ctype:
        return {
            "content": [{"type": "text", "text":
                f"camera daemon returned status={status} content-type={ctype!r} "
                f"body-len={len(body)}"}],
            "isError": True,
        }
    return {
        "content": [{
            "type": "image",
            "data": base64.b64encode(body).decode("ascii"),
            "mimeType": "image/jpeg",
        }],
        "isError": False,
    }


def _http_text_tool(path: str, accept: str = "application/json") -> dict:
    status, body, ctype = _http_get(path, accept=accept)
    if status == 0:
        return {
            "content": [{"type": "text", "text":
                f"Bridge daemon unreachable at {DAEMON_HTTP}{path}.\n"
                "Run `x2d_bridge.py daemon --http 127.0.0.1:8765` first.\n"
                "Override the URL via $X2D_DAEMON_HTTP."}],
            "isError": True,
        }
    text = body.decode("utf-8", errors="replace") if body else ""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": status >= 400,
    }


def _read_resource(params: dict) -> dict:
    uri = params.get("uri", "")
    if uri == "x2d://state":
        # Prefer the running daemon's HTTP /state — it's a cheap GET that
        # returns the cached push without re-dialing MQTT.
        status, body, _ctype = _http_get("/state")
        if status == 200 and body:
            return {
                "contents": [{
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": body.decode("utf-8", errors="replace"),
                }],
            }
        # Fallback: shell out to `status` which dials MQTT itself.
        code, stdout, stderr = _run_bridge(["status"])
        if code != 0:
            raise _ToolError(f"x2d://state failed: rc={code} stderr={stderr.strip()[:200]}")
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": stdout,
            }],
        }
    if uri == "x2d://camera/snapshot":
        status, body, ctype = _http_get("/cam.jpg")
        if status != 200 or not body:
            raise _ToolError(
                f"x2d://camera/snapshot unreachable (status={status}, "
                f"daemon={DAEMON_HTTP}); run `x2d_bridge.py camera` first.")
        return {
            "contents": [{
                "uri": uri,
                "mimeType": ctype or "image/jpeg",
                "blob": base64.b64encode(body).decode("ascii"),
            }],
        }
    raise _ToolError(f"unknown resource uri {uri!r}")


def _ping(_params: dict) -> dict:
    return {}


METHODS: dict[str, Callable[[dict], dict]] = {
    "initialize":      _initialize,
    "tools/list":      _tools_list,
    "tools/call":      _call_tool,
    "resources/list":  _resources_list,
    "resources/read":  _read_resource,
    "ping":            _ping,
}


class _ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# Stdio loop
# ---------------------------------------------------------------------------

def _handle(message: dict) -> dict | None:
    """Dispatch one JSON-RPC message; return the response (or None if
    notification)."""
    req_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not method:
        return None if is_notification else _err(
            req_id, -32600, "missing method")

    # Notifications: handle silently, never reply.
    if is_notification:
        # initialized / cancelled / progress are the common ones; all are
        # no-ops for this stateless server.
        return None

    handler = METHODS.get(method)
    if handler is None:
        return _err(req_id, -32601, f"method not found: {method}")
    try:
        return _ok(req_id, handler(params))
    except _ToolError as e:
        return _err(req_id, -32602, str(e))
    except Exception as e:  # noqa: BLE001 - top-level guard
        traceback.print_exc(file=sys.stderr)
        return _err(req_id, -32603, f"internal error: {e}")


def serve_stdio(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    print(f"[mcp] x2d-bridge MCP server up (bridge={BRIDGE_PATH}, "
          f"daemon={DAEMON_HTTP})", file=sys.stderr, flush=True)
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            stdout.write(json.dumps(_err(None, -32700, f"parse error: {e}")) + "\n")
            stdout.flush()
            continue
        response = _handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
