# Changelog

All notable changes to this project. Tracks every IMPROVEMENTS.md
ledger item between v0.1.0 and v1.0.0.

## v1.0.0 — Feature-complete LAN-first stack

86 commits, 62 ledger items closed, ~28 K lines added across the
bridge daemon, the runtime/ subsystems, the web UI, the test
harnesses, and the per-feature docs.

### Highlights

* **Six-surface daemon** built on top of the v0.1.0 signed-MQTT
  bridge: REST + Server-Sent Events, Prometheus `/metrics`,
  structured JSON access log, Home Assistant MQTT auto-discovery,
  WebRTC chamber-camera streaming, MCP stdio server, and a
  mobile-friendly web UI.
* **Multi-printer everywhere** — daemon, web UI, queue, HA
  publisher, MCP server. One `[printer:NAME]` section per printer
  in `~/.x2d/credentials` and every surface auto-discovers them.
* **Full BambuStudio Termux GUI port** — 12 source patches against
  upstream BambuStudio v02.06.00.51 plus an `LD_PRELOAD` GTK/locale
  shim, plus a 100-symbol `libbambu_networking.so` ABI shim that
  lets the GUI's Connect/AMS-sync/Print buttons drive printers
  through the bridge.
* **Native Home Assistant integration** with **32 entities** + 1
  Device per printer, including AMS-color → Bambu-profile
  auto-resolve. Live-tested against real Home Assistant Core
  2025.1.4 in a proot Ubuntu chroot — registry snapshots in
  `docs/ha-live-proof/`.
* **Claude Desktop / Cursor / Continue MCP server** wrapping every
  bridge op as a tool, plus a natural-language assistant in the web
  UI that calls the same toolset (with a no-API-key local fallback).

### New surfaces (Phase 1 daemon expansion: items 36-40)

* **#36 multi-printer daemon** — one X2DClient per credentials
  section, all sharing one HTTP server with `?printer=NAME`
  routing. Connection failures are isolated.
* **#37 per-printer `last_message_ts` persistence** at
  `~/.x2d/last_message_ts_<serial>` so `/healthz` reports a
  meaningful age immediately after a daemon restart.
* **#38 Prometheus `/metrics` endpoint** — per-printer gauges
  (nozzle/bed/chamber temps, AMS humidity, mc_percent) +
  per-printer counters (messages_total, mqtt_disconnects_total)
  + global counter (ssdp_notifies_total).
* **#39 structured JSON access log** — one line per HTTP request
  to `~/.x2d/access.log` with 1 MiB rotation; ts, method, path,
  status, duration_ms, printer, authed, client.
* **#40 proactive auto-connect on SSDP** — when an SSDP NOTIFY
  matches a credentials serial, the bridge opens MQTT before any
  shim asks. Cached state replays on every subscribe.

### Phase 2 surfaces (items 42-49)

* **#42 MCP stdio server** at `runtime/mcp/server.py` (callable
  as `python -m mcp_x2d`). 18 tools: status, pause, resume, stop,
  gcode, set_temp, chamber_light, ams_load/unload, jog, upload,
  print, camera_snapshot, list_printers, healthz, metrics, home,
  level. Two resources: `x2d://state`, `x2d://camera/snapshot`.
* **#43 Claude Desktop config docs** at `docs/MCP.md` with
  per-platform install (Termux / Linux / mac / Windows) and the
  SSH-tunnel pattern for running the bridge on Termux while the
  client lives on a laptop.
* **#44 live-tested every MCP tool** against the real X2D —
  `tools/call status` returned actual `nozzle=27 bed=25
  wifi=-58dBm` end-to-end through the JSON-RPC pipeline.
