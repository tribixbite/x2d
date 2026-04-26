# x2d quickstart

The hot-path commands for "I just want to print to my X2D from this phone."
Skip the source-build / patch-derivation prose — that's in the main README.

## 0. One-time install

If you haven't already:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)
```

This drops `bambu-studio` + `runtime/` + `helpers/` under `~/x2d/`,
seeds a Bambu vendor profile so the Device tab works, and writes a
`~/.x2d/credentials` skeleton.

## 1. Set the printer credentials

Edit `~/.x2d/credentials` and fill in three fields the printer's screen
shows under **Settings → Network**:

```ini
[printer]
ip     = 192.168.0.x         # WLAN status panel
code   = 12345678            # 8-char access code
serial = 03ABC123XXXXXXX     # serial sticker / About page
```

Verify it talks to the printer (no GUI needed):

```bash
~/x2d/helpers/x2d_bridge.py status | head     # one-shot push, prints JSON
```

If you see a JSON dump with `mc_print` keys, you're connected.

### Multiple printers

Add named sections; pick with `--printer NAME` or `X2D_PRINTER=NAME`:

```ini
[printer:living]
ip = 10.0.0.10
code = …
serial = …

[printer:lab]
ip = 10.0.0.11
…
```

## 2. Start the X server (once per Termux session)

In the **termux-x11** Android app: tap "Open in full-screen", then in
this Termux session run:

```bash
termux-x11 :1 &
```

(The launcher checks `$DISPLAY=:1` is reachable.)

## 3. Launch the GUI

```bash
~/x2d/run_gui.sh
```

That single command:

* spawns the bridge daemon under a watchdog (auto-respawns on crash)
* sets up locale + GTK env so wxLocale doesn't pop the "Switching
  language failed" modal
* loads the LD_PRELOAD shim that fixes the GTK-before-init crash, the
  ICU locale check, and dialog sizing
* execs `bambu-studio`

When the window opens:
- **Prepare** tab → Printer panel at top should show the green WiFi
  icon within ~30s (proves SSDP discovery works).
- **Device** tab → the agent-driven monitor view (chamber light,
  temps, AMS spool colours).

## 4. Connect to the printer (inside the GUI)

The shim auto-handles SSDP discovery so the X2D shows up automatically.
If the green WiFi icon never appears within 60s, the bridge log helps:

```bash
tail -50 ~/.x2d/bridge.log
```

Look for lines like `[serve] ssdp listening on udp/2021` and
`SSDP NOTIFY parsed dev_id=… dev_ip=…`.

## 5. Print a job from the CLI (no GUI needed)

```bash
# 1. Slice a model with bambu-studio CLI to produce a .gcode.3mf
~/x2d/bin/bambu-studio --slice 0 -o out.gcode.3mf model.stl

# 2. Upload + start print in one shot
~/x2d/helpers/x2d_bridge.py print out.gcode.3mf
```

## Print-control shortcuts (live MQTT publishes)

```bash
x=~/x2d/helpers/x2d_bridge.py

# State
$x status                   # one-shot pushall + dump
$x daemon --http :8765      # long-running monitor + HTTP JSON

# Operator actions (mid-print)
$x pause
$x resume
$x stop                     # cancel the current job

# G-code interactively
$x gcode "M115"             # firmware info
$x gcode "G28"              # equivalent to `$x home`
$x home
$x level                    # auto bed level

# Heat
$x set-temp bed 60
$x set-temp nozzle 220
$x set-temp chamber 35

# Lights
$x chamber-light on
$x chamber-light off
$x chamber-light flashing

# AMS
$x ams-load 0 1 --tar-temp 220   # AMS 0 / slot 1, preheat 220
$x ams-unload 0 --tar-temp 220

# Manual jog
$x jog x 10                 # 10mm +X
$x jog z -1                 # 1mm -Z

# Camera proxy
$x camera --bind 127.0.0.1:8766          # default RTSPS
$x camera --proto local --skip-check     # alternate LVL_Local TLS:6000
```

## Monitoring shortcuts

```bash
# Long-running JSON daemon + HTTP endpoint for Home Assistant / Grafana
x2d_bridge.py daemon --http :8765

