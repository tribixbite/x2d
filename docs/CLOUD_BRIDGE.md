# cloud bridge — Bambu cloud-mediated control & print

The X2D bridge has two parallel command surfaces:

- **LAN-direct** (`pause`, `print`, `chamber-light`, …) — talks straight
  to your printer's local MQTT broker. Fast, works offline, but the X2D
  series rejects `print.*` mutating commands without the per-installation
  cert that's only minted by Bambu Handy after login (items #65 / #66 / #68).
- **Cloud-mediated** (`cloud-pause`, `cloud-print`, `cloud-chamber-light`,
  …) — routes the same payloads through Bambu's cloud MQTT broker
  (`{us,cn}.mqtt.bambulab.com:8883`) using your Bambu account JWT.
  Sidesteps the cert issue entirely, works from anywhere on the
  internet, but needs a one-time login.

Use cloud-* if you can't get LAN-direct `print.*` working on your X2D —
that's the realistic ship path for items #65 / #66 / #68.

---

## 0. One-time login

```bash
python3 x2d_bridge.py cloud-login --email YOU@gmail.com --password 'XXX'
```

If your account has email-code verification or 2FA TOTP enabled, the CLI
will prompt for the code interactively. Token is cached at
`~/.x2d/cloud_session.json` (chmod 600). Auto-refreshes before expiry.

```bash
python3 x2d_bridge.py cloud-status      # check login state
python3 x2d_bridge.py cloud-printers    # list bound printers
python3 x2d_bridge.py cloud-logout      # wipe the session
```

---

## 1. Remote monitoring

```bash
# Snapshot — fires pushall, dumps first state, exits.
python3 x2d_bridge.py cloud-state

# Stream every report from the printer (Ctrl-C to stop).
python3 x2d_bridge.py cloud-state --follow

# Pin a specific printer when multiple are bound to the account.
python3 x2d_bridge.py cloud-state --serial 20P9AJ612700155
```

Same JSON shape as the LAN `status` command. Useful for off-LAN dashboards
or just checking if the printer's actually online before walking over to
it.

---

## 2. Remote control

```bash
python3 x2d_bridge.py cloud-pause
python3 x2d_bridge.py cloud-resume
python3 x2d_bridge.py cloud-stop
python3 x2d_bridge.py cloud-gcode "G28"                # home all
python3 x2d_bridge.py cloud-gcode "M141 S30"           # chamber to 30°C
python3 x2d_bridge.py cloud-chamber-light on
python3 x2d_bridge.py cloud-chamber-light flashing --loops 3
```

Or any payload directly:

```bash
python3 x2d_bridge.py cloud-publish --payload \
  '{"print":{"command":"set_bed_temp","sequence_id":"1","temp":65}}'
```

Schema is **identical** to the LAN command-publish shape — anything that
works against `device/<SN>/request` over LAN works the same way
through the cloud broker.

---

## 3. Remote printing (full round-trip)

```bash
python3 x2d_bridge.py cloud-print rumi_frame.gcode.3mf \
  --slot 3 --bed-temp 65 --no-level
```

Three steps under the hood:
1. Get a one-shot OSS upload credential from
   `/v1/iot-service/api/user/file/oss/upload-token`.
2. PUT the .gcode.3mf to Aliyun OSS (handles both presigned-URL and
   STS-credentials response shapes; HMAC-SHA1 signing for the latter).
3. Publish `print.project_file` with `print_type=cloud` and
   `url=cloud://<bucket>/<path>` to `device/<SN>/request` via the
   cloud broker. Bambu's cloud relays to the printer; printer pulls
   from OSS via its cloud channel.

Args mirror the LAN `print` command — `--slot`, `--no-ams`, `--plate`,
`--bed-type`, `--bed-temp`, `--no-level`, `--flow-cali`,
`--vibration-cali`, `--timelapse`. Add `--dry-run` to print the MQTT
payload without uploading or publishing.

---

## 4. HTTP API

When the bridge daemon is running with `--http :8765`, the cloud
commands are also reachable over HTTP for the web UI / Home Assistant /
external tools:

| Method | Path                              | Notes |
|--------|-----------------------------------|-------|
| GET    | `/cloud/status`                   | login state + token expiry |
| GET    | `/cloud/printers`                 | list bound printers |
| GET    | `/cloud/state?serial=&timeout=`   | first state snapshot |
| POST   | `/cloud/publish`                  | body `{serial,payload,timeout?}` |

All gated on the daemon's existing bearer-token auth when the bind is
non-loopback. 401 returned cleanly when no cloud session exists.

---

## 5. Why this works where LAN-direct doesn't

The X2D firmware's `print.*` commands require a *per-installation* X.509
cert to verify the MQTT signature. That cert is minted dynamically by
Bambu's cloud during account-bind, returned via the
`/v1/iot-service/api/user/applications/{appToken}/cert` endpoint, and
stored encrypted at rest inside Bambu Handy (Tink AES-GCM with a
hardware-backed master key in AndroidKeyStore — extracting it from
outside Bambu's process is multi-day RE work; details in
`runtime/handy_extract/` and `IMPROVEMENTS.md` #68).

The cloud broker doesn't have this constraint. Auth is just
`(u_<user_id>, <jwt>)`. The printer pulls the file from OSS via
Bambu's cloud channel, which is signed by Bambu's cloud
infrastructure — the per-installation cert is invoked implicitly.

Result: cloud-print is **today's realistic path** for X2D printing
through this bridge. LAN-direct `print.*` remains a known limitation
pending an independent way to get the cert.

---

## 6. Region routing

Endpoints automatically resolve from your account's TLD:

| Region | API host             | MQTT broker                  |
|--------|----------------------|------------------------------|
| `us`   | api.bambulab.com     | us.mqtt.bambulab.com:8883    |
| `cn`   | api.bambulab.cn      | cn.mqtt.bambulab.com:8883    |

Override with `--region {us,cn}` on `cloud-login` or `X2D_REGION` env.

---

## 7. Files

- `cloud_client.py` — REST + OAuth + OSS upload + MQTT-creds derivation.
  ~700 lines, dependency-free except urllib + paho-mqtt.
- `x2d_bridge.py` — CLIs at `cmd_cloud_*` (~lines 3500-3700) and HTTP
  helpers at `_http_cloud_*` (~lines 2440-2580).
- Session: `~/.x2d/cloud_session.json` (chmod 600).
