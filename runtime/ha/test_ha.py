"""End-to-end test for the HA discovery bridge (item #50).

Spins up an in-process amqtt broker on a free port, brings up an
x2d_bridge.py daemon (with a mock X2DClient that records publishes
sent through /control/<verb>), connects an `HAPublisher` to both, and
verifies:

* All discovery configs land under `homeassistant/<component>/x2d_<id>/<key>/config`
  with the canonical HA shape (unique_id, device.identifiers, etc.)
* `x2d/<id>/availability` flips online → offline cleanly
* SSE updates from the bridge land on `x2d/<id>/state`
* Sending `ON` to `x2d/<id>/light/set` POSTs `/control/light {"state":"on"}`
  to the daemon, which the mock client records as a `ledctrl led_mode=on`
* Sending `PAUSE` to `x2d/<id>/print/set` triggers `/control/pause`
* Sending `60` to `x2d/<id>/temp/bed/set` triggers
  `/control/temp {"target":"bed","value":60}`
* Sending any payload to `x2d/<id>/ams/3/load` triggers
  `/control/ams_load {"slot":3}`

No real MQTT broker, no real X2D — but every wire format hit by HA in
production is exercised against a real amqtt broker + real paho client.
"""

from __future__ import annotations

import asyncio
import json
import socket
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


class _MockX2DClient:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


# ---------------------------------------------------------------------------
# In-process amqtt broker
# ---------------------------------------------------------------------------