# Health-check (200 if recent push, 503 after --max-staleness)
curl -s http://127.0.0.1:8765/healthz | jq .

# Latest state JSON
curl -s http://127.0.0.1:8765/state | jq '.print.bed_temper, .print.nozzle_temper'

# Camera stream in a browser
http://127.0.0.1:8766/cam.mjpeg     # MJPEG (low latency, browser-renderable)
http://127.0.0.1:8766/cam.m3u8      # HLS  (higher latency, mobile-Safari-friendly)
http://127.0.0.1:8766/cam.jpg       # one-shot snapshot
```

### Exposing on the LAN

By default the daemon and camera bind only to `127.0.0.1`. To expose on
the LAN, you **must** set a bearer token:

```bash
export X2D_AUTH_TOKEN=$(openssl rand -hex 32)
x2d_bridge.py daemon --http 0.0.0.0:8765
x2d_bridge.py camera --bind 0.0.0.0:8766

# From another box on the LAN:
curl -H "Authorization: Bearer $X2D_AUTH_TOKEN" http://<phone-ip>:8765/healthz
```

Without the token the daemon returns `401` with a `WWW-Authenticate`
hint. Loopback binds without a token stay open for local convenience.

## Bambu cloud login (optional)

Only needed if you want the GUI's user-account dropdown / cloud-synced
presets to populate. LAN-only flow doesn't touch this.

```bash
x2d_bridge.py cloud-login --email me@example.com --password '…'
x2d_bridge.py cloud-status
x2d_bridge.py cloud-logout
```

Tokens persist at `~/.x2d/cloud_session.json` (chmod 600), refreshed
automatically when within 5 min of expiry.

## When something doesn't work

| Symptom | Check | Fix |
|---|---|---|
| GUI never opens | `pgrep bambu-studio` | rerun `run_gui.sh` from a Termux shell, watch stderr |
| GUI opens but Device tab is blank | preset selected? | confirm `presets.printer` starts with "Bambu Lab" in `~/.config/BambuStudioInternal/BambuStudio.conf`, or `rm ~/.x2d/.ssdp_seeded` and relaunch — the auto-pop will re-fire |
| Green WiFi icon never appears | bridge alive? | `tail -f ~/.x2d/bridge.log` ; if blank, kill any stale `x2d_bridge.py serve` and re-run launcher |
| `status` hangs | wrong IP / wrong access code | re-check Settings → Network on the printer |
| `gcode/pause/resume` returns "verify failed" | leaked cert outdated | update from upstream repo (`cd ~/x2d && git pull`) |
| Camera 503 forever | LAN-mode liveview is OFF on the printer | flip it on at the printer touchscreen: Settings → Network → Liveview |
| Cloud login 400 | wrong email/password OR account is `.cn` region | retry with `--region cn` |
| /healthz returns 503 immediately after restart | Should NOT happen — the bridge persists `last_message_ts` | check `~/.x2d/last_message_ts` exists and is readable |

## Files / paths cheat sheet

| Where | What |
|---|---|
| `~/x2d/run_gui.sh` | GUI launcher (the one command you actually run) |
| `~/x2d/helpers/x2d_bridge.py` | LAN client + daemon + RPC server CLI |
| `~/.x2d/credentials` | per-printer ip/code/serial (chmod 600) |
| `~/.x2d/cloud_session.json` | optional Bambu cloud tokens (chmod 600) |
| `~/.x2d/bridge.sock` | Unix socket the shim talks to |
| `~/.x2d/bridge.log` | watchdog + bridge stderr (1 MiB rotation) |
| `~/.x2d/last_message_ts` | persisted `/healthz` last-push timestamp |
| `~/.x2d/.ssdp_seeded` | marker file (delete to re-trigger auto-pop) |
| `~/.config/BambuStudioInternal/BambuStudio.conf` | BambuStudio's AppConfig — preset selection lives here |
| `~/.config/BambuStudioInternal/system/BBL/` | Bambu vendor profile dir (1464 filaments + every printer model) |
