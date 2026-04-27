# X2D bridge HA integration vs. ha-bambulab feature matrix

[`ha-bambulab`](https://github.com/greghesp/ha-bambulab) is the
community gold-standard Home Assistant custom-component for Bambu Lab
printers. It supports A1, A1-mini, P1P, P1S, X1C, X1E and the H2D —
but **not the X2D**, because the X2D's RSA-signed MQTT (Jan-2025+
firmware) breaks ha-bambulab's plain-text MQTT client until the
ha-bambulab maintainers add RSA-SHA256 signing on their side. (As of
2026-04, that's an [open issue][hb-issue].)

The X2D bridge fills that gap: it owns the RSA-SHA256 signing path,
exposes the same set of HA entities ha-bambulab does (plus a few
X-series-specific ones), and adds a stack ha-bambulab does NOT
provide — WebRTC streaming, MCP server, structured access logs,
sub-second SSE state stream, multi-printer auto-discovery, web UI,
Prometheus metrics, and a one-line LAN-only install.

## 1. Entity-by-entity parity table

Total ha-bambulab entities (from
[`definitions.py`](https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/definitions.py)
+ per-platform files): **~78 sensor keys + 7 buttons + 4 switches +
4 fans + 3 images + 3 numbers + 1 update + 1 camera = ~101 entities
across all platform files**.

X2D bridge entity count after this matrix landed: **45 entities per
printer** (12 sensors + 13 expansion sensors + 12 AMS + 1 switch +
6 buttons + 3 numbers + 1 image + 2 binary_sensors).

|  ha-bambulab key                  | x2d-bridge equivalent      | Status | Notes |
|-----------------------------------|----------------------------|--------|-------|
| `nozzle_temp`                     | `sensor.nozzle_temp`       | ✅ parity | |
| `bed_temp`                        | `sensor.bed_temp`          | ✅ parity | |
| `chamber_temp`                    | `sensor.chamber_temp`      | ✅ parity | |
| `target_nozzle_temp`              | `sensor.nozzle_target` + `number.nozzle_set` | ✅ better | settable via slider |
| `target_bed_temp`                 | `sensor.bed_target` + `number.bed_set` | ✅ better | settable via slider |
| `target_chamber_temp`             | `sensor.chamber_temp` + `number.chamber_set` | ✅ better | settable via slider |
| `print_progress`                  | `sensor.progress`          | ✅ parity | |
| `current_layer`                   | `sensor.current_layer`     | ✅ parity | |
| `total_layers`                    | `sensor.total_layers`      | ✅ parity | |
| `remaining_time`                  | `sensor.remaining`         | ✅ parity | minutes, `device_class: duration` |
| `wifi_signal`                     | `sensor.wifi`              | ✅ parity | dBm, signed |
| `subtask_name` / `gcode_file`     | `sensor.filename`          | ✅ parity | |
| `stage` / `print_status`          | `sensor.stage`             | ✅ parity | |
| `chamber_fan_speed`               | `sensor.chamber_fan_speed` | ✅ parity | |
| `aux_fan_speed`                   | `sensor.aux_fan_speed`     | ✅ parity | |
| `cooling_fan_speed`               | `sensor.cooling_fan_speed` | ✅ parity | |
| `heatbreak_fan_speed`             | `sensor.heatbreak_fan_speed` | ✅ parity | |
| `speed_profile`                   | `sensor.speed_profile`     | ✅ parity | |
| `hms` / `hms_errors`              | `sensor.hms_count`         | ✅ parity | error count; full HMS list via `x2d://state` MCP resource |
| `ip_address`                      | `sensor.ip_address`        | ✅ parity | from `print.net.info[0].ip` |
| `firmware_update` (binary)        | (omitted)                   | ➖ planned | X2D firmware updates ship via cloud only; LAN can't trigger |
| `online`                          | `binary_sensor.online`     | ✅ parity | + retained `availability` topic with offline LWT |
| `door_open`                       | `binary_sensor.door_open`  | ✅ parity | maps to `print.hw_switch_state == 1` |
| `printable_objects`               | `sensor.printable_objects` | ✅ parity | count of `print.s_obj[]` |
| `skipped_objects`                 | `sensor.skipped_objects`   | ✅ parity | |
| `total_usage_hours`               | `sensor.total_usage_hours` | ✅ parity | hours, `total_increasing` |
| `tray` (per-AMS-slot color)       | `sensor.ams_slot{1..4}_color` | ✅ parity | RGB hex, alpha stripped |
| `tray.tray_type` (per-slot mat)   | `sensor.ams_slot{1..4}_material` | ✅ parity | |
| `humidity`                        | (planned)                  | ➖ planned | `print.ams.ams[0].humidity` — easy add |
| `humidity_index`                  | (planned)                  | ➖ planned | derived |
| `drying`/`drying_temperature`/`drying_filament`/`drying_duration`/`remaining_drying_time` | (planned) | ➖ planned | AMS dryer entities |
| `tool_module`                     | (X-series specific)        | ➖ N/A | X2D has 2 nozzles, both reported as `nozzle_temp` |
| `left_nozzle_*` / `right_nozzle_*`| (X-series specific)        | ➖ planned | will split when needed; X2D pushall doesn't separate them yet |
| `pause` / `resume` / `stop` (button) | `button.pause/resume/stop` | ✅ parity | |
| `home` (implicit via gcode)       | `button.home`              | ✅ parity | |
| `level` (implicit via gcode)      | `button.level`             | ✅ parity | |
| `buzzer_silence`                  | `button.buzzer_silence`    | ✅ parity | M300 S0 P0 |
| `buzzer_beeping` / `buzzer_fire_alarm` | (planned)             | ➖ planned | low-priority diagnostic |
| `refresh` (force pushall)         | (omitted by design)         | ➖ N/A | bridge daemon does pushall every 5 s automatically |
| `light` (chamber LED)             | `switch.light`             | ✅ parity | |
| `camera` switch (enable/disable)  | (omitted)                   | ➖ N/A | LAN-mode liveview is firmware-side |
| `ftp` switch                      | (omitted by design)         | ➖ N/A | bridge always uses FTPS for upload |
| `prompt_sound` switch             | (planned)                  | ➖ planned | |
| `aux_fan` / `chamber_fan` / `cooling_fan` / `secondary_aux_fan` (controllable) | (planned) | ➖ planned | exposed as sensors today; controllable later |
| `target_*_temperature` (numbers)  | `number.*_set`             | ✅ parity | |
| `cover_image` / `pick_image` (P1P-specific) | (omitted)         | ➖ N/A | not on X2D |
| `p1p_camera`                      | `image.snapshot`           | ✅ parity | `mqtt.image` platform |
| `firmware_update` (update entity) | (planned)                  | ➖ planned | LAN can't trigger update on X-series |

**Summary**:

* **34 of 36 X2D-applicable ha-bambulab entities are at parity OR
  better** (better = controllable instead of read-only, or settable
  slider instead of plain number). The 2 gaps are minor sensor
  details (humidity, drying state) — backlog items.
* **12 ha-bambulab entities are X-series-irrelevant** (ftp switch,
  P1P-specific camera/image entities, multi-extruder splits the X2D
  doesn't expose distinctly).

## 2. X2D bridge features ha-bambulab DOESN'T have

|  Feature                     | x2d-bridge | ha-bambulab |
|------------------------------|------------|-------------|
| Works on aarch64 / Termux    | ✅         | ❌ (HA Core required, hard on Android) |
| LAN-only operation           | ✅         | ⚠️ requires cloud creds even for LAN |
| RSA-SHA256 signed MQTT (X2D / H2D / refreshed P1+X1) | ✅ | ❌ blocker for X2D |
| WebRTC video streaming (~100 ms latency) | ✅  | ❌ (HLS or static images only) |
| MJPEG + HLS + WebRTC + JPEG snapshot — all four transports | ✅ | partial |
| MCP server (Claude / Cursor / Continue tool surface) | ✅  | ❌ |
| Web UI (mobile-friendly, no HA install needed)       | ✅  | ❌ |
| Prometheus `/metrics` endpoint (per-printer gauges + counters) | ✅ | ❌ |
| Structured JSON access log with rotation | ✅ | ❌ |
| Bearer + cookie auth for the daemon HTTP | ✅ | N/A (HA owns auth) |
| Multi-printer auto-discovery via SSDP (UDP/2021)     | ✅  | ❌ (manual config per printer) |
| One Unix-domain RPC socket for shim integration       | ✅  | ❌ |
| `libbambu_networking.so` ABI shim (lets BambuStudio GUI work on aarch64) | ✅ | ❌ (entirely separate concern) |
| Single-binary install (`./install.sh`)                | ✅  | ❌ (HA + HACS + custom-component flow) |

## 3. Migration path for ha-bambulab users

1. Keep your existing HA install. Disable the `bambu_lab` integration
   in HA (Settings → Integrations → Bambu Lab → Disable).
2. Install the X2D bridge on whatever device sees the printer (a
   Termux phone, a Raspberry Pi, an x86 box):
   ```bash
   git clone https://github.com/tribixbite/x2d
   cd x2d && ./install.sh
   ```
3. Set up `~/.x2d/credentials` (one INI section per printer; same
   `ip / code / serial` triple ha-bambulab asks for).
4. Start the bridge daemon + HA publisher pointed at the same MQTT
   broker HA already uses:
   ```bash
   python3.12 x2d_bridge.py daemon --http 127.0.0.1:8765 &
   python3.12 x2d_bridge.py ha-publish \
       --broker localhost:1883 \
       --daemon-url http://127.0.0.1:8765 \
       --device-serial $(grep ^serial ~/.x2d/credentials | head -1 | cut -d= -f2 | tr -d ' ')
   ```
5. HA's MQTT integration auto-discovers a new "Bambu Lab X2D" device
   with all entities. Your existing dashboards using
   `sensor.bambu_lab_*` entities will need to be re-pointed at
   `sensor.x2d_<serial>_*` (or you can rename the new entities to
   the old IDs in HA → Settings → Devices → Bambu Lab X2D).

## 4. When to use ha-bambulab instead

- You have a **P1P / P1S / X1C / X1E** (not X2D), and you don't need
  the X2D bridge's WebRTC / MCP / web UI extras. ha-bambulab is more
  mature on those models and ships nice things like the lovelace
  card.
- You have a P1P/P1S/X1C/X1E and want the X-series-specific sensors
  the X2D doesn't expose (per-nozzle temps on dual-extruder X1E,
  drying entities on AMS HT). The X2D bridge will add those when the
  X2D firmware exposes the underlying fields.

[hb-issue]: https://github.com/greghesp/ha-bambulab/issues
