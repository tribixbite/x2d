# BambuStudio on Termux aarch64 — X2D / H2D / signed-LAN-MQTT toolkit

[![ci](https://github.com/tribixbite/x2d/actions/workflows/ci.yml/badge.svg)](https://github.com/tribixbite/x2d/actions/workflows/ci.yml)

This repo collects the patches, runtime shims, and helpers needed to run
[BambuStudio v02.06.00.51](https://github.com/bambulab/BambuStudio/releases/tag/v02.06.00.51)
natively on aarch64 Termux + termux-x11, plus a pure-Python LAN client that
talks to recent Bambu printers (X2D / H2D / refreshed P1+X1) using their new
RSA-SHA256-signed MQTT protocol — no Bambu Network Plug-in, no cloud login.

> The Bambu Network Plug-in `.so` is shipped only for x86\_64 Linux and
> arm64 macOS. On aarch64 Termux it has no equivalent build, so out of the
> box BambuStudio's GUI cannot connect to a LAN printer or sync AMS spool
> data. The bridge in this repo (`x2d_bridge.py`) replaces what the plug-in
> would have done for the LAN-only path.

## Layout

```
.
├── patches/                  # 6 unified diffs against upstream BambuStudio
│   ├── Button.cpp.termux.patch
│   ├── AxisCtrlButton.cpp.termux.patch
│   ├── SideButton.cpp.termux.patch
│   ├── TabButton.cpp.termux.patch
│   ├── BBLTopbar.cpp.termux.patch
│   └── BBLTopbar.hpp.termux.patch
├── runtime/
│   └── preload_gtkinit.c     # LD_PRELOAD shim: GTK pre-init, locale fix,
│                             # wxLocale ICU bypass, wx 3.3 assert silencer,
│                             # hidden config_wizard_startup override
├── run_gui_clean.sh          # canonical GUI launcher
├── x2d_bridge.py             # signed-MQTT LAN client (status/upload/print/daemon)
├── bambu_cert.py             # publicly-leaked Bambu Connect signing key
├── lan_upload.py             # FTPS:990 implicit-TLS uploader (helper subset)
├── lan_print.py              # upload + start_print combo
├── make_frame.py             # generates a debossed picture-frame STL
├── inject_thumbnails.py      # injects the 5 PNG thumbnails firmware needs
├── resolve_profile.py        # flatten BambuStudio profile inheritance
└── test_signed_mqtt.py       # diagnostic: pushall with RSA-SHA256 signature
```

`bs-bionic/` (the BambuStudio source tree) and `dist/` (built tarball) are
gitignored — the patches in `patches/` reproduce the source changes against
a clean clone.

## Quick start

**TL;DR runtime guide:** see [docs/QUICKSTART.md](docs/QUICKSTART.md)
for the install → credentials → launch → connect → print walkthrough,
plus copy-pasteable shortcuts for every print-control verb, the
camera proxy URLs, and a troubleshooting table.

### Use the prebuilt distribution

### One-line install (recommended)

```
bash <(curl -fsSL https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)
```

Idempotent — runs the platform check, `pkg install`s the runtime deps,
fetches the latest release tarball, verifies SHA-256, drops the
`libbambu_networking.so` + `libBambuSource.so` shims into
`~/.config/BambuStudioInternal/plugins/`, pre-seeds
`~/.config/BambuStudioInternal/BambuStudio.conf` with
the X2D model entry, writes a chmod-600 `~/.x2d/credentials` skeleton
for you to fill in, and (if Termux:Boot is installed) drops a boot-time
launcher for the bridge daemon. Re-run any time to upgrade — your
`BambuStudio.conf` and credentials file are preserved.

### Manual install (alternative)

A prebuilt tarball is attached to the GitHub release:

```
curl -L -o bs-x2d.tar.xz \
    https://github.com/tribixbite/x2d/releases/latest/download/bambustudio-x2d-termux-aarch64.tar.xz
tar -xJf bs-x2d.tar.xz
cd bambustudio-x2d-termux-aarch64
./run_gui.sh                  # needs termux-x11 running on DISPLAY=:1
```

### Termux dependencies

```
pkg install x11-repo
pkg install \
    wxwidgets gtk3 webkit2gtk-4.1 \
    glew glfw mesa libllvm llvm \
    glib pango cairo gdk-pixbuf atk \
    fontconfig freetype libpng libjpeg libtiff \
    openssl curl libcurl \
    opencv libdbus libwebp \
    libavcodec libswscale libavutil ffmpeg \
    python python-cryptography xdotool \
    openbox
pip install paho-mqtt
```

**Why openbox is in the list**: termux-x11 ships without a window manager.
Without one, `wxFileDialog`s open undecorated at (0,0) and stack *under*
the main frame so Cancel-button taps land on the main frame instead of
the dialog ("Cancel buttons don't work"); transient dialogs can't be
dragged; `wxFrame::Maximize` is a no-op. Openbox (≈600 KB) supplies a
minimal EWMH-aware WM that fixes all of those. `run_gui.sh` will spawn
it automatically on launch if it's installed and not already running.

The most version-sensitive of these is `libllvm` — Mesa requires it at
the same major version. If `pkg upgrade` ever leaves them mismatched
you'll get `EGL_BAD_PARAMETER` / "Unable to get EGL Display"; recover
with `pacman -S libllvm llvm`.

You also need a working X server. The reference setup is **termux-x11**
(install the Android app and run `termux-x11` in a Termux session,
reachable on `:1`).

### Build from source

```
git clone --recurse-submodules https://github.com/bambulab/BambuStudio
cd BambuStudio
git checkout v02.06.00.51
for p in /path/to/x2d/patches/*.termux.patch; do git apply "$p"; done
# Use the bionic-toolchain steps from
# ~/.claude/skills/bambustudio-on-termux-aarch64.md
mkdir build && cd build && cmake -GNinja .. && ninja bambu-studio
gcc -fPIC -shared ../runtime/preload_gtkinit.c \
    $(pkg-config --cflags --libs gtk+-3.0) -ldl -o ../runtime/libpreloadgtk.so
```

## libbambu\_networking.so shim — making the GUI talk to LAN printers

The Bambu Network Plug-in `.so` is shipped only for x86\_64 Linux + arm64
macOS, so on aarch64 Termux the GUI's "Connect / AMS / Print" buttons
have nothing to dlopen. `runtime/network_shim/` builds a drop-in
replacement that exports the entire NetworkAgent ABI (103 `bambu_network_*`
symbols + 21 `ft_*` symbols) and forwards the LAN-relevant calls over a
Unix-domain socket to a long-running `x2d_bridge.py serve` process.
Cloud-only entry points return success-with-empty so the GUI's cloud
panels stay quiet but the LAN flow works end-to-end.

```
cd runtime/network_shim
make            # builds libbambu_networking.so + libBambuSource.so
make install    # → ~/.config/BambuStudioInternal/plugins/
```

A self-test harness lives at `runtime/network_shim/tests/test_shim_e2e.py`.
It dlopens the .so, asserts every host-expected symbol is exported, then
spawns a fresh `x2d_bridge.py serve` and round-trips through the protocol
against a real X2D (handshake → connect → state event → disconnect):

```
python3.12 runtime/network_shim/tests/test_shim_e2e.py
```

Wire format and op set are spec'd in `runtime/network_shim/PROTOCOL.md`.
The shim spawns `x2d_bridge.py serve` itself if no socket is found at
`$X2D_BRIDGE_SOCK` (default `~/.x2d/bridge.sock`), so the GUI launch
flow is just: install the .so once, then `./run_gui.sh`.

## Using the LAN bridge (`x2d_bridge.py`)

Save credentials once. Either a single `[printer]` section (the
default, used when no `--printer NAME` is passed) or as many named
`[printer:NAME]` sections as you want:

```
mkdir -p ~/.x2d && chmod 700 ~/.x2d
cat > ~/.x2d/credentials <<EOF
# Single-printer setup:
[printer]
ip = 192.168.x.y
code = <8-char access code from printer screen>
serial = <printer serial from device sticker>

# OR, multiple named printers:
[printer:studio]
ip = 192.168.x.y
code = …
serial = …

[printer:basement]
ip = 10.0.0.50
code = …
serial = …
EOF
chmod 600 ~/.x2d/credentials
```

Pick a named printer with `--printer NAME`, the `X2D_PRINTER` env
var, or — when only one named section exists and there's no plain
`[printer]` — automatically.

Then:

```
# One-shot state dump
x2d_bridge.py status

# Upload + start print on AMS slot 4
x2d_bridge.py print myfile.gcode.3mf --slot 3

# Long-running monitor — polls every 5s, exposes JSON at http://127.0.0.1:8765/state
x2d_bridge.py daemon --http 127.0.0.1:8765 --quiet

# Same daemon also exposes /healthz for uptime monitoring:
#   200 + {"healthy": true,  ...} when fresh state arrived recently
#   503 + {"healthy": false, ...} when MQTT silently disconnected
# Threshold is configurable via --max-staleness (default 30s).
curl http://127.0.0.1:8765/healthz

# Prometheus scrape: gauges for nozzle/bed/chamber temps, AMS humidity,
# print progress; counters for total_messages, mqtt_disconnects, ssdp_notifies.
curl http://127.0.0.1:8765/metrics

# Every HTTP hit is appended as one JSON line to ~/.x2d/access.log
# (ts, method, path, status, duration_ms, printer, authed, client).
# Rotates to access.log.1 at 1 MiB.
tail -f ~/.x2d/access.log
```

Credentials can also come from `--ip / --code / --serial` flags or
`X2D_IP / X2D_CODE / X2D_SERIAL` environment variables.

### Print-control commands

Each verb is a single signed-MQTT publish — same protocol the official
GUI uses, just routed through our bridge so it works on aarch64. Every
command exits as soon as the printer ACKs the publish.

```
x2d_bridge.py pause                      # pause the current print
x2d_bridge.py resume                     # resume after pause
x2d_bridge.py stop                       # abort the current print
x2d_bridge.py home                       # G28 — home all axes
x2d_bridge.py level                      # G29 — auto bed-level
x2d_bridge.py set-temp bed     60        # set_bed_temp 60°C
x2d_bridge.py set-temp nozzle 220 --idx 0  # set_nozzle_temp on extruder 0
x2d_bridge.py set-temp chamber 35        # M141 S35 (chamber heater)
x2d_bridge.py chamber-light on           # ledctrl on / off / flashing
x2d_bridge.py chamber-light flashing --on-time 200 --off-time 200 --loops 5
x2d_bridge.py ams-load 0 3 --tar-temp 220   # AMS 0 / slot 3, preheat to 220
x2d_bridge.py ams-unload 0 --tar-temp 220   # unload from AMS 0
x2d_bridge.py jog X 10                   # relative move +10 mm on X
x2d_bridge.py jog Z -5 --feed 600        # relative -5 mm on Z @ 600 mm/min
x2d_bridge.py gcode "M115"               # send arbitrary gcode_line
```

Payload schemas reverse-engineered from
`bs-bionic/src/slic3r/GUI/DeviceManager.cpp::MachineObject::command_*`
so the printer behaviour matches what the official GUI sends.

### Camera proxy

The printer's chamber camera streams over RTSPS, but only after you
**enable LAN-mode liveview on the printer's touchscreen**
(Settings → Network → Liveview). Otherwise the stream lives on a closed
proprietary protocol on TCP/6000 that requires the x86\_64-only
`libBambuSource.so` to decode.

Once enabled, run:

```
x2d_bridge.py camera                     # bind 127.0.0.1:8766 by default
x2d_bridge.py camera --bind 0.0.0.0:8766 # expose on LAN (be careful!)
```

Then point any browser at `http://127.0.0.1:8766/cam.mjpeg` for the
multipart MJPEG stream, or `/cam.jpg` for a one-shot snapshot. Multiple
viewers share one ffmpeg subprocess, so bandwidth to the printer is
constant regardless of how many people are watching.

Pre-flight checks `ipcam.rtsp_url` via signed MQTT and bails with a
clear hint if liveview is still disabled. Pass `--skip-check` to bypass
(useful when MQTT is flaky but RTSP is open).

Two transport options:

* `--proto rtsp` (default): RTSPS:322 via ffmpeg. Fast, well-tested,
  gated on `ipcam.rtsp_url != "disable"`.
* `--proto local`: TLS:6000 LVL_Local — Bambu's proprietary stream
  protocol. The TLS handshake + 80-byte auth blob + 16-byte frame
  framing are reverse-engineered in `runtime/network_shim/lvl_local.py`.
  The same printer-touchscreen "LAN-mode liveview" toggle gates this
  path too — without it the printer rejects with status `0x0003013f`
  and a clear error message.

Three HTTP outputs:

* `/cam.mjpeg` — multipart/x-mixed-replace, browser-renderable, low latency
* `/cam.jpg`   — single latest JPEG snapshot (one-shot)
* `/cam.m3u8`  — HLS playlist with 2-second segments (12s sliding window).
  Plays in any mobile browser via `<video src="…/cam.m3u8" controls>`.
  Higher latency than MJPEG (~6-8s) but survives flaky connections
  better and supports seeking.

### Exposing the daemon / camera on the LAN

`/state` `/healthz` `/cam.mjpeg` `/cam.jpg` are open on loopback by
default (single-user local use). Binding non-loopback (`--http
0.0.0.0:8765` or `--bind 0.0.0.0:8766`) requires a bearer token,
otherwise every request returns `401 Unauthorized` with a
`WWW-Authenticate: Bearer` hint:

```
# Generate a long random token, then:
export X2D_AUTH_TOKEN=$(openssl rand -hex 32)
x2d_bridge.py daemon --http 0.0.0.0:8765
x2d_bridge.py camera --bind 0.0.0.0:8766

# From another box on the LAN:
curl -H "Authorization: Bearer $X2D_AUTH_TOKEN" http://<phone>:8765/healthz
```

`--auth-token` on the command line takes precedence over the env var.
Loopback binds keep the open path even with a token configured? — no:
once you set a token, loopback also enforces it, so the same `curl`
command works against both `127.0.0.1` and a LAN-exposed bind.

### Filament presets without a cloud account

When no cloud session exists, `bambu_network_get_user_presets`
falls back to a local catalog so the GUI's AMS spool dropdown
isn't empty:

* All `instantiation: true` BBL filament profiles shipped under
  `resources/profiles/BBL/filament/` (1464 entries on the current
  binary — every Bambu vendor variant for every supported model).
* A small community-curated set under
  `runtime/network_shim/data/community_filaments.json` covering
  Generic PLA / PETG / ABS / TPU / ASA plus Polymaker / Prusament /
  eSun / Hatchbox flavours. Edit that file to add your own.

If you sign in via `cloud-login`, the cloud-synced presets take
precedence — the local fallback only activates anonymously.

### Bambu cloud login (optional)

The bridge can talk to the Bambu Lab cloud REST API to populate the
GUI surfaces that the shim was previously stubbing — `is_user_login`,
`get_user_id`, `get_user_presets`, `get_user_tasks`. Without a cloud
session, those calls keep returning empty (LAN-only flow is unaffected).

```
x2d_bridge.py cloud-login --email me@example.com --password '…'
x2d_bridge.py cloud-status     # confirms session, expiry, region
x2d_bridge.py cloud-logout     # wipes ~/.x2d/cloud_session.json
```

Tokens land at `~/.x2d/cloud_session.json` (chmod 600). The bridge
auto-refreshes the access token when it's within 5 min of expiry.
Region is auto-detected from the email TLD (`.cn` → CN, else US),
override with `--region`.

This module hits the same endpoints the open-source community already
documented (`pybambu`, `bambu-farm-manager`, `OrcaSlicer`). Bambu has
no published SDK — if they rotate URLs, this module needs to rotate
in lockstep with the upstream consumers.

## Thin web UI (`http://<phone>:8765/`)

The bridge daemon serves a mobile-friendly remote-control surface at
`/`. No build step, no framework — `web/index.html` + `web/index.js`
+ `web/index.css` (~17 KB total). Live state arrives over Server-Sent
Events (`/state.events`); pause / resume / stop / lights / heat
presets / AMS slot loads POST to `/control/<verb>` and the daemon
publishes via the long-lived MQTT client. Camera tabs let you flip
between `/cam.jpg` snapshot polling, native HLS at `/cam.m3u8`, and
WebRTC at `/cam.webrtc/offer`.

```bash
python3.12 x2d_bridge.py daemon --http 0.0.0.0:8765 --auth-token "$X2D_AUTH_TOKEN"
# Then open http://<phone-ip>:8765/ in any modern browser.
```

Test harness: `PYTHONPATH=. python3.12 runtime/webui/test_webui.py` →
33/33 PASS, exercises every static / SSE / control route end-to-end.

## MCP server (Claude Desktop, Cursor, Continue, …)

The bridge ships an MCP server at `runtime/mcp/server.py` so any
MCP-aware client can drive prints conversationally. Eighteen tools
(status, pause, resume, stop, gcode, set_temp, chamber_light,
ams_load/unload, jog, upload, print, camera_snapshot, list_printers,
healthz, metrics, home, level) plus two resources (`x2d://state`,
`x2d://camera/snapshot`).

```jsonc
// Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json (mac)
//                 %APPDATA%\Claude\claude_desktop_config.json           (Windows)
//                 ~/.config/Claude/claude_desktop_config.json           (Linux)
{
  "mcpServers": {
    "x2d": {
      "command": "python3.12",
      "args": ["-m", "mcp_x2d"],
      "cwd": "/absolute/path/to/x2d"
    }
  }
}
```

Smoke-test the server before wiring it in:

```bash
python3.12 runtime/mcp/test_mcp.py     # 47/47 PASS, ALL TESTS PASSED
```

Full per-platform install + Termux-via-SSH setup in [`docs/MCP.md`](docs/MCP.md).

## What's broken on Termux without these patches

| Symptom | Root cause | Patch |
|---|---|---|
| GUI aborts at start with `Gtk-ERROR: Can't create a GtkStyleContext without a display connection` | wxFont static init touches GTK CSS before `gtk_init` | `runtime/preload_gtkinit.c` (constructor 101) |
| GUI shows "Switching language en\_US failed" then exits | wx 3.3 `wxUILocale::IsAvailable` is ICU-backed, Termux libicu has no `en_US` | shim overrides the symbol |
| `setlocale("en_US", …)` returns NULL → modal exit | bionic accepts `en_US.UTF-8` but not bare `en_US` | shim retries with `.UTF-8` suffix |
| GUI runs ~20s then dies on first GL draw with `zink_kopper.c:720` assert | Mesa picks zink (Vulkan→GL); kopper needs DRI3/Present which termux-x11 lacks | `run_gui_clean.sh` forces `GALLIUM_DRIVER=llvmpipe` |
| Cancel buttons / AMS spool taps / sidebar buttons silently dropped | custom `Button::mouseReleased` strict bounds check vs. touch-drift | `patches/{Button,AxisCtrlButton,SideButton,TabButton}.cpp.termux.patch` |
| Maximize button does nothing / window goes off-screen in portrait | termux-x11 has no WM; BBLTopbar relies on `wxFrame::Maximize()`; min-size 1000×600 exceeds portrait width | `patches/BBLTopbar.{cpp,hpp}.termux.patch` |
| LAN connect / AMS sync / print impossible | Network Plug-in is x86\_64 only | `runtime/network_shim/` (libbambu\_networking.so stub) + `x2d_bridge.py serve` |
| `mqtt message verify failed` (err\_code 84033543) on every command | Jan-2025+ firmware requires RSA-SHA256 signature in `header` block | `bambu_cert.py` (publicly-leaked Bambu Connect cert) |

## Provenance

Built and tested on:

* Termux aarch64, x11-repo packages (`wxwidgets 3.3`, `gtk3`, `webkit2gtk-4.1`,
  `mesa 26.0.5`, `libllvm 21`, …)
* termux-x11 Android app, software-rendering display `:1`
* Bambu Lab X2D, dual-extruder, AMS HT 4-slot, firmware ≥ Jan 2025

GPL-3.0+ (matches upstream BambuStudio). Bambu and BambuStudio are
trademarks of Shenzhen Bambu Lab Technology Co., Ltd. — this repo is not
affiliated with or endorsed by them.
