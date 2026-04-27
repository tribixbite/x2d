"""End-to-end test for the HA snapshot pipeline (item #53).

Verifies the full bytes-flow from a synthetic camera daemon → bridge
daemon's /snapshot.jpg proxy → HA publisher's snapshot poll → MQTT
broker → HA-side image entity bytes.

Stack-up:

* synthetic camera HTTP server (1×1 JPEG, port C)
* bridge `_serve_http` daemon (port B), with $X2D_CAMERA_URL → port C
* HA publisher pointed at bridge daemon (port B) and the in-process
  amqtt broker (port M); poll cadence 1 s for fast feedback
* sniffer client subscribed to `x2d/<id>/snapshot`

Expected: within ~3 s, the sniffer receives the same JPEG bytes the
synthetic camera serves (verified via byte-equality + JFIF magic).
"""

from __future__ import annotations

import asyncio
import http.server
import io
import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from amqtt.broker import Broker

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import x2d_bridge
from runtime.ha.publisher import HAPublisher


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_jpeg() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (160, 120), (200, 80, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


_SYNTH_JPEG = _make_jpeg()


def _start_synth_camera(port: int):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): return
        def do_GET(self):
            if self.path.startswith("/cam.jpg"):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(_SYNTH_JPEG)))
                self.end_headers()
                self.wfile.write(_SYNTH_JPEG)
            else:
                self.send_response(404); self.end_headers()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="synth-cam-snap").start()


class _BrokerThread:
    def __init__(self, port: int):
        self.port = port
        self._ready = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True,
                                   name=f"amqtt-{port}")

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        broker = Broker({
            "listeners": {"default": {"type": "tcp",
                                       "bind": f"127.0.0.1:{self.port}"}},
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }, loop=loop)
        loop.run_until_complete(broker.start())
        self._ready.set()
        loop.run_forever()

    def start(self):
        self.t.start()
        self._ready.wait(timeout=10)


def main() -> int:
    failed: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    cam_port    = _free_port()
    bridge_port = _free_port()
    broker_port = _free_port()
    print(f"[snap-test] cam={cam_port} bridge={bridge_port} broker={broker_port}")

    _start_synth_camera(cam_port)

    broker = _BrokerThread(broker_port)
    broker.start()

    # Bridge daemon — env-override its camera URL to point at the
    # synthetic camera.
    os.environ["X2D_CAMERA_URL"] = f"http://127.0.0.1:{cam_port}"

    class M:
        def __init__(self): self.published = []
        def publish(self, p): self.published.append(p)

    threading.Thread(
        target=x2d_bridge._serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{bridge_port}",
            "get_state":     lambda _p: {"print": {"nozzle_temper": 27.0}},
            "get_last_ts":   lambda _p: time.time() - 1,
            "max_staleness": 30.0,
            "auth_token":    None,
            "printer_names": [""],
            "clients":       {"": M()},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True, name="snap-test-bridge",
    ).start()
    time.sleep(0.4)

    # Sniff x2d/<id>/snapshot for the JPEG.
    received: list[bytes] = []
    sniff_done = threading.Event()
    sniff = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="snap-sniffer")

    def on_msg(_c, _u, msg):
        if msg.topic.endswith("/snapshot"):
            received.append(msg.payload)
            sniff_done.set()
    sniff.on_message = on_msg
    sniff.connect("127.0.0.1", broker_port, keepalive=10)
    sniff.subscribe("x2d/+/snapshot", qos=0)
    sniff.loop_start()

    # ----- 1. Bridge /snapshot.jpg proxies the synthetic JPEG -----
    import urllib.request
    with urllib.request.urlopen(
            f"http://127.0.0.1:{bridge_port}/snapshot.jpg", timeout=5) as r:
        body = r.read()
    check("bridge /snapshot.jpg returns 200", r.status == 200, str(r.status))
    check("bridge /snapshot.jpg byte-identical to synth JPEG",
          body == _SYNTH_JPEG,
          detail=f"got {len(body)} B, want {len(_SYNTH_JPEG)} B")

    # ----- 2. Publisher pulls + republishes to MQTT --------------
    pub = HAPublisher(
        broker_host="127.0.0.1",
        broker_port=broker_port,
        daemon_url=f"http://127.0.0.1:{bridge_port}",
        printer_name="",
        device_serial="20P9AJ612700155")
    pub.snapshot_period = 1.0
    pub.start()

    # Wait up to 5 s for the snapshot to land on the MQTT topic.
    sniff_done.wait(timeout=5)
    check("MQTT x2d/<id>/snapshot received within 5s",
          len(received) >= 1)
    if received:
        first = received[0]
        check("MQTT snapshot bytes start with JFIF magic",
              first[:3] == b"\xff\xd8\xff",
              detail=str(first[:8]))
        check("MQTT snapshot bytes match synth JPEG byte-for-byte",
              first == _SYNTH_JPEG,
              detail=f"got {len(first)}B want {len(_SYNTH_JPEG)}B")

    # ----- 3. mqtt.image discovery payload uses image_topic ------
    # Read what HA would see by re-decoding the discovery config the
    # publisher sent earlier.
    cfg_topic = "homeassistant/image/x2d_20P9AJ612700155/snapshot/config"
    cfg_msgs: list[tuple[str, bytes]] = []
    cfg_done = threading.Event()
    cfg_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=f"cfg-sniff-{os.getpid()}-{time.time_ns()}")
    cfg_subscribed = threading.Event()
    def on_cfg_connect(c, _u, _f, _rc, _props):
        c.subscribe(cfg_topic, qos=1)
    def on_cfg_subscribe(_c, _u, _mid, _rcs, _props):
        cfg_subscribed.set()
    def on_cfg(_c, _u, msg):
        cfg_msgs.append((msg.topic, msg.payload))
        if msg.topic == cfg_topic:
            cfg_done.set()
    cfg_client.on_connect   = on_cfg_connect
    cfg_client.on_subscribe = on_cfg_subscribe
    cfg_client.on_message   = on_cfg
    cfg_client.connect("127.0.0.1", broker_port, keepalive=10)
    cfg_client.loop_start()
    cfg_subscribed.wait(timeout=4)
    cfg_done.wait(timeout=4)
    check("image discovery config retained on broker",
          len(cfg_msgs) >= 1)
    if cfg_msgs:
        for t, p in cfg_msgs:
            print(f"  cfg recv topic={t} payload[:32]={p[:32]!r}")
        # cfg_msgs may include retained snapshot-bytes if the broker
        # delivers them in subscribe-order; pick the JSON one.
        json_msgs = [p for t, p in cfg_msgs if p and p[:1] == b"{"]
        cfg_blob = json_msgs[0] if json_msgs else cfg_msgs[0][1]
        try:
            cfg = json.loads(cfg_blob)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            check("image discovery config decodes as JSON", False,
                  detail=f"{e} — got {cfg_blob[:32]!r}")
            cfg = {}
        check("image config has image_topic = x2d/<id>/snapshot",
              cfg.get("image_topic") == "x2d/20P9AJ612700155/snapshot",
              detail=str(cfg.get("image_topic")))
        check("image config has content_type=image/jpeg",
              cfg.get("content_type") == "image/jpeg",
              detail=str(cfg.get("content_type")))

    pub.stop()
    sniff.disconnect(); sniff.loop_stop()
    cfg_client.disconnect(); cfg_client.loop_stop()

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — HA snapshot pipeline (#53)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
