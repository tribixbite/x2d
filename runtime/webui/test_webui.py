"""End-to-end test for the bridge thin-client web UI (#46).

Spins up `_serve_http` in a background thread with a fake state
callback and a mock X2DClient that records publishes, then drives:

* GET /, /index.html, /index.js, /index.css      — static asset serving
* GET /state.events                              — SSE state push
* POST /control/pause                            — MQTT publish via mock
* POST /control/light  {state:"on"}              — system payload
* POST /control/temp   {target:"bed",value:60}   — set_bed_temp
* POST /control/ams_load {slot:3}                — ams_change_filament

No real printer involved — the test verifies the HTTP / SSE plumbing
and the payload shapes the route builds. Live printer round-trips for
these payloads are already covered by cmd_pause / cmd_resume / cmd_set_temp
end-to-end tests in #44.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request

import x2d_bridge


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockClient:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


def main() -> int:
    port = _free_port()
    fake_state = {
        "print": {
            "nozzle_temper": 213.5,
            "bed_temper":     58.7,
            "chamber_temper": 35.0,
            "subtask_name":   "rumi_frame.gcode.3mf",
            "mc_percent":     42,
            "mc_current_layer": 17,
            "total_layer_num":  120,
            "mc_remaining_time": 75,
            "ams": {"ams": [{"id": 0, "tray": [
                {"tray_color": "FF7676FF", "tray_type": "PLA"},
                {"tray_color": "66E08CFF", "tray_type": "PETG"},
                {},
                {},
            ]}], "tray_now": "0"},
        },
    }

    def get_state(_p): return fake_state
    def get_last_ts(_p): return time.time() - 2

    mock = _MockClient()
    server_thread = threading.Thread(
        target=x2d_bridge._serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     get_state,
            "get_last_ts":   get_last_ts,
            "max_staleness": 30.0,
            "auth_token":    None,
            "printer_names": [""],
            "clients":       {"": mock},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True,
        name="webui-test-server",
    )
    server_thread.start()
    time.sleep(0.5)  # let bind() complete

    base = f"http://127.0.0.1:{port}"
    failed: list[str] = []

    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    def http_get(path: str, timeout: float = 5.0):
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")

    def http_post(path: str, body: dict, timeout: float = 5.0):
        req = urllib.request.Request(
            base + path, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    # ---------- static assets ----------
    for path, expect_type in [
        ("/",           "text/html"),
        ("/index.html", "text/html"),
        ("/index.js",   "application/javascript"),
        ("/index.css",  "text/css"),
    ]:
        s, body, ctype = http_get(path)
        check(f"GET {path} status 200", s == 200, str(s))
        check(f"GET {path} content-type {expect_type}",
              expect_type in ctype, ctype)
        check(f"GET {path} body non-empty", len(body) > 100,
              f"len={len(body)}")
        if path == "/index.html":
            check("GET /index.html mentions printer-name span",
                  b"printer-name" in body, body[:200].decode("utf-8", "replace"))

    # ---------- /printers ----------
    s, body, _ = http_get("/printers")
    payload = json.loads(body)
    check("GET /printers returns the configured names",
          payload.get("printers") == [""], str(payload))

    # ---------- SSE /state.events ----------
    sse_data = []
    sse_done = threading.Event()

    def sse_consumer():
        try:
            with urllib.request.urlopen(base + "/state.events", timeout=8) as r:
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    line = r.readline()
                    if not line:
                        break
                    line = line.rstrip(b"\r\n")
                    if line.startswith(b"data: "):
                        try:
                            sse_data.append(json.loads(line[6:]))
                            break
                        except json.JSONDecodeError:
                            pass
        finally:
            sse_done.set()

    t = threading.Thread(target=sse_consumer, daemon=True)
    t.start()
    sse_done.wait(timeout=8)
    check("SSE /state.events delivered at least one frame",
          len(sse_data) >= 1)
    if sse_data:
        first = sse_data[0]
        check("SSE frame has state.print.nozzle_temper",
              first.get("state", {}).get("print", {}).get("nozzle_temper")
              == 213.5,
              detail=str(first)[:200])
        check("SSE frame includes ts",
              isinstance(first.get("ts"), (int, float)),
              detail=str(first.get("ts")))

    # ---------- POST /control/pause ----------
    s, body = http_post("/control/pause", {})
    check("POST /control/pause returns 200", s == 200, str(s))
    check("mock client recorded pause publish",
          any(p.get("print", {}).get("command") == "pause"
              for p in mock.published),
          detail=str(mock.published))

    # ---------- POST /control/resume ----------
    s, body = http_post("/control/resume", {})
    check("POST /control/resume returns 200", s == 200, str(s))
    check("mock client recorded resume publish",
          any(p.get("print", {}).get("command") == "resume"
              for p in mock.published))

    # ---------- POST /control/light ----------
    s, body = http_post("/control/light", {"state": "on"})
    check("POST /control/light state=on returns 200", s == 200, str(s))
    check("light publish has system.command=ledctrl + led_mode=on",
          any(p.get("system", {}).get("command") == "ledctrl"
              and p.get("system", {}).get("led_mode") == "on"
              for p in mock.published),
          detail=str(mock.published))

    s, body = http_post("/control/light", {"state": "purple"})
    check("POST /control/light bad state returns 400", s == 400, str(s))

    # ---------- POST /control/temp ----------
    s, body = http_post("/control/temp", {"target": "bed", "value": 60})
    check("POST /control/temp target=bed returns 200", s == 200, str(s))
    check("temp publish has set_bed_temp + temp=60",
          any(p.get("print", {}).get("command") == "set_bed_temp"
              and p.get("print", {}).get("temp") == 60
              for p in mock.published))

    s, body = http_post("/control/temp", {"target": "nozzle", "value": 215, "idx": 0})
    check("POST /control/temp target=nozzle idx=0 returns 200", s == 200, str(s))
    check("nozzle publish has set_nozzle_temp + extruder_index=0 + target_temp=215",
          any(p.get("print", {}).get("command") == "set_nozzle_temp"
              and p.get("print", {}).get("extruder_index") == 0
              and p.get("print", {}).get("target_temp") == 215
              for p in mock.published))

    s, body = http_post("/control/temp", {"target": "bed"})
    check("POST /control/temp without value returns 400", s == 400, str(s))

    # ---------- POST /control/ams_load ----------
    s, body = http_post("/control/ams_load", {"slot": 3})
    check("POST /control/ams_load returns 200", s == 200, str(s))
    check("ams publish has ams_change_filament + target=2 (1-indexed→0-indexed)",
          any(p.get("print", {}).get("command") == "ams_change_filament"
              and p.get("print", {}).get("target") == 2
              for p in mock.published))

    s, body = http_post("/control/ams_load", {"slot": 99})
    check("POST /control/ams_load slot=99 returns 400", s == 400, str(s))

    # ---------- bad path ----------
    s, body = http_post("/control/launch_missiles", {})
    check("POST /control/<unknown> returns 404", s == 404, str(s))

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print(f"\nALL TESTS PASSED — {len(mock.published)} publishes recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
