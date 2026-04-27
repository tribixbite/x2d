"""Home Assistant MQTT auto-discovery publisher (item #50).

Bridges between an x2d_bridge.py daemon's HTTP /state.events SSE feed
and a Home Assistant MQTT broker. Publishes:

* discovery configs at ``<discovery_prefix>/<component>/x2d_<serial>/<key>/config``
* per-entity state at ``x2d/<serial>/state`` (single JSON blob with
  every field; entities use ``value_template`` to project)
* availability at ``x2d/<serial>/availability`` (online/offline)

Subscribes to:

* ``x2d/<serial>/light/set``       → POST /control/light
* ``x2d/<serial>/print/set``       → POST /control/{pause,resume,stop}
* ``x2d/<serial>/temp/<target>/set``→ POST /control/temp
* ``x2d/<serial>/ams/<slot>/load`` → POST /control/ams_load

The serial is the ``[printer:NAME]`` section name (or ``default`` for
the unnamed section). One HA Device per printer, with all entities
linked via the ``device.identifiers`` block.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import paho.mqtt.client as mqtt

LOG = logging.getLogger("x2d.ha")


# ---------------------------------------------------------------------------
# Entity catalogue
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Entity:
    """One HA entity. ``component`` is the HA platform (sensor / switch
    / camera / button / number). ``key`` becomes the discovery topic
    leaf and the unique_id suffix. ``value_template`` extracts the
    field from the printer's pushall JSON (Jinja-ish but evaluated by
    HA, not by us)."""
    component: str
    key: str
    name: str
    value_template: str = ""
    unit: str = ""
    device_class: str = ""
    state_class: str = ""
    icon: str = ""
    extra: dict = dataclasses.field(default_factory=dict)


# Sensor entities — one per scalar field in the printer's state.
SENSOR_ENTITIES: list[Entity] = [
    Entity("sensor", "nozzle_temp",   "Nozzle temperature",
           "{{ value_json.print.nozzle_temper | round(1) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_target", "Nozzle target",
           "{{ value_json.print.nozzle_target_temper | round(0) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "bed_temp",      "Bed temperature",
           "{{ value_json.print.bed_temper | round(1) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "bed_target",    "Bed target",
           "{{ value_json.print.bed_target_temper | round(0) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "chamber_temp",  "Chamber temperature",
           "{{ value_json.print.chamber_temper | round(1) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "progress",      "Print progress",
           "{{ value_json.print.mc_percent | int(0) }}",
           "%", "", "measurement", "mdi:progress-clock"),
    Entity("sensor", "current_layer", "Current layer",
           "{{ value_json.print.mc_current_layer | int(0) }}",
           "", "", "measurement", "mdi:layers"),
    Entity("sensor", "total_layers",  "Total layers",
           "{{ value_json.print.total_layer_num | int(0) }}",
           "", "", "measurement", "mdi:layers-outline"),
    Entity("sensor", "remaining",     "Time remaining",
           "{{ value_json.print.mc_remaining_time | int(0) }}",
           "min", "duration", "measurement", "mdi:clock-outline"),
    Entity("sensor", "wifi",          "Wi-Fi signal",
           "{{ value_json.print.wifi_signal | replace('dBm','') | trim }}",
           "dBm", "signal_strength", "measurement"),
    Entity("sensor", "filename",      "Print job",
           "{{ value_json.print.subtask_name | default('') }}",
           "", "", "", "mdi:file"),
    Entity("sensor", "stage",         "Stage",
           "{{ value_json.print.gcode_state | default(value_json.print.mc_print_sub_stage) | default('idle') }}",
           "", "", "", "mdi:state-machine"),
]

# Per-AMS-slot color/material entities. We expose the FIRST AMS unit's
# four trays — multi-AMS support is item #54. Tray-color values come
# back as 8-char hex (RRGGBBAA); the value_template strips the alpha so
# HA's color sensor card understands it.
def ams_entities() -> list[Entity]:
    out: list[Entity] = []
    for slot in range(1, 5):  # 1-indexed to match AMS UI labelling
        # AMS slot is 0-indexed in the wire payload, so subtract 1.
        idx = slot - 1
        out.append(Entity(
            "sensor", f"ams_slot{slot}_color", f"AMS slot {slot} color",
            "{{ '#' + value_json.print.ams.ams[0].tray[" + str(idx) +
            "].tray_color[:6] | default('') }}",
            "", "", "", "mdi:palette"))
        out.append(Entity(
            "sensor", f"ams_slot{slot}_material", f"AMS slot {slot} material",
            "{{ value_json.print.ams.ams[0].tray[" + str(idx) +
            "].tray_type | default('') }}",
            "", "", "", "mdi:printer-3d-nozzle"))
        out.append(Entity(
            "button", f"ams_slot{slot}_load", f"AMS slot {slot} load",
            "", "", "", "", "mdi:tray-arrow-up",
            extra={"command_topic": f"__BASE__/ams/{slot}/load",
                    "payload_press": "ON"}))
    return out


# Switch / button / number entities.
CONTROL_ENTITIES: list[Entity] = [
    Entity("switch", "light", "Chamber light",
           "{{ value_json.print.lights_report[0].mode | default('off') }}",
           "", "", "", "mdi:lightbulb",
           extra={"command_topic":  "__BASE__/light/set",
                  "state_on":       "on",  "state_off": "off",
                  "payload_on":     "ON",  "payload_off": "OFF"}),
    Entity("button", "pause", "Pause print",
           "", "", "", "", "mdi:pause",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "PAUSE"}),
    Entity("button", "resume", "Resume print",
           "", "", "", "", "mdi:play",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "RESUME"}),
    Entity("button", "stop", "Stop print",
           "", "", "", "", "mdi:stop",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "STOP"}),
    # Temp setpoints — HA `number` slider.
    Entity("number", "bed_set",     "Bed setpoint",
           "{{ value_json.print.bed_target_temper | int(0) }}",
           "°C", "temperature", "", "mdi:thermometer",
           extra={"command_topic": "__BASE__/temp/bed/set",
                  "min":  0, "max": 110, "step": 1, "mode": "slider"}),
    Entity("number", "nozzle_set",  "Nozzle setpoint",
           "{{ value_json.print.nozzle_target_temper | int(0) }}",
           "°C", "temperature", "", "mdi:printer-3d-nozzle-heat",
           extra={"command_topic": "__BASE__/temp/nozzle/set",
                  "min":  0, "max": 320, "step": 1, "mode": "slider"}),
    Entity("number", "chamber_set", "Chamber setpoint",
           "{{ value_json.print.chamber_target_temper | int(0) }}",
           "°C", "temperature", "", "mdi:thermometer-lines",
           extra={"command_topic": "__BASE__/temp/chamber/set",
                  "min":  0, "max":  60, "step": 1, "mode": "slider"}),
]


# Camera entity references the bridge daemon's /cam.jpg URL directly.
def camera_entity(snapshot_url: str) -> Entity:
    return Entity(
        "camera", "snapshot", "Chamber camera",
        "", "", "", "", "mdi:camera",
        extra={"topic":          "",  # we use URL-based, not topic
                "still_image_url": snapshot_url,
                "frame_interval":  10})


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

class HAPublisher:
    def __init__(self, *,
                 broker_host: str,
                 broker_port: int = 1883,
                 broker_username: str | None = None,
                 broker_password: str | None = None,
                 daemon_url: str = "http://127.0.0.1:8765",
                 daemon_token: str | None = None,
                 discovery_prefix: str = "homeassistant",
                 printer_name: str = "",
                 device_serial: str = "",
                 device_model: str = "X2D",
                 client_id: str | None = None) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.daemon_url = daemon_url.rstrip("/")
        self.daemon_token = daemon_token
        self.discovery_prefix = discovery_prefix.rstrip("/")
        # Wire-side name (matches the bridge's `--printer NAME` /
        # ?printer=NAME query). Empty string → the default `[printer]`
        # section in ~/.x2d/credentials.
        self.printer_name = printer_name or ""
        # Display label for the HA Device card.
        self._display_name = printer_name or "default"
        # The device "identifier" — used in unique_id and topic prefixes.
        # Prefer the printer's serial when available; fall back to the
        # display name.
        self.device_serial = device_serial or self._display_name
        self.device_model = device_model
        clean = re.sub(r"[^A-Za-z0-9_]", "_", self.device_serial)
        self.device_id = f"x2d_{clean}"
        self.base_topic = f"x2d/{clean}"
        self._stop = threading.Event()
        self._client_id = client_id or f"x2d-ha-{clean}-{os.getpid()}"
        # Build entity list now so command-topic substitution can resolve
        # __BASE__ before any subscribe.
        self._entities = list(SENSOR_ENTITIES) + ams_entities() + \
                         list(CONTROL_ENTITIES) + [
                             camera_entity(self.daemon_url + "/cam.jpg")]
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, self._client_id)
        if broker_username:
            self._client.username_pw_set(broker_username, broker_password or "")
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.will_set(self._availability_topic(), "offline",
                              qos=1, retain=True)
        self._sse_thread: threading.Thread | None = None

    # ----- topics ------------------------------------------------------
    def _state_topic(self) -> str:
        return f"{self.base_topic}/state"

    def _availability_topic(self) -> str:
        return f"{self.base_topic}/availability"

    def _discovery_topic(self, entity: Entity) -> str:
        return (f"{self.discovery_prefix}/{entity.component}/"
                f"{self.device_id}/{entity.key}/config")

    def _resolve_extra(self, extra: dict) -> dict:
        out = {}
        for k, v in extra.items():
            if isinstance(v, str) and "__BASE__" in v:
                out[k] = v.replace("__BASE__", self.base_topic)
            else:
                out[k] = v
        return out

    # ----- HA discovery payload ----------------------------------------
    def _device_block(self) -> dict:
        return {
            "identifiers":  [self.device_id],
            "name":          (f"Bambu Lab {self.device_model} ({self._display_name})"
                              if self._display_name != "default"
                              else f"Bambu Lab {self.device_model}"),
            "manufacturer": "Bambu Lab",
            "model":         self.device_model,
            "sw_version":    "x2d_bridge",
        }

    def _discovery_payload(self, entity: Entity) -> dict:
        payload: dict[str, Any] = {
            "name":          entity.name,
            "unique_id":     f"{self.device_id}_{entity.key}",
            "object_id":     f"{self.device_id}_{entity.key}",
            "device":        self._device_block(),
            "availability_topic": self._availability_topic(),
        }
        # State-bearing entities reuse the single full-state topic.
        if entity.component in ("sensor", "switch", "binary_sensor", "number"):
            payload["state_topic"] = self._state_topic()
            if entity.value_template:
                payload["value_template"] = entity.value_template
        if entity.component == "camera":
            payload.pop("availability_topic", None)
        if entity.unit:           payload["unit_of_measurement"] = entity.unit
        if entity.device_class:   payload["device_class"]        = entity.device_class
        if entity.state_class:    payload["state_class"]         = entity.state_class
        if entity.icon:           payload["icon"]                = entity.icon
        if entity.extra:
            payload.update(self._resolve_extra(entity.extra))
        return payload

    # ----- mqtt callbacks ----------------------------------------------
    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            LOG.error("connect failed rc=%s", reason_code)
            return
        LOG.info("connected to %s:%d as %s",
                 self.broker_host, self.broker_port, self._client_id)
        # Publish all discovery configs (retained).
        for ent in self._entities:
            client.publish(self._discovery_topic(ent),
                           json.dumps(self._discovery_payload(ent)),
                           qos=1, retain=True)
        # Mark online (also retained — survives broker restarts).
        client.publish(self._availability_topic(), "online",
                       qos=1, retain=True)
        # Subscribe to every command topic the entities advertise.
        seen: set[str] = set()
        for ent in self._entities:
            for k, v in self._resolve_extra(ent.extra).items():
                if k.endswith("_topic") and k != "state_topic" \
                        and k != "availability_topic" and isinstance(v, str) \
                        and v.startswith(self.base_topic + "/"):
                    if v not in seen:
                        client.subscribe(v, qos=1)
                        seen.add(v)

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props):
        LOG.warning("disconnected rc=%s", reason_code)

    def _on_message(self, _client, _userdata, msg) -> None:
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
        except Exception:
            payload = ""
        LOG.info("rx %s = %r", topic, payload)
        # Dispatch by topic suffix.
        try:
            if topic == f"{self.base_topic}/light/set":
                state = "on" if payload.upper() in ("ON", "1", "TRUE") else "off"
                self._http_post("/control/light", {"state": state})
            elif topic == f"{self.base_topic}/print/set":
                v = payload.upper()
                if v in ("PAUSE", "RESUME", "STOP"):
                    self._http_post("/control/" + v.lower(), {})
            elif topic.endswith("/load") and "/ams/" in topic:
                # x2d/<id>/ams/<slot>/load → POST /control/ams_load
                m = re.match(re.escape(self.base_topic) + r"/ams/(\d+)/load$", topic)
                if m:
                    self._http_post("/control/ams_load",
                                     {"slot": int(m.group(1))})
            elif "/temp/" in topic and topic.endswith("/set"):
                # x2d/<id>/temp/<target>/set
                m = re.match(re.escape(self.base_topic)
                              + r"/temp/(bed|nozzle|chamber)/set$", topic)
                if m:
                    self._http_post("/control/temp",
                                     {"target": m.group(1),
                                      "value":  int(float(payload))})
        except Exception as e:
            LOG.exception("dispatch failed for %s: %s", topic, e)

    # ----- HTTP forwarder ----------------------------------------------
    def _http_post(self, path: str, body: dict) -> None:
        url = (f"{self.daemon_url}{path}"
               f"?printer={urllib.parse.quote(self.printer_name)}")
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        if self.daemon_token:
            req.add_header("Authorization", f"Bearer {self.daemon_token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                LOG.info("forwarded %s → %d", path, r.status)
        except urllib.error.HTTPError as e:
            LOG.warning("forward %s → %d (%s)", path, e.code,
                        e.read()[:200].decode("utf-8", "replace"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            LOG.warning("forward %s failed: %s", path, e)

    # ----- SSE consumer ------------------------------------------------
    def _sse_loop(self) -> None:
        url = (f"{self.daemon_url}/state.events"
               f"?printer={urllib.parse.quote(self.printer_name)}")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url)
                if self.daemon_token:
                    req.add_header("Authorization",
                                    f"Bearer {self.daemon_token}")
                with urllib.request.urlopen(req, timeout=15) as r:
                    backoff = 1.0
                    while not self._stop.is_set():
                        line = r.readline()
                        if not line:
                            break
                        if line.startswith(b"data: "):
                            try:
                                envelope = json.loads(line[6:].decode())
                            except json.JSONDecodeError:
                                continue
                            state = envelope.get("state") or {}
                            self._client.publish(
                                self._state_topic(),
                                json.dumps(state),
                                qos=0, retain=True)
            except (urllib.error.URLError, ConnectionError,
                    TimeoutError, OSError) as e:
                LOG.warning("SSE reconnect in %.1fs (%s)", backoff, e)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 1.5, 15.0)

    # ----- lifecycle ---------------------------------------------------
    def start(self) -> None:
        self._client.connect_async(self.broker_host, self.broker_port,
                                    keepalive=30)
        self._client.loop_start()
        self._sse_thread = threading.Thread(
            target=self._sse_loop, name=f"x2d-ha-sse-{self.printer_name}",
            daemon=True)
        self._sse_thread.start()

    def run_forever(self) -> int:
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        except KeyboardInterrupt:
            pass
        self.stop()
        return 0

    def stop(self) -> None:
        self._stop.set()
        try:
            self._client.publish(self._availability_topic(),
                                  "offline", qos=1, retain=True)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception:
            pass


def run(*, broker: str,
        daemon_url: str,
        printer: str,
        device_serial: str = "",
        device_model: str = "X2D",
        discovery_prefix: str = "homeassistant",
        broker_username: str | None = None,
        broker_password: str | None = None,
        daemon_token: str | None = None) -> int:
    """Synchronous entry point used by ``x2d_bridge.py ha-publish``."""
    logging.basicConfig(
        level=os.environ.get("X2D_HA_LOG", "INFO"),
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s")
    host, _, port_part = broker.rpartition(":")
    if not host:
        host = broker
        port = 1883
    else:
        port = int(port_part)
    pub = HAPublisher(
        broker_host=host, broker_port=port,
        broker_username=broker_username, broker_password=broker_password,
        daemon_url=daemon_url, daemon_token=daemon_token,
        discovery_prefix=discovery_prefix,
        printer_name=printer or "",
        device_serial=device_serial,
        device_model=device_model)
    print(f"[x2d-ha] device_id={pub.device_id} base_topic={pub.base_topic} "
          f"discovery={discovery_prefix}", file=sys.stderr, flush=True)
    return pub.run_forever()