class _BrokerThread:
    def __init__(self, port: int) -> None:
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.broker: Broker | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True,
                                   name=f"amqtt-broker-{port}")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        config = {
            "listeners": {
                "default": {
                    "type":     "tcp",
                    "bind":     f"127.0.0.1:{self.port}",
                    "max_connections": 50,
                },
            },
            "auth":     {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
        self.broker = Broker(config, loop=self.loop)
        self.loop.run_until_complete(self.broker.start())
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.broker.shutdown())
            self.loop.close()
            self._stopped.set()

    def start(self) -> None:
        self.t.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("amqtt broker never came up")

    def stop(self) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._stopped.wait(timeout=10)


# ---------------------------------------------------------------------------
# In-process daemon HTTP server
# ---------------------------------------------------------------------------

def _spawn_daemon_http(port: int, mock: _MockX2DClient) -> threading.Thread:
    fake_state = {
        "print": {
            "nozzle_temper":  213.5,
            "nozzle_target_temper": 215,
            "bed_temper":     58.7,
            "bed_target_temper": 60,
            "chamber_temper": 35.0,
            "subtask_name":   "rumi_frame.gcode.3mf",
            "mc_percent":     42,
            "mc_current_layer": 17,
            "total_layer_num":  120,
            "mc_remaining_time": 75,
            "wifi_signal":    "-58dBm",
            "lights_report":  [{"node": "chamber_light", "mode": "off"}],
            "ams": {"ams": [{"id": 0, "tray": [
                {"tray_color": "FF7676FF", "tray_type": "PLA"},
                {"tray_color": "66E08CFF", "tray_type": "PETG"},
                {"tray_color": "FFC857FF", "tray_type": "PLA"},
                {},
            ]}], "tray_now": "0"},
        },
    }
    t = threading.Thread(
        target=x2d_bridge._serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     lambda _p: fake_state,
            "get_last_ts":   lambda _p: time.time() - 1,
            "max_staleness": 30.0,
            "auth_token":    None,
            "printer_names": [""],
            "clients":       {"": mock},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True,
        name=f"ha-test-daemon-{port}",
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# Sniffer client — collects every retained discovery + state message
# ---------------------------------------------------------------------------

class _Sniffer:
    def __init__(self, port: int) -> None:
        self.port = port
        self.lock = threading.Lock()
        self.messages: dict[str, bytes] = {}
        self.history: list[tuple[str, bytes]] = []
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="ha-test-sniffer")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._connected = threading.Event()

    def _on_connect(self, client, _ud, _flags, rc, _props):
        client.subscribe("homeassistant/#", qos=1)
        client.subscribe("x2d/#", qos=1)
        self._connected.set()

    def _on_message(self, _client, _ud, msg):
        with self.lock:
            self.messages[msg.topic] = msg.payload
            self.history.append((msg.topic, msg.payload))

    def start(self) -> None:
        self.client.connect("127.0.0.1", self.port, keepalive=15)
        self.client.loop_start()
        if not self._connected.wait(timeout=8):
            raise RuntimeError("sniffer never connected")

    def stop(self) -> None:
        try:
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass

    def wait_for(self, topic: str, timeout: float = 5.0) -> bytes | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if topic in self.messages:
                    return self.messages[topic]
            time.sleep(0.05)
        return None


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

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

    broker_port = _free_port()
    daemon_port = _free_port()
    broker_url  = f"127.0.0.1:{broker_port}"
    daemon_url  = f"http://127.0.0.1:{daemon_port}"

    print(f"[ha-test] broker=:{broker_port}  daemon=:{daemon_port}")
    broker = _BrokerThread(broker_port)
    broker.start()

    mock = _MockX2DClient()
    _spawn_daemon_http(daemon_port, mock)
    time.sleep(0.5)

    sniffer = _Sniffer(broker_port)
    sniffer.start()

    pub = HAPublisher(
        broker_host="127.0.0.1",
        broker_port=broker_port,
        daemon_url=daemon_url,
        printer_name="",  # default [printer] section on the daemon
        device_serial="20P9AJ612700155",
    )
    pub.start()

    try:
        # Allow discovery + first SSE frame to land.
        time.sleep(2.5)

        device_id = pub.device_id
        base = pub.base_topic

        # ----- 1. discovery configs ----------------------------------
        # Spot-check a few entities of each component type.
        for component, key in [
            ("sensor", "nozzle_temp"),
            ("sensor", "bed_temp"),
            ("sensor", "progress"),
            ("sensor", "ams_slot1_color"),
            ("switch", "light"),
            ("button", "pause"),
            ("button", "resume"),
            ("button", "stop"),
            ("button", "ams_slot3_load"),
            ("image", "snapshot"),
        ]:
            topic = f"homeassistant/{component}/{device_id}/{key}/config"
            payload = sniffer.wait_for(topic, timeout=3)
            check(f"discovery config {component}/{key}",
                  payload is not None and len(payload) > 20,
                  detail=f"missing or empty: {topic}")
            if payload:
                cfg = json.loads(payload)
                check(f"  {component}/{key} has unique_id",
                      cfg.get("unique_id", "").endswith(key),
                      detail=str(cfg.get("unique_id")))
                check(f"  {component}/{key} has device.identifiers",
                      device_id in cfg.get("device", {}).get("identifiers", []),
                      detail=str(cfg.get("device")))

        # ----- 2. availability online --------------------------------
        avail = sniffer.wait_for(f"{base}/availability", timeout=3)
        check("availability=online retained",
              avail == b"online", detail=str(avail))

        # ----- 3. state JSON delivered -------------------------------
        # SSE → publisher → state topic happens once per second.
        state_payload = None
        for _ in range(50):
            time.sleep(0.3)
            msg = sniffer.messages.get(f"{base}/state")
            if msg:
                state_payload = msg
                break
        check("state topic populated",
              state_payload is not None and len(state_payload) > 50,
              detail=str(state_payload)[:200] if state_payload else "missing")
        if state_payload:
            try:
                state = json.loads(state_payload)
                check("state JSON has print.nozzle_temper=213.5",
                      state.get("print", {}).get("nozzle_temper") == 213.5,
                      detail=str(state.get("print", {}).get("nozzle_temper")))
                check("state JSON has ams.tray[].tray_color",
                      state.get("print", {}).get("ams", {}).get("ams", [{}])[0]
                          .get("tray", [{}])[0].get("tray_color")
                          == "FF7676FF",
                      detail="missing AMS color")
            except json.JSONDecodeError as e:
                check("state JSON parses", False, detail=str(e))

        # ----- 4. command flow ---------------------------------------
        # Publish on the broker and confirm the mock X2DClient received
        # the corresponding bridge MQTT payload via /control/<verb>.
        publisher_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="ha-test-cmd-publisher")
        publisher_client.connect("127.0.0.1", broker_port, keepalive=10)
        publisher_client.loop_start()

        before = len(mock.published)

        publisher_client.publish(f"{base}/light/set", "ON", qos=1)
        time.sleep(1.0)
        check("light ON → ledctrl led_mode=on published",
              any(p.get("system", {}).get("command") == "ledctrl"
                  and p.get("system", {}).get("led_mode") == "on"
                  for p in mock.published[before:]),
              detail=str(mock.published[before:]))

        before = len(mock.published)
        publisher_client.publish(f"{base}/print/set", "PAUSE", qos=1)
        time.sleep(0.8)
        check("print PAUSE → /control/pause publish",
              any(p.get("print", {}).get("command") == "pause"
                  for p in mock.published[before:]),
              detail=str(mock.published[before:]))

        before = len(mock.published)
        publisher_client.publish(f"{base}/print/set", "RESUME", qos=1)
        time.sleep(0.8)
        check("print RESUME → /control/resume publish",
              any(p.get("print", {}).get("command") == "resume"
                  for p in mock.published[before:]),
              detail=str(mock.published[before:]))

        before = len(mock.published)
        publisher_client.publish(f"{base}/temp/bed/set", "60", qos=1)
        time.sleep(0.8)
        check("temp bed=60 → set_bed_temp temp=60",
              any(p.get("print", {}).get("command") == "set_bed_temp"
                  and p.get("print", {}).get("temp") == 60
                  for p in mock.published[before:]),
              detail=str(mock.published[before:]))

        before = len(mock.published)
        publisher_client.publish(f"{base}/ams/3/load", "ON", qos=1)
        time.sleep(0.8)
        check("ams slot 3 load → ams_change_filament target=2",
              any(p.get("print", {}).get("command") == "ams_change_filament"
                  and p.get("print", {}).get("target") == 2
                  for p in mock.published[before:]),
              detail=str(mock.published[before:]))

        publisher_client.disconnect()
        publisher_client.loop_stop()

    finally:
        # ----- 5. clean shutdown sets availability=offline ----------
        pub.stop()
        time.sleep(0.5)
        avail = sniffer.messages.get(f"{base}/availability")
        check("availability flipped to offline on stop",
              avail == b"offline", detail=str(avail))

        sniffer.stop()
        broker.stop()

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — HA discovery bridge (#50)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
