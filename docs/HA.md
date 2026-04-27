# X2D + Home Assistant integration

The bridge ships an MQTT auto-discovery publisher (`x2d_bridge.py
ha-publish`) that exposes the printer to Home Assistant as a single
Device with 32 entities. Tested against real Home Assistant Core
2025.1.4 — see §4 for the live-test artefacts.

## 1. Quick start (with mosquitto + HA already installed)

```bash
# 1. Start mosquitto (or any other MQTT broker)
mosquitto -p 1883 &

# 2. Start the x2d bridge daemon (already running for the web UI)
python3.12 x2d_bridge.py daemon --http 127.0.0.1:8765 &

# 3. Run the HA discovery publisher
python3.12 x2d_bridge.py ha-publish \
    --broker      127.0.0.1:1883 \
    --daemon-url  http://127.0.0.1:8765 \
    --device-serial $(grep '^serial' ~/.x2d/credentials | head -1 | cut -d= -f2 | tr -d ' ') \
    --device-model X2D
```

In Home Assistant, go to **Settings → Integrations → MQTT** and point
it at the same broker. Within seconds, a "Bambu Lab X2D" Device
appears under MQTT with 32 entities.

## 2. Entities published

| Component | Count | Examples |
|---|---|---|
| `sensor`  | 23 | nozzle_temp, bed_temp, chamber_temp, progress, current_layer, total_layers, remaining, wifi, filename, stage, ams_slot{1..4}_color, ams_slot{1..4}_material |
| `number`  | 3  | bed_set, nozzle_set, chamber_set (slider sets temp target) |
| `switch`  | 1  | light (chamber LED on/off) |
| `button`  | 7  | pause, resume, stop, ams_slot{1..4}_load |
| `image`   | 1  | snapshot (chamber camera, refreshed via daemon /cam.jpg) |

All 32 are linked to one HA Device:

```json
{
  "identifiers":  [["mqtt", "x2d_<SERIAL>"]],
  "name":         "Bambu Lab X2D",
  "manufacturer": "Bambu Lab",
  "model":        "X2D",
  "sw_version":   "x2d_bridge"
}
```

## 3. Wire-format topics

```
homeassistant/<component>/x2d_<serial>/<key>/config   discovery (retained)
x2d/<serial>/state                                    full pushall JSON (retained)
x2d/<serial>/availability                             online/offline (retained)
x2d/<serial>/light/set         payload: ON | OFF
x2d/<serial>/print/set         payload: PAUSE | RESUME | STOP
x2d/<serial>/temp/<bed|nozzle|chamber>/set  payload: integer °C
x2d/<serial>/ams/<slot>/load   payload: any (slot is 1-indexed)
x2d/<serial>/snapshot          payload: JPEG bytes (item #53)
```

## 4. Live test against real HA

`runtime/ha/test_ha_live.sh` (or run the steps below by hand) drives
the full stack against actual Home Assistant Core 2025.1.4 inside an
Ubuntu chroot:

```bash
# In Termux: bring up an in-process MQTT broker
python3.12 -c 'import asyncio; from amqtt.broker import Broker; \
  asyncio.run((lambda: (Broker({"listeners": {"default": {"type": "tcp", "bind": "0.0.0.0:21883"}}, "auth": {"allow-anonymous": True}, "topic-check": {"enabled": False}}).start()))()) ' &

# Bridge daemon + publisher
python3.12 x2d_bridge.py daemon --http 127.0.0.1:18555 --quiet &
python3.12 x2d_bridge.py ha-publish --broker 127.0.0.1:21883 \
    --daemon-url http://127.0.0.1:18555 \
    --device-serial $(grep ^serial ~/.x2d/credentials | head -1 | cut -d= -f2 | tr -d ' ') &

# In proot Ubuntu: install + start HA
proot-distro login ubuntu --shared-tmp -- bash -c '
  cd /root/ha && source venv/bin/activate
  hass -c /root/ha-config --log-no-color
'
```

After ~60 s of HA boot, inspect HA's persisted registries:

```bash
cat $PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root/ha-config/.storage/core.entity_registry \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
    print(len([e for e in d["data"]["entities"] if "x2d_" in e["unique_id"]]))'
# Expect: 32
```

Sample of `core.restore_state` after live X2D state was processed by
HA's Jinja templates (saved to `docs/ha-live-proof/`):

```
sensor.x2d_<id>_ams_slot2_color    state="#F95D73"   (live AMS color, RGB-only — alpha stripped)
sensor.x2d_<id>_ams_slot2_material state="PLA"
sensor.x2d_<id>_ams_slot3_color    state="#A03CF7"
number.x2d_<id>_bed_set             state="0"        (bed target temperature)
number.x2d_<id>_nozzle_set          state="0"        (nozzle target temperature)
```

These are real values pulled from the running printer through the
full SSE → publisher → MQTT → HA pipeline.

## 5. Authentication for non-loopback brokers

Pass `--broker-username` / `--broker-password` (or set
`X2D_HA_USER` / `X2D_HA_PASS`) to the publisher. The publisher
authenticates as a regular MQTT client; HA's MQTT integration is
configured separately on the HA side.

For the bridge daemon's HTTP control routes, pass `--daemon-token`
matching `--auth-token` on the daemon side. The publisher will attach
`Authorization: Bearer …` to every `/control/<verb>` POST.

## 6. Multi-printer

Run `ha-publish` without `--printer` and it spawns one HAPublisher
thread per `[printer:NAME]` section in `~/.x2d/credentials`,
sharing one process. Each printer gets its own HA Device with
distinct `device.identifiers`, namespaced `unique_id`s, and
isolated availability/state/command topics:

```bash
# ~/.x2d/credentials
[printer:studio]
ip = 192.168.1.42
code = 12345678
serial = 03ABC0001234567

[printer:garage]
ip = 192.168.1.43
code = 87654321
serial = 03DEF0007654321

# one process drives both
python3.12 x2d_bridge.py ha-publish \
    --broker 127.0.0.1:1883 \
    --daemon-url http://127.0.0.1:8765
```

Failures are isolated per-printer — if one publisher errors during
startup the others stay up; if one publisher crashes mid-run, the
others keep flowing.

Use `--printer NAME` to drive only one printer (single-process mode).
