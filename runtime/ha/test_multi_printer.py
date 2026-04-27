"""Multi-printer HA discovery test (item #54).

Spins up two daemons (one per simulated printer), an in-process amqtt
broker, and two `HAPublisher` instances — one per printer name — to
verify:

1. Each named printer gets its own HA Device with distinct
   `device.identifiers` and unique entity `unique_id` prefixes.
2. Entities are namespaced by printer (no key collisions even though
   both expose `nozzle_temp`).
3. Two retained `availability` topics live on the broker side-by-
   side, one per printer.
4. Killing one publisher doesn't take the other down.
"""

from __future__ import annotations

import asyncio
import json
import os
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


class _MockX2DClient:
    def __init__(self, label: str):
        self.label = label
        self.published: list[dict] = []

    def publish(self, p):
        self.published.append(p)


def _spawn_daemon(port: int, mock: _MockX2DClient,
                   nozzle_temp: float) -> threading.Thread:
    fake_state = {
        "print": {
            "nozzle_temper": nozzle_temp,
            "bed_temper":     60.0,
            "chamber_temper": 35.0,
            "subtask_name":   f"job-{mock.label}.gcode.3mf",
            "mc_percent":     42,
            "ams": {"ams": [{"id": 0, "tray": [
                {"tray_color": "FF0000FF", "tray_type": "PLA"}, {}, {}, {},
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
            # The publisher addresses the daemon as
            # ?printer=<mock.label>, so the daemon's printer-name
            # registry must include that exact name. This mirrors
            # what `x2d_bridge.py daemon` does when ~/.x2d/credentials
            # has [printer:studio] / [printer:garage] sections.
            "printer_names": [mock.label],
            "clients":       {mock.label: mock},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True, name=f"daemon-{mock.label}",
    )
    t.start()
    return t


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
    studio_port = _free_port()
    garage_port = _free_port()
    print(f"[multi] broker={broker_port} studio={studio_port} garage={garage_port}")

    broker = _BrokerThread(broker_port)
    broker.start()

    studio_mock = _MockX2DClient("studio")
    garage_mock = _MockX2DClient("garage")
    _spawn_daemon(studio_port, studio_mock, nozzle_temp=210.0)
    _spawn_daemon(garage_port, garage_mock, nozzle_temp=220.0)
    time.sleep(0.4)

    # Sniffer first so it catches retained discovery on subscribe.
    received: dict[str, bytes] = {}
    history: list[tuple[str, bytes]] = []
    sniff = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="multi-sniff")
    sniff_done = threading.Event()
    def on_msg(_c, _u, msg):
        received[msg.topic] = msg.payload
        history.append((msg.topic, msg.payload))
    sniff.on_message = on_msg
    sniff.on_connect = lambda c, *_: c.subscribe([
        ("homeassistant/#", 1), ("x2d/#", 1)])
    sniff.connect("127.0.0.1", broker_port, keepalive=10)
    sniff.loop_start()
    time.sleep(0.5)

    # Spin up two publishers.
    pub_studio = HAPublisher(
        broker_host="127.0.0.1", broker_port=broker_port,
        daemon_url=f"http://127.0.0.1:{studio_port}",
        printer_name="studio", device_serial="STUDIO_SERIAL_001",
        device_model="X2D")
    pub_studio.snapshot_period = 999  # don't burn cycles in this test
    pub_studio.start()

    pub_garage = HAPublisher(
        broker_host="127.0.0.1", broker_port=broker_port,
        daemon_url=f"http://127.0.0.1:{garage_port}",
        printer_name="garage", device_serial="GARAGE_SERIAL_002",
        device_model="X2D")
    pub_garage.snapshot_period = 999
    pub_garage.start()

    # Allow discovery + first state push to land.
    time.sleep(3)

    studio_id = pub_studio.device_id
    garage_id = pub_garage.device_id
    studio_base = pub_studio.base_topic
    garage_base = pub_garage.base_topic

    # ----- 1. distinct device_ids -----
    check("studio device_id != garage device_id",
          studio_id != garage_id,
          detail=f"{studio_id} == {garage_id}")
    check("studio device_id has the studio serial",
          "STUDIO_SERIAL_001" in studio_id, studio_id)
    check("garage device_id has the garage serial",
          "GARAGE_SERIAL_002" in garage_id, garage_id)

    # ----- 2. each printer gets its own discovery topics -----
    studio_topics = [t for t in received if studio_id in t]
    garage_topics = [t for t in received if garage_id in t]
    check("studio has ≥30 discovery+state topics",
          len(studio_topics) >= 30,
          detail=f"got {len(studio_topics)}")
    check("garage has ≥30 discovery+state topics",
          len(garage_topics) >= 30,
          detail=f"got {len(garage_topics)}")
    check("topic sets are disjoint",
          not (set(studio_topics) & set(garage_topics)),
          detail=str((set(studio_topics) & set(garage_topics)))[:200])

    # ----- 3. unique_ids and device.identifiers are distinct -----
    studio_cfg_topic = f"homeassistant/sensor/{studio_id}/nozzle_temp/config"
    garage_cfg_topic = f"homeassistant/sensor/{garage_id}/nozzle_temp/config"
    s_cfg = received.get(studio_cfg_topic)
    g_cfg = received.get(garage_cfg_topic)
    check("studio sensor config retained", s_cfg is not None,
          detail=f"want {studio_cfg_topic}")
    check("garage sensor config retained", g_cfg is not None,
          detail=f"want {garage_cfg_topic}")
    if s_cfg and g_cfg:
        s = json.loads(s_cfg)
        g = json.loads(g_cfg)
        check("unique_ids differ",
              s["unique_id"] != g["unique_id"],
              detail=f"{s['unique_id']} == {g['unique_id']}")
        check("device.identifiers differ",
              s["device"]["identifiers"] != g["device"]["identifiers"],
              detail=str(s["device"]["identifiers"]))
        check("studio device.name has 'studio'",
              "studio" in s["device"]["name"].lower(),
              detail=s["device"]["name"])
        check("garage device.name has 'garage'",
              "garage" in g["device"]["name"].lower(),
              detail=g["device"]["name"])

    # ----- 4. availability topics live side-by-side -----
    studio_avail = received.get(f"{studio_base}/availability")
    garage_avail = received.get(f"{garage_base}/availability")
    check("studio availability=online retained",
          studio_avail == b"online", str(studio_avail))
    check("garage availability=online retained",
          garage_avail == b"online", str(garage_avail))

    # ----- 5. state JSON has different nozzle temps -----
    studio_state = received.get(f"{studio_base}/state")
    garage_state = received.get(f"{garage_base}/state")
    check("studio state JSON has nozzle=210",
          studio_state and json.loads(studio_state)["print"]["nozzle_temper"] == 210.0,
          detail=str(studio_state)[:120] if studio_state else "missing")
    check("garage state JSON has nozzle=220",
          garage_state and json.loads(garage_state)["print"]["nozzle_temper"] == 220.0,
          detail=str(garage_state)[:120] if garage_state else "missing")

    # ----- 6. command isolation: ON to studio's light doesn't fire on garage -----
    cmd_pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                           client_id="multi-cmd")
    cmd_pub.connect("127.0.0.1", broker_port, keepalive=10)
    cmd_pub.loop_start()
    time.sleep(0.3)

    s_before = len(studio_mock.published)
    g_before = len(garage_mock.published)
    cmd_pub.publish(f"{studio_base}/light/set", "ON", qos=1)
    time.sleep(1.2)
    s_pubs = studio_mock.published[s_before:]
    g_pubs = garage_mock.published[g_before:]
    check("studio light publish landed on studio's mock client",
          any(p.get("system", {}).get("led_mode") == "on" for p in s_pubs),
          detail=str(s_pubs))
    check("studio light publish did NOT leak to garage",
          not any(p.get("system", {}).get("led_mode") == "on" for p in g_pubs),
          detail=str(g_pubs))

    cmd_pub.disconnect(); cmd_pub.loop_stop()

    # ----- 7. killing one publisher doesn't take the other down -----
    pub_studio.stop()
    time.sleep(0.5)
    studio_avail_after = received.get(f"{studio_base}/availability")
    garage_avail_after = received.get(f"{garage_base}/availability")
    check("studio availability flipped to offline after stop",
          studio_avail_after == b"offline",
          detail=str(studio_avail_after))
    check("garage availability still online (not collateral damage)",
          garage_avail_after == b"online",
          detail=str(garage_avail_after))

    pub_garage.stop()
    sniff.disconnect(); sniff.loop_stop()

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print(f"\nALL TESTS PASSED — multi-printer HA support (#54)\n"
          f"   {len(studio_topics)} studio topics + {len(garage_topics)} garage topics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