* **#45 WebRTC streaming** via aiortc + aiohttp — sub-second
  latency, browser viewer at `/cam.webrtc.html`. Pinned
  aiortc==1.10.1 + av==13.1.0 (newer versions need PyAV 14
  features that don't build on Termux). libsrtp built from
  source; covered in `docs/WEBRTC.md`.
* **#46 thin web UI** at the daemon's `/` — three static files
  (~17 KB), live state via SSE, control verbs over POST. No
  framework, no build step.
* **#47 mobile-friendly UI** verified at S25 Ultra viewport via
  real headless chromium-browser. CSS hardening: `overflow-x:
  hidden`, `* { min-width: 0 }`, `@media (max-width: 480px)`
  font shrinks. ≥44 px touch targets per Apple HIG / Google MD3.
* **#48 bearer-token login flow** with cookie + localStorage —
  `_check_bearer` accepts either source so EventSource (which
  can't set headers) works via the cookie path. New `/auth/info`
  + `/auth/check` + `/login.html` + `/login.js`.
* **#49 Phase 2 end-to-end soak** — `runtime/test_phase2_smoke.py
  --duration 600` runs all four daemons (bridge, camera, WebRTC,
  MCP) under continuous load for 10 minutes; **0 failures**, 0%
  RSS / thread / FD drift across every daemon.

### Phase 3: Home Assistant integration (items 50-54)

* **#50 MQTT auto-discovery publisher** at `runtime/ha/publisher.py`.
  32 entities per printer (12 sensors + 12 AMS slot entities + 1
  switch + 6 buttons + 3 number sliders + 1 image), all
  discovery-protocol-compliant under
  `<discovery_prefix>/<component>/x2d_<id>/<key>/config`.
* **#51 live-tested against real Home Assistant Core 2025.1.4** in
  a proot Ubuntu chroot. Registry snapshots at
  `docs/ha-live-proof/` show 32 x2d entities + 1 Bambu Lab X2D
  Device with **live values** (`ams_slot2_color="#F95D73"`,
  `ams_slot2_material="PLA"`, `ams_slot3_color="#A03CF7"`).
* **#52 ha-bambulab feature parity matrix** at
  `docs/HA_VS_BAMBULAB.md`. 34 of 36 X2D-applicable ha-bambulab
  entities at parity OR better. Added 13 missing entities to the
  publisher (4 fan speeds, speed_profile, hms_count, ip_address,
  firmware_version, printable/skipped objects, total_usage_hours,
  online + door_open binary sensors, home/level/buzzer_silence
  buttons).
* **#53 HA snapshot entity** — `/snapshot.jpg` proxy on the
  bridge daemon + publisher snapshot loop pushes JPEG bytes to
  `x2d/<id>/snapshot` every 10 s with `retain=True` so HA's
  image card always renders something even after a restart.
* **#54 multi-printer HA support** — `cmd_ha_publish` without
  `--printer` spawns one `HAPublisher` per credentials section
  in the same process. Each gets its own HA Device with
  namespaced topics; failures are isolated per-printer.

### Phase 4: features upstream BambuStudio doesn't have (items 55-58)

* **#55 multi-printer print queue** at `runtime/queue/manager.py`
  — file-backed FIFO at `~/.x2d/queue.json` with strict
  idle-detection auto-dispatch. Crash-safe (running → pending on
  reload). HTML5 native drag-and-drop reorder in the web UI.
* **#56 timelapse browser** at `runtime/timelapse/recorder.py` —
  per-printer state-driven capture (RUNNING → starts JPEG poll
  thread; FINISH → stops + writes meta). One-click `ffmpeg`
  stitch into MP4 with H.264 + faststart. Web UI Timelapse card
  with sampled thumbnail grid + inline `<video>`.
* **#57 AI assistant panel** at `runtime/assistant/router.py` —
  three providers (`local` rule-based, no API key; `anthropic`
  with the canonical MCP toolset; `auto` with graceful fallback).
  Web UI chat panel with color-coded user/assistant/tool turns.
* **#58 real-time AMS color sync** at
  `runtime/colorsync/mapper.py` — loads BambuStudio's official
  `filaments_color_codes.json` (~7000 entries) and resolves any
  RGB hex to the closest Bambu profile by Euclidean distance,
  with material-family filter. Web UI AMS swatches show the
  matched filament name + distance tooltip.

### Phase 5: docs + release (items 59-62)

* **#59 README reorg** — top-level "What is this" + "Who is this
  for" + 16-row feature matrix vs Bambu Studio + Cloud +
  ha-bambulab + 5-command quick-install + per-feature doc table.
  First three sections (~50 lines) tell a brand-new visitor
  everything within 60 s.
* **#60 per-feature docs** in `docs/`: 11 markdown files covering
  every Phase 1-4 feature with overview / install / API / examples /
  test-harness link.
* **#61 demo media** — five 1280×720 H.264 MP4s in `docs/demos/`
  (CLI, GUI, MCP, Web UI, HA dashboard) totalling ~3.2 min.
  Reproducible via `runtime/demos/render.py` (PIL + ffmpeg only).
* **#62 v1.0.0 release** — this changelog, the release notes, the
  refreshed dist tarball + SHA, and the GitHub release.

### Phase 0 fixes shipped after v0.1.0 (items 21-34)

Source-patches against BambuStudio v02.06.00.51 + LD_PRELOAD shim
hardening that landed before the daemon expansion:

* **#21** `GUI_App::config_wizard_startup` source-returns false
* **#22** BBLTopbar narrow-display padding shrink
* **#23** SelectMachinePop modal management — Hide() before
  spawning the Connect dialog so it z-orders correctly
* **#24** Swallow noisy wx sizer CheckExpectedParentIs asserts
* **#25** `EGL_PLATFORM=x11` (not surfaceless) — fixes 3D viewport
  black screen on llvmpipe + wxGLCanvas
* **#26** `cd $HOME` before launching so wxFileDialog defaults
  there instead of `/`
* **#27** Suppress gvfs "Could not read /" popup
* **#28** wxLocale `en_US` ICU bypass via source patch (replaces
  the shim symbol)
* **#29** Cache + replay latest MQTT state on every shim
  subscribe — AMS populates within milliseconds instead of
  waiting for the next 30 s pushall
* **#30** Show occupied LAN-mode printers in the Network combobox's
  "Other Device" list (was being filtered out by `is_avaliable()`)
* **#31** Inflate CheckBox hitbox to 42 px for touchscreens
* **#32** Register the "wx" WebView script-message handler so
  Home-tab WebView click events fire properly
* **#33** Resolved by #25 (same EGL surface root cause)
* **#34** Delete `patch_bambu_skip_wizard.py` + dead shim symbols
  now that #21+#28 supersede them

### Deferred to follow-up

Two ledger items intentionally pushed past v1.0.0 because they
require physical-print-time + an attached ADB device (or a
human at the printer):

* **#35 Final Phase 0 ADB verification** — wipe `~/.config/
  BambuStudioInternal/`, run `install.sh`, launch bambu-studio,
  manually verify zero papercuts.
* **#41 Print the rumi frame end-to-end via the GUI** — physical
  print run from a sliced plate. The bridge-side `start_print`
  C-ABI is already exercised end-to-end by
  `runtime/network_shim/tests/test_shim_e2e.py` against the real
  X2D, so the underlying code path is proven independent of this
  manual GUI run.

## v0.1.0 — initial release (2026-04-25)

* `bin/bambu-studio` — patched BambuStudio v02.06.00.51 (~77 MB
  stripped) with seven Termux/touchscreen patches.
* `runtime/libpreloadgtk.so` — LD_PRELOAD GTK/locale shim.
* `helpers/x2d_bridge.py` + `helpers/bambu_cert.py` — pure-Python
  signed-MQTT LAN client.
* `helpers/{lan_upload,lan_print,resolve_profile,inject_thumbnails,make_frame,test_signed_mqtt}.py` — slicing + LAN-print pipeline.
* Bambu cloud REST endpoints (login + a few read-only verbs).

Full v0.1.0 release notes:
https://github.com/tribixbite/x2d/releases/tag/v0.1.0
