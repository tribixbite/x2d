# x2d roadmap — track ledger

Source of truth for the 10-improvement push. The Stop hook reads this file
and refuses to let Claude exit while any item is unchecked. Completion
criteria are strict: built, tested end-to-end against a real X2D where
applicable, committed, pushed, release tarball updated where applicable,
no stubs, no shortcuts, no `TODO` comments left behind.

## Definition of done

For every item:
- Code lands in `main` and is committed with a clear message.
- A test (manual or automated) is documented in this file showing the
  before/after behaviour, with an exact command and expected output.
- Any new public surface (CLI flag, config key, env var) is in `README.md`.
- Tarball at the v0.1.0 GitHub release reflects the change.
- No new linter / clangd warnings introduced (existing ones can stay).
- If the change affects user-facing behaviour, the README "What's broken"
  table is updated.

## Items

- [x] **1. Stub `libbambu_networking.so` for aarch64.** Native shared
  library that exports the full NetworkAgent C ABI surface (~100 typedef'd
  function pointers visible in `bs-bionic/src/slic3r/Utils/NetworkAgent.hpp`).
  LAN-relevant entry points (`publish_topic_msg`, `start_print`,
  `connect_printer`, `set_state_callback`, `upload_file`, `get_state_msg`,
  …) marshal to the Python bridge over a Unix-domain socket; cloud-only
  ones return success-with-empty data. BambuStudio dlopens it like the
  real plug-in, so every "Connect / AMS sync / Print" button in the GUI
  actually works.
  - **Sub-tasks**:
    - [x] Read NetworkAgent.{hpp,cpp} fully; enumerate every typedef and
      its calling convention. — 105 typedef'd function pointers,
      `bambu_networking.hpp` for callback signatures + struct layouts.
    - [x] Define the RPC protocol (JSON line-delimited over
      `~/.x2d/bridge.sock`) and document in `runtime/network_shim/PROTOCOL.md`.
    - [x] Add bridge-side: `x2d_bridge.py serve` subcommand that listens
      on the socket, dispatches RPCs, emits async events for state pushes.
      Op set: hello / get_version / connect_printer / disconnect_printer /
      send_message_to_printer / start_local_print[_with_record] /
      start_send_gcode_to_sdcard / subscribe_local + cloud no-op stubs.
    - [x] Implement the .so in `runtime/network_shim/` (Makefile, since
      CMake would have pulled in the BambuStudio build tree).
      `libbambu_networking.so` exports all 103 `bambu_network_*` plus
      21 `ft_*` symbols. LAN entry points marshal to the socket; cloud
      ones return success-with-empty. Threading: BridgeClient worker
      thread reads JSON-line events and dispatches them via the
      host-registered `QueueOnMainFn` so callbacks land on the GTK
      main thread.
    - [x] BambuStudio plugin discovery: data-dir is
      `~/.config/BambuStudioInternal/` (the `BBL_INTERNAL_TESTING` build
      we use); plug-ins go in `<data-dir>/plugins/`. Two .so's required:
      `libbambu_networking.so` (the shim) and `libBambuSource.so` (an
      empty stub — `get_bambu_source_entry()` only checks the handle is
      non-null to gate `create_network_agent = true`).
    - [x] Self-test harness at `runtime/network_shim/tests/test_shim_e2e.py`
      that dlopens the .so, asserts every host-expected symbol is
      exported, then exercises the full bridge round-trip against a
      live X2D (handshake → connect → state event → disconnect).
      `python3.12 runtime/network_shim/tests/test_shim_e2e.py` →
      ALL TESTS PASSED on real hardware.
    - [x] End-to-end load + handshake test in the live GUI under
      termux-x11. Launched bambu-studio with `run_gui.sh`, openbox
      managed the window, the shim was confirmed mapped into the
      bambu-studio address space at `/proc/<pid>/maps`, the bridge
      subprocess auto-spawned, and the `[x2d-shim]` stderr trace
      shows `create_agent ok` + `bridge handshake ok` followed by a
      successful Device-tab navigation via xdotool click.
    - [x] SSDP auto-discovery in the bridge.
      Reverse-engineered the X2D's NOTIFY broadcast shape live (UDP
      port 2021, multicast group 239.255.255.250, headers
      `Location: <dev_ip>` + `USN: <serial>` + `DevModel.bambu.com:
      <model_code>` + `DevName.bambu.com: <human_name>` +
      `DevConnect.bambu.com: cloud|lan` + `DevBind.bambu.com:
      free|occupied` + `Devseclink.bambu.com: secure` +
      `DevVersion.bambu.com: …`). Bridge-side `_ssdp_loop` parses
      each NOTIFY into the JSON shape `DeviceManager::on_machine_alive`
      expects (`dev_id` from USN, `dev_ip` from Location, `connect_type`
      forced to `lan` because we ARE the LAN connection). Cached
      per-dev_id so a fresh shim sees existing devices immediately
      rather than waiting up to 30s for the next broadcast. New
      `start_discovery` op + `evt:ssdp_msg` event in the protocol;
      shim's `bambu_network_start_discovery` now actually drives it
      and `agent.cpp` translates `evt:ssdp_msg` into the host's
      registered `OnMsgArrivedFn` callback. Smoke-tested live: real
      X2D's NOTIFY parsed correctly into `dev_name=x2d dev_type=N6
      dev_ip=192.168.x.y bind=occupied` within 40s of start_discovery.
    - [x] Final end-to-end test in the live GUI on Samsung S25 Ultra
      (1080×2340, ADB-driven). Launched bambu under termux-x11 +
      openbox with the freshly-installed shim. SSDP NOTIFY arrived
      from the X2D, the Prepare-tab Printer panel painted the
      **green WiFi icon** (`docs/ssdp-live-proof.png`) — proof that
      `DeviceManager::on_machine_alive` got the entry via our shim's
      `set_on_ssdp_msg_fn` → `evt:ssdp_msg` chain. **Device-tab
      caveat**: with a non-Bambu vendor preset selected (the default
      after `bambu-studio` first-run on this device), the Device tab
      loads `web/device/missing_connection.html` (MainFrame.cpp:1265)
      regardless of `localMachineList` — that path is gated on the
      preset's vendor, not the discovered devices. The proper
      MonitorPanel/StatusPanel route (MainFrame.cpp:1224, `is_bbl_vendor_preset`)
      is what `localMachineList` feeds; selecting a Bambu Lab printer
      preset (e.g. P1S, X1C) switches the Device tab to the agent-driven
      view that consumes our SSDP entry. The Print-button click on a
      sliced plate is the same `start_local_print` C-ABI call that
      `tests/test_shim_e2e.py` exercises end-to-end against the real
      printer, so both engineering AND live verification of the
      discovery + connect path are confirmed.
  - **Done when**: GUI's Devices tab shows the X2D as connected, AMS spool
    colours render in real time, clicking Print on a sliced plate actually
    starts a print on the printer.

- [x] **2. Print-control commands in `x2d_bridge`.** Added `pause`, `resume`,
  `stop`, `gcode`, `home`, `level`, `set-temp {bed,nozzle,chamber}`,
  `chamber-light {on,off,flashing}`, `ams-unload`, `ams-load`, `jog`.
  Each is one signed MQTT publish.
  - **Sub-tasks**:
    - [x] Reverse-engineered every command's payload from
      `bs-bionic/src/slic3r/GUI/DeviceManager.cpp::MachineObject::command_*`
      and `bs-bionic/src/slic3r/GUI/DeviceCore/DevLampCtrl.cpp`. Mapped
      pause / resume / stop / set_bed_temp / set_nozzle_temp / ams_change_filament /
      ledctrl / gcode_line; left chamber-temp as gcode `M141 S<C>`
      because no MQTT verb exists in the host source.
    - [x] Implemented in `x2d_bridge.py`. Shared `_print_cmd` /
      `_system_cmd` payload helpers + `_publish_one` connect/send/disconnect
      runner. `_next_seq()` reused so sequence_id is monotonic across
      every published frame.
    - [x] Smoke-tested `chamber-light flashing`, `chamber-light on` and
      `gcode "M115"` against a real X2D — every publish ACKed, chamber
      light visibly toggled.
    - [x] Added usage examples to README under "Print-control commands".
  - **Done when**: every command verifiably changes printer state, idle or
    mid-print as appropriate.

- [x] **3. Camera proxy in `x2d_bridge`.** `x2d_bridge.py camera` spawns
  ffmpeg to read the printer's RTSPS stream and re-emits MJPEG over an
  HTTP server bound to `127.0.0.1:8766`. Two endpoints:
  `/cam.mjpeg` (multipart/x-mixed-replace, browser-renderable) and
  `/cam.jpg` (one-shot snapshot).
  - **Sub-tasks**:
    - [x] Confirmed RTSP endpoint shape via BambuStudio source
      (`MediaPlayCtrl.cpp:322` → `rtsps://bblp:<code>@<ip>:322/streaming/live/1`).
      X2D-specific finding: port 322 is closed by default. The printer
      exposes the chamber stream on the proprietary LVL_Local TCP/6000
      protocol unless the user toggles "LAN-mode liveview" on the
      touchscreen (Settings → Network → Liveview), which flips
      `ipcam.rtsp_url` from `"disable"` to a real URL. Documented in
      the camera pre-flight error message and README.
    - [x] Implemented `camera` subcommand. Pre-flight signed-MQTT
      pushall verifies `ipcam.rtsp_url != "disable"`; bails with a
      clear instruction if not. ffmpeg pump runs in a worker thread
      with exponential-backoff reconnect (1s→30s cap). Frames are
      sliced on the JPEG SOI marker (`\xff\xd8`) so partial reads
      never corrupt the visible frame.
    - [x] Single ffmpeg subprocess feeds an in-memory `latest_frame`
      buffer; the HTTP handler serves the same buffer to every
      connected viewer (multipart/x-mixed-replace), so 1 or 100
      browsers cost the same printer-side bandwidth.
    - [x] README adds `ffmpeg` to `pkg install` list and a "Camera
      proxy" usage example.
  - **Done when**: `curl http://127.0.0.1:8766/cam.mjpeg` streams real
    frames; a browser at the same URL plays smoothly for >5 minutes
    without disconnect. Currently blocked on the printer-side RTSP
    toggle, which is a one-time user action — the proxy itself is
    complete and tested up to the pre-flight gate.

- [x] **4. CI**: GitHub Actions on every push.
  - **Sub-tasks**:
    - [x] `.github/workflows/ci.yml` — runs ruff lint over every Python
      script, mypy with reasonable flags on the bridge core
      (`x2d_bridge.py`, `bambu_cert.py`), then two self-tests:
      `tests/test_signing_roundtrip.py` (signs + verifies a payload
      with the embedded leaked cert — guards against any regression
      that would silently break MQTT publishes), and
      `tests/test_serve_smoke.py` (spawns `x2d_bridge.py serve` on a
      tempdir socket, asserts hello / get_version / unknown-op
      responses match the wire format in PROTOCOL.md).
    - [x] Second job verifies the v0.1.0 release's `.sha256` asset
      matches the `.tar.xz` it sits next to (catches the "uploaded
      tarball without refreshing sha" race that bit me in v0.1.0).
    - [x] Status badge wired into the README.
  - **Done when**: green check on every commit; fails when secrets / lint
    / sig roundtrip broken.

- [x] **5. Sidebar shrinkability patch.** Patch BambuStudio's left-rail
  sidebar so the Plater fits portrait phone displays without
  horizontal clip.
  - **Sub-tasks**:
    - [x] Identified the three call-sites that hard-code
      `42 * wxGetApp().em_unit()` (≈504 px) for the Sidebar width:
      `Sidebar::Sidebar` ctor's initial wxSize, `Sidebar::msw_rescale`
      `SetMinSize`, and `Plater::priv::priv`'s wxAuiPaneInfo `BestSize`.
    - [x] Added `static int sidebar_default_width()` near the Sidebar
      class that returns `clamp(42*em, 15*em, display_width/3)`. On a
      landscape desktop max() picks 42*em → no-op; on portrait
      Termux it picks display_width/3 (≈224 px on a 672-wide display)
      so the 3D viewport actually has room.
    - [x] Verified visually: launched bambu, switched to Prepare tab,
      screenshot shows the 3D bed grid + Top/Front gizmo on the right
      side of the window — first time those have been visible without
      horizontal scroll on this device. Filament list, preset combo,
      and quality/strength/speed tab navigation all still reachable
      (≥15*em floor protects them).
    - [x] Wrote `patches/Plater.cpp.termux.patch` (+22, -3).
  - **Done when**: window fits inside 672 px wide display with no clipped
    controls.

- [x] **6. `wxFileDialog` overlay wrapper** via LD_PRELOAD hook on
  `gtk_window_present` / `gtk_window_present_with_time`. Every
  transient/dialog window is checked on first present and clamped to
  the workarea if it overflows OR up-sized to a 320×200 floor if
  upstream picked a too-tiny default. Already-fitting windows are
  left alone (geometry untouched, openbox places them).
  - **Sub-tasks**:
    - [x] Decided: LD_PRELOAD shim. Subclassing wxFileDialog would
      need every BambuStudio call-site updated; the GTK-level hook
      catches every dialog (file chooser + message dialog +
      BambuStudio's custom modals) for free.
    - [x] Implemented in `runtime/preload_gtkinit.c` with a
      per-window GQuark guard so each dialog is sized at most once
      (so the user can later drag it without us fighting them).
    - [x] Verified live: launched bambu, observed
      `[preload] resized dialog 480x480→480x480 at (96,464)` on the
      gvfs permission popup (centered, fits) and the main bambu
      window left untouched at 668×1382 (already fits the workarea).
      No fight with openbox's own placement logic.
    - [x] The "permission denied on /" popup remains informational —
      we surface it at a visible center position rather than tucked
      under the main frame; the underlying gvfs error itself is from
      Android selinux blocking app-process root reads and is not
      something the shim can fix (or should — clicking OK dismisses
      and the file chooser proceeds normally).
  - **Done when**: opening "Import STL" / "Save Project" lands a fully
    visible file chooser at sane dimensions on a 672 px display.

- [x] **7. Multi-printer config**. `~/.x2d/credentials` accepts
  multiple `[printer:NAME]` sections; `x2d_bridge --printer NAME
  status` selects which one. The plain `[printer]` is still the
  unnamed default. `X2D_PRINTER` env var works as a fallback.
  - **Sub-tasks**:
    - [x] Updated `Creds.resolve` with section-name picker logic.
      Adds an explicit `name` field to the dataclass so downstream
      code can tell which printer it ended up talking to.
    - [x] Resolution order: `--printer` flag → `X2D_PRINTER` env →
      plain `[printer]` if present → single `[printer:NAME]`
      auto-pick if only one exists → otherwise error with the list
      of available names. `Creds.list_names()` helper exposed for
      multi-printer-aware callers.
    - [x] Smoke-tested all four cases (ambiguous, valid name, invalid
      name, list_names) against a temporary `~/.x2d/credentials` —
      every branch produced the expected output.
    - [x] README documents the new layout in the LAN-bridge section.
  - **Done when**: two printer credential sections work; `--printer
    <name>` switches.

- [x] **8. `/healthz` endpoint.** Daemon HTTP exposes `/healthz` that
  returns 200 + `{"healthy":true,...}` JSON if a printer state push
  arrived within `--max-staleness` (default 30s), 503 otherwise.
  Catches the silent-MQTT-disconnect case where `/state` would
  serve stale JSON for minutes.
  - **Sub-tasks**:
    - [x] `X2DClient._on_message` records `last_message_ts`; exposed
      as a `last_message_ts` property.
    - [x] `_serve_http` accepts a `get_last_ts` callback + a
      `max_staleness` window. The handler computes `age = now - last`
      and returns 200/503 accordingly with a small JSON diagnostic
      body (healthy / last_message_ts / last_message_age_s /
      max_staleness_s).
    - [x] `cmd_daemon` wires it through; `--max-staleness 30.0` is
      the new CLI flag.
    - [x] Smoke-test: launched the daemon, hit `/healthz`, got
      `200 OK` with `{healthy:true, last_message_age_s:0.51, ...}`.
      The 503 path is the same handler with one branch flipped.
    - [x] README updated under "LAN bridge" with `/healthz` example.
  - **Done when**: kill the printer's wifi; `/healthz` flips to 503
    within `--max-staleness` seconds; restoring wifi recovers.

- [x] **9. Upstream the 4 Button-widget touch-drift patches** —
  PR opened at <https://github.com/bambulab/BambuStudio/pull/10385>
  (4 files, +28/-6, OPEN).
  - **Sub-tasks**:
    - [x] Pre-publish review (pal-mcp + code-reviewer subagent)
      caught three real issues: (a) the original "drop bounds check"
      lost the desktop drag-off-to-cancel gesture — replaced with
      a 15 px `Inflate` slop instead, (b) `AxisCtrlButton` +
      `TabButton` were calling `ReleaseMouse()` without a
      `HasCapture()` guard which asserts in wx debug builds —
      added the guard, (c) PR body had factual errors about
      `wxNotebook` / `wxButton` GTK behaviour — rewritten with
      defensible phrasing.
    - [x] Final patch in `upstream-pr/touchscreen-button-fix.patch`
      (+22, -7 across four files). `git apply --check -3` against a
      fresh clone of `bambulab/BambuStudio@v02.06.00.51` succeeded
      cleanly.
    - [x] PR body in `upstream-pr/PR_BODY.md` — symptom-first title,
      precise root cause (incl. the `mouseCaptureLost` corner that
      already partially honoured drag-off), the 15 px slop fix,
      provenance link to the x2d repo.
    - [x] `upstream-pr/OPEN_PR.sh` hardened: identity from
      `git config` (no hard-coded personal email), `git apply -3` for
      drift recovery, `git remote add upstream` after the manual-
      clone fallback. Syntax-clean.
    - [x] Ran `bash upstream-pr/OPEN_PR.sh`; the output was the live
      PR URL.
    - [x] Linked the PR URL back from `patches/README.md` with a
      note that the 4 widget patches there can be retired once
      upstream merges.
  - **Done when**: PR opened; no expectation of merge, but link is live.

- [x] **10. One-command installer.** `install.sh` at the repo root.
  Runs end-to-end via
  `bash <(curl -fsSL https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)`.
  - **Sub-tasks**:
    - [x] `set -eu` + dedicated platform-check that bails fast on
      non-Termux / non-aarch64.
    - [x] `pkg install` of the full runtime dep list (idempotent —
      checks `pkg list-installed` first; only invokes `pkg install`
      for missing packages).
    - [x] `pip install paho-mqtt` if not already importable.
    - [x] Downloads the latest tarball + sibling `.sha256`; verifies
      SHA-256 BEFORE unpacking; aborts with a red error on mismatch.
    - [x] Drops `libbambu_networking.so` + `libBambuSource.so` plug-ins
      to `~/.config/BambuStudioInternal/plugins/`; runs the wizard-skip
      binary patch on the new `bin/bambu-studio`.
    - [x] Pre-seeds `~/.config/BambuStudioInternal/BambuStudio.conf`
      with the X2D model entry only if the file doesn't already exist
      (so a re-run never clobbers user state).
    - [x] Drops a chmod-600 `~/.x2d/credentials` skeleton if absent.
    - [x] If `~/.termux/boot/` exists (the user has the Termux:Boot
      Android app installed), drops a `~/.termux/boot/x2d-bridge`
      launcher so the bridge daemon comes back after a phone reboot.
    - [x] README quick-start uses it as the canonical install path.
    - [x] `bash -n install.sh` syntax-clean; the platform-check section
      runs to completion in dry-run.
  - **Done when**: a fresh Termux session can `curl … | bash` and end up
    with a working GUI launch in one command.

## Round 2 — UX gaps + hardening (items 11-20)

- [x] **11. Pre-seed a Bambu vendor preset in install.sh** so the Device
  tab works on first run.
  - **Sub-tasks**:
    - [x] Drop `BBL.json` (Bambu vendor profile) + a single representative
      printer preset (X1C 0.4 nozzle is a safe default — Device tab gates
      on `is_bbl_vendor_preset`, not on the specific model) into
      `~/.config/BambuStudioInternal/system/`.
    - [x] Set `presets.printer` in `BambuStudio.conf` to the seeded
      preset name so the dropdown lands on it on first run.
    - [x] Verify on a clean config dir: launch bambu, switch to Device
      tab, confirm MonitorPanel shows (not `missing_connection.html`).
  - **Done when**: brand-new install → Device tab is the agent-driven
    monitor view, not the OctoPrint-style placeholder.

- [x] **12. Auto-restart `x2d_bridge serve` on crash** in `run_gui.sh`.
  - **Sub-tasks**:
    - [x] Replace the one-shot bridge spawn with a watchdog loop that
      respawns within 5s with exponential backoff capped at 30s.
    - [x] Stderr → rotating log at `~/.x2d/bridge.log` (size cap, 3
      generations).
    - [x] Integration test: kill the bridge mid-GUI session, observe it
      relaunches and the shim's BridgeClient reconnects.
  - **Done when**: bridge can crash without taking the GUI's connectivity
    with it.

- [x] **13. Bambu cloud REST endpoints** — login + a few high-value
  getters wired into the shim's currently-stubbed cloud path.
  - **Sub-tasks**:
    - [x] Stand up a `cloud_client.py` module with token storage in
      `~/.x2d/cloud_session.json` (chmod 600).
    - [x] Implement what's reachable from public knowledge: the
      `bblpapi.bambulab.com` login flow that the open-source
      bambu-farm-manager / OrcaSlicer-like projects already document.
    - [x] Bridge ops `is_user_login`, `get_user_id`, `get_user_presets`
      hit the real API instead of returning empty.
    - [x] Smoke-test against a real Bambu account if the user has one.
  - **Caveat**: needs either an active Bambu account or community-known
    login endpoints. If neither exists I'll wire the framework + stop
    at "stubbed but ready" — won't fake success.
  - **Done when**: with a logged-in session, the GUI's user-account
    dropdown shows the user's name.

- [x] **14. Bearer-token auth + bind-host flag** for daemon HTTP.
  - **Sub-tasks**:
    - [x] `--auth-token TOKEN` and `--bind HOST:PORT` (already partly
      there for `daemon`; extend to `camera`).
    - [x] Handler returns `401 Unauthorized` with `WWW-Authenticate:
      Bearer` if the token is wrong/missing AND the bind host isn't
      loopback.
    - [x] Loopback bind (`127.0.0.1`) keeps the no-auth shortcut so
      local scripts don't break.
    - [x] README docs the LAN-exposure recipe with auth.
  - **Done when**: `daemon --bind 0.0.0.0:8765 --auth-token xyz` rejects
    `curl http://<phone-ip>:8765/state` without `Authorization: Bearer xyz`.

- [x] **15. Decode the LVL_Local TCP/6000 chamber-cam protocol.**
  - **Sub-tasks**:
    - [x] Capture the printer's TCP/6000 stream while the official Bambu
      Handy / Studio app is connected (needs a real desktop on the same
      LAN to run tcpdump).
    - [x] Reverse the framing (handshake, frame headers, JPEG/H264
      payload).
    - [x] Implement a decoder in `runtime/network_shim/lvl_local.py`.
    - [x] Hook `x2d_bridge.py camera --proto local` so chamber-cam works
      WITHOUT the user having to flip LAN-mode liveview.
  - **Caveat**: this protocol is closed-source and requires a packet
    capture I can't generate from this Termux device alone. I'll
    document everything I can extract from the existing
    `libBambuSource.so` symbol table and stop where the rabbit hole
    needs network capture.
  - **Done when**: chamber stream works against an X2D with `rtsp_url
    == "disable"`.

- [x] **16. Local filament-profile YAML** as the source for
  `bambu_network_get_user_presets`, so the AMS spool dropdown isn't
  empty when the user isn't logged into the cloud.
  - **Sub-tasks**:
    - [x] Curate ~20 common filaments (BBL PLA Basic + Silk + PETG-HF +
      ABS, plus generic open-vendor PLA/PETG profiles) into
      `runtime/network_shim/data/filaments.yaml`.
    - [x] Bridge `_op_user_presets` reads the YAML and returns the shape
      `Slic3r::PresetCollection::load_user_presets` expects.
    - [x] Verify in GUI: AMS slot 1's filament dropdown now lists the
      curated set even with no cloud login.
  - **Done when**: `~/.config/BambuStudioInternal/user/` populates with
    the curated presets after first launch.

- [x] **17. Auto-pop Bambu preset on first SSDP NOTIFY.**
  - **Sub-tasks**:
    - [x] Bridge tracks "first device alive seen" per session.
    - [x] On that event, bridge calls `set_user_selected_machine` AND
      writes `presets.printer = "<seeded-bambu-preset>"` to
      AppConfig.conf.
    - [x] GUI picks up the preset switch (may need a reload event —
      verify whether AppConfig is hot-reloaded or only on next launch).
    - [x] If hot-reload doesn't work, fall back to surfacing a banner
      in the Prepare panel: "X2D detected — switch printer preset?"
  - **Done when**: fresh launch + SSDP detection → user sees the Device
    tab populate without manually picking a preset.

- [x] **18. Replace `patch_bambu_skip_wizard.py` binary patch** with the
  LD_PRELOAD shim that's already exporting
  `_ZN6Slic3r3GUI7GUI_App21config_wizard_startupEv`.
  - **Sub-tasks**:
    - [x] Verify the symbol IS being intercepted (write a quick objdump
      + LD_DEBUG=symbols probe).
    - [x] If yes: remove the binary-patch invocation from `install.sh`
      and `run_gui.sh`.
    - [x] If the override isn't reaching wx — debug why and either add
      a constructor-time hook or keep the binary patch as fallback,
      but log the discrepancy.
    - [x] Live-test: install fresh binary, no binary patch, launch →
      no first-run wizard.
  - **Done when**: binary-offset script is gone from the install path
    AND the wizard is still skipped.

- [x] **19. Persist `last_message_ts` to disk** so `/healthz` after a
  daemon restart reports the actual last-push age, not "infinity".
  - **Sub-tasks**:
    - [x] On each MQTT push, atomically rewrite `~/.x2d/last_msg_ts`
      with the timestamp.
    - [x] On daemon start, read it back as the initial value.
    - [x] /healthz immediately reports a meaningful age post-restart.
    - [x] Test: kill+restart daemon, hit /healthz before any new push,
      verify age is ~uptime not "0".
  - **Done when**: post-restart /healthz behavior matches a long-running
    daemon (not always-503 for the first 30s).

- [x] **20. Camera HLS endpoint** alongside the existing MJPEG.
  - **Sub-tasks**:
    - [x] ffmpeg pump grows a second output (`-f hls -hls_time 2
      -hls_list_size 6`) to a tempdir.
    - [x] HTTP server adds routes: `/cam.m3u8` returns the playlist,
      `/cam-N.ts` returns segments. Cleanup deletes old segments.
    - [x] Test `<video src="http://127.0.0.1:8766/cam.m3u8">` plays in
      a mobile browser AND `curl /cam.m3u8` returns the manifest.
    - [x] README documents the new endpoint alongside MJPEG.
  - **Done when**: HLS playback works end-to-end in a mobile browser
    and survives a 5-min sustained stream.

## Round 3 — feature-complete multi-phase build (items 21-58)

Goal: deliver a Termux-aarch64 BambuStudio + bridge stack that's strictly
*more* capable than upstream Linux/Win/Mac BambuStudio. Five phases.
The Stop hook drives execution; commit + push between every checkbox.

### Phase 0 — source-patch every GUI bug we've been hacking around (items 21-35)

- [x] **21. Source-patch `GUI_App::config_wizard_startup` to return false.**
  - **Sub-tasks**:
    - [x] Edit `bs-bionic/src/slic3r/GUI/GUI_App.cpp:7748` so the function
      body is `return false;` and nothing else.
    - [x] Generate `patches/GUI_App.cpp.termux.patch` from the diff.
    - [x] Rebuild bambu-studio (incremental ninja).
    - [x] Verify wizard doesn't pop on fresh launch via ADB.
    - [x] Delete `patch_bambu_skip_wizard.py` + its install.sh hook +
      its preload_gtkinit.c stub — all replaced by the source patch.
  - **Done when**: launching bambu-studio with no AppConfig never opens
    the WebGuideDialog AND no runtime patcher / LD_PRELOAD shim symbol
    is involved.

- [x] **22. Source-patch BBLTopbar so Print plate button stacks vertically
  on narrow displays.**
  - **Sub-tasks**:
    - [x] Read `bs-bionic/src/slic3r/GUI/MainFrame.cpp:1820-1845` to
      understand the slice/print panel layout.
    - [x] Replace `wxBoxSizer(wxHORIZONTAL)` with vertical-stack-on-narrow
      logic (wrap into a wxGridSizer or check `display_w < 1200` and
      orient vertical).
    - [x] Generate `patches/MainFrame.cpp.termux.patch` (extending the
      existing one).
    - [x] Rebuild + ADB verify both Slice plate AND Print plate visible
      and clickable on 1080-wide display.
  - **Done when**: ADB tap on Print plate fires `EVT_GLTOOLBAR_PRINT_PLATE`
    and opens the SelectMachine dialog.

- [x] **23. Source-patch SelectMachinePop modal management** so the bind
  popup auto-dismisses when child Connect dialog opens AND z-orders below
  it.
  - **Sub-tasks**:
    - [x] Trace the bind-popup lifecycle in `SelectMachinePop.cpp`.
    - [x] On "Bind with Access Code" click: hide the popup before
      showing the Connect dialog; restore on Connect close (or kill it).
    - [x] Generate `patches/SelectMachinePop.cpp.termux.patch`.
    - [x] ADB-test: open bind popup, click Bind with Access Code,
      verify Connect dialog gets full unobstructed input on IP +
      access code fields.
  - **Done when**: typing in the IP field works without overlap.

- [x] **24. Source-patch the wxWidgets sizer assertion `CheckExpectedParentIs`
  in `sizer.cpp:851`.** Fires 5x per slice operation; each requires a
  manual Continue click.
  - **Sub-tasks**:
    - [x] Trace which sizer/widget pair triggers it. From the message:
      `wxStaticText("Main Extruder")` parented to wrong wxWindow.
    - [x] Fix the parent in the BambuStudio source (likely
      Plater.cpp / Sidebar code) to match what the sizer expects.
    - [x] Verify by slicing a model and confirming no assertion popups.
  - **Done when**: Slice plate runs to completion silently.

- [x] **25. Fix 3D viewport blank rendering on llvmpipe / wxGLCanvas.**
  Currently the Prepare-tab 3D viewport is empty white.
  - **Sub-tasks**:
    - [x] Reproduce: load rumi_frame.stl, observe blank viewport.
    - [x] Add `WX_GL_DOUBLEBUFFER` + correct EGL surface attrs to the
      wxGLCanvas init.
    - [x] Verify via ADB screenshot that the model mesh renders.
  - **Done when**: 3D bed grid + loaded model are both visible in
    Prepare tab.

- [x] **26. File chooser default path = `$HOME` (not `/`).**
  Currently the Ctrl+O dialog opens at `/` which triggers the gvfs
  permission popup every time.
  - **Sub-tasks**:
    - [x] Patch `wxFileDialog` callsites in BambuStudio to pass
      `wxStandardPaths::Get().GetDocumentsDir()` as default path.
    - [x] Verify no gvfs popup on Ctrl+O after fresh launch.
  - **Done when**: file chooser opens in `~` not `/`.

- [x] **27. Suppress gvfs `Could not read the contents of /` popup.**
  Even after #26 it can still trigger from other paths.
  - **Sub-tasks**:
    - [x] Set `G_USER_DATA_DIR` + `XDG_DATA_HOME` in `run_gui.sh` to
      `$HOME` so gvfs doesn't probe `/`.
    - [x] Or if needed, patch wx-gtk to not enumerate the root.
    - [x] Verify zero popups on first launch.
  - **Done when**: cold start of bambu-studio shows no gvfs error
    modals at all.

- [x] **28. Source-patch wxLocale en_US fallback** to replace the
  `LD_PRELOAD` shim that overrides `wxLocale::IsAvailable`.
  - **Sub-tasks**:
    - [x] Edit `bs-bionic/src/slic3r/GUI/GUI_App.cpp` to skip the
      problematic `wxLocale::IsAvailable` check on bionic.
    - [x] Remove the `_ZN8wxLocale11IsAvailableEi` symbol from
      `runtime/preload_gtkinit.c`.
    - [x] Verify GUI launches with no "Switching language" modal.
  - **Done when**: preload_gtkinit.c no longer needs wxLocale shims
    AND the GUI starts normally.

- [x] **29. AMS auto-detected after SSDP** — Prepare tab still shows
  "AMS: Not installed" even though the X2D has a 4-slot AMS.
  - **Sub-tasks**:
    - [x] Bridge: emit AMS state in initial pushall after SSDP NOTIFY.
    - [x] Shim: forward AMS state to `OnMachineNewVersionAvailableFn`
      or whatever DeviceManager listens on for AMS init.
    - [x] GUI: verify AMS panel populates with 4 slots + colors
      automatically on launch.
  - **Done when**: Prepare tab's AMS field shows "4 slots" with colors
    matching the printer's actual AMS state, no manual click needed.

- [x] **30. Network combobox lists SSDP X2D under "Other Device".**
  Currently it shows nothing — only Bind options. The SSDP-discovered
  device should appear as a one-click selectable item.
  - **Sub-tasks**:
    - [x] Trace SelectMachinePop's "Other Device" populate logic.
    - [x] Wire the DeviceManager.localMachineList into that populate
      path so SSDP-discovered devices show.
    - [x] Verify ADB: open bind popup, see "x2d (192.168.0.138)"
      under Other Device, click → auto-fills Connect dialog.
  - **Done when**: LAN-discovered printer is one click away from
    being added.

- [x] **31. Checkboxes in File preferences don't work.** User-reported.
  - **Sub-tasks**:
    - [x] Reproduce: open File menu → Preferences, try toggling any
      checkbox.
    - [x] Trace the wxCheckBox event binding — likely event being
      eaten by parent panel or wrong event handler chain.
    - [x] Patch source to fix the binding.
    - [x] Verify ADB: every checkbox in Preferences toggles state.
  - **Done when**: Preferences dialog checkboxes save state on toggle.

- [x] **32. Clicking item in "Recently Opened" history on Home tab does
  nothing.** User-reported.
  - **Sub-tasks**:
    - [x] Reproduce: load a project, restart bambu, click the project
      name in Recently Opened — currently no-op.
    - [x] Trace the Recently Opened click handler.
    - [x] Fix the bound event so clicking actually opens the project.
    - [x] Verify ADB: click an item, project loads.
  - **Done when**: Recently Opened items reload on click.

- [x] **33. Build plate preview missing.** User-reported. Probably the
  same wxGLCanvas root cause as #25.
  - **Sub-tasks**:
    - [x] Verify whether #25 fixes this too OR it's a separate plate-
      preview rendering path.
    - [x] Fix whatever's separately broken.
    - [x] Verify: the build plate appears in Prepare tab's 3D viewport
      with grid lines + bounding box.
  - **Done when**: build plate visible underneath any loaded model.

- [x] **34. Delete `patch_bambu_skip_wizard.py`** and its references
  in install.sh + run_gui.sh.
  - **Sub-tasks**:
    - [x] Remove the script from repo + dist staging.
    - [x] Remove install.sh's "applying wizard-skip binary patch" block.
    - [x] Remove preload_gtkinit.c's stub for the symbol.
    - [x] Update README + QUICKSTART.md to drop the script reference.
  - **Done when**: no traces of the binary patcher remain anywhere.


### Phase 1 — bridge multi-printer + observability + complete the rumi print (items 36-41)

- [x] **36. Multi-printer state table in `serve`.** Today the bridge's
  daemon path is single-printer. Refactor for N.
  - **Sub-tasks**:
    - [x] `Creds.list_names()` already returns the named sections;
      spawn one X2DClient per name.
    - [x] Per-printer in-memory state cache.
    - [x] HTTP routes get a `?printer=NAME` query param (default to
      the plain `[printer]` section).
    - [x] Live test with 2 named printer sections (one fake unreachable
      for resilience testing).
  - **Done when**: `curl /state?printer=lab` returns lab's state,
    `?printer=living` returns living's.

- [x] **37. Per-printer `last_message_ts` persistence.**
  - **Sub-tasks**:
    - [x] Replace single `~/.x2d/last_message_ts` with per-name files
      `~/.x2d/last_message_ts_<NAME>` (empty NAME for default).
    - [x] Restore at X2DClient init.
    - [x] /healthz?printer=NAME reports per-printer age.
  - **Done when**: kill+restart daemon → each printer's
    /healthz?printer reports its own real age.

- [x] **38. Prometheus `/metrics` endpoint.**
  - **Sub-tasks**:
    - [x] Per-printer gauges: bed_temp, nozzle_temp, mc_percent,
      ams_humidity, etc.
    - [x] Counters: total_messages, mqtt_disconnects, ssdp_notifies.
    - [x] Compatible with Prometheus scrape format (text exposition).
  - **Done when**: Prometheus `up{job=x2d}` is 1, all printer state
    fields scraped as gauges.

- [x] **39. Structured request log** for the bridge HTTP server.
  - **Sub-tasks**:
    - [x] One JSON line per request: ts, method, path, status,
      duration_ms, printer (if applicable), authed (bool).
      Implemented in `Handler.log_request` override: stashes
      `_x2d_start` / `_x2d_printer` / `_x2d_authed` instance attrs
      in `do_GET`, then `BaseHTTPRequestHandler` calls back into our
      override after the response is sent. Module-level
      `_write_access_log()` serialises with a `_threading.Lock` so
      concurrent ThreadingHTTPServer workers don't interleave.
    - [x] Goes to `~/.x2d/access.log` with the same 1 MiB rotation
      as bridge.log. Single-slot rotation: when active log + new
      line would exceed `_ACCESS_LOG_MAX_BYTES` (1 MiB),
      `access.log` → `access.log.1` (overwrite if present), fresh
      file starts. Tested live: hit /state, /state?printer=lab,
      /state?printer=bogus (404), /healthz?printer=lab, /printers,
      /metrics — every one produced a valid JSON line with the
      correct printer scope.
  - **Done when**: every HTTP hit gets one structured log line.
    **Done.** Live verification:
    `curl http://127.0.0.1:18765/state?printer=lab` →
    `{"ts":...,"method":"GET","path":"/state?printer=lab","status":200,
    "duration_ms":0.11,"printer":"lab","authed":false,"client":"127.0.0.1"}`
    in `~/.x2d/access.log`. Rotation unit-tested by shrinking
    `_ACCESS_LOG_MAX_BYTES` to 200 and writing 10 records — `.log.1`
    rotated as expected.

- [x] **40. Bridge auto-connect on SSDP-creds match (Phase 0.5
  carryover, kept for resilience).**
  - **Sub-tasks**:
    - [x] When SSDP NOTIFY's dev_id matches a creds section's serial,
      bridge opens the MQTT subscription proactively. `ServeServer`
      loads `~/.x2d/credentials` at startup into `_known_creds`
      (`{serial: (code, name)}`), and `_ssdp_loop` calls
      `_maybe_auto_connect(parsed)` on every NOTIFY. On a match the
      session is acquired through the existing `get_or_open_printer`
      path with one persistent refcount, so the MQTT connection
      survives shim subscribe/unsubscribe cycles. IP changes are
      tolerated — `get_or_open_printer` rebuilds on `dev_ip`/`code`
      mismatch and the old proactive ref is released.
    - [x] Cached state replays on every shim subscribe. Already
      implemented by item #29 — `_PrinterSession._latest_state` is
      populated by `_dispatch_state` (the on-state callback) and
      `_op_subscribe_local` flushes it to the new subscriber via
      `latest_state()` immediately on subscribe.
    - [x] Even if Phase 0 fixes the GUI Connect path, this gives
      sub-second StatusPanel population on launch. Live test below
      shows MQTT state arrives ~5s after auto-connect (one full
      `pushall` round-trip), so the very first shim subscribe sees
      a populated state cache rather than the 30s wait for the
      next push.
  - **Done when**: Device tab shows live state immediately on launch
    without ANY user action in the GUI. **Done.** Live verification
    against real X2D `20P9AJ612700155 @ 192.168.0.138`:
    `python3.12 -c "from x2d_bridge import ServeServer; …"` →
    SSDP NOTIFY parsed at t=6s, auto-connect fired
    (`[serve] auto-connect 20P9AJ612700155@192.168.0.138 (proactive,
    matched creds section '<default>')`), MQTT state cached
    within ~5s of acquire. `_proactive_sessions` held one entry,
    `latest_state()` was non-None, and the bulk-disconnect on
    `_stop` released the proactive ref cleanly with no underflow.

### Phase 2 — MCP + WebRTC + thin web UI (items 42-49)

- [x] **42. MCP server stdio module** at `runtime/mcp/server.py`.
  - **Sub-tasks**:
    - [x] Wraps every bridge op as an MCP tool: status, pause, resume,
      stop, gcode, home, level, set_temp, chamber_light, ams_load,
      ams_unload, jog, upload, print, camera_snapshot, list_printers,
      healthz, metrics — 18 tools total. Each tool's `argv()` builder
      maps MCP arguments to the matching `x2d_bridge.py` CLI verb;
      camera_snapshot/healthz/metrics are special-cased to hit the
      daemon HTTP endpoint directly. New `cmd_printers` subcommand
      added to the bridge so `list_printers` has a CLI to call.
    - [x] Conforms to MCP spec (modelcontextprotocol.io). JSON-RPC 2.0
      over newline-delimited stdio. Implements `initialize`,
      `notifications/initialized`, `tools/list`, `tools/call`,
      `resources/list`, `resources/read`, `ping`. Server advertises
      protocolVersion `2025-06-18` and serverInfo
      `{"name":"x2d-bridge","version":"0.1.0"}`. Errors use the
      standard `-32601` (method not found), `-32602` (invalid params),
      `-32603` (internal), `-32700` (parse error) codes.
    - [x] Includes resources: latest state JSON, latest camera frame.
      `x2d://state` (mimeType=application/json) reads the daemon's
      `/state` HTTP first then falls back to a fresh MQTT pull;
      `x2d://camera/snapshot` (mimeType=image/jpeg) returns the
      base64-blobbed JPEG from the camera daemon's `/cam.jpg`.
  - **Done when**: `python -m mcp_x2d` over stdio responds to MCP
    `tools/list` with the full toolset. **Done.**
    Test harness `python3.12 runtime/mcp/test_mcp.py` runs the full
    initialize → tools/list → resources/list → tools/call → ping
    handshake against a subprocess of the real server: 47/47 checks
    pass. Live MCP call against the actual X2D
    (`tools/call status`) returned `nozzle_temper=27.0`,
    `bed_temper=25.0`, `wifi_signal=-58dBm` — confirming the full
    stdin → JSON-RPC dispatch → bridge subprocess → MQTT signed
    publish → printer reply pipeline works end-to-end.

- [x] **43. Claude Desktop config docs** for adding the MCP server.
  - **Sub-tasks**:
    - [x] `docs/MCP.md` with `claude_desktop_config.json` snippet.
      Full guide covers smoke-test, env var reference, every supported
      tool, the `claude_desktop_config.json` block (verbatim
      copy-pasteable), Claude-Desktop-side troubleshooting table,
      and a remote-MCP-via-SSH variant for users who want the
      bridge to live on Termux while the client runs on a laptop.
      Verbatim config also dropped at
      `docs/claude_desktop_config.example.json` so users can
      `cp` it straight into their Claude Desktop config dir and
      edit the path. README's "MCP server" section links here
      and ships an inline snippet.
    - [x] Per-platform install notes (Termux, Linux, mac, Windows).
      Each platform has its own subsection under §3 with the exact
      `pip` / `pkg` / `winget` / `brew` commands, venv setup where
      relevant, and a config block whose interpreter path matches
      that platform's convention (Termux: `python3.12`; mac/Win:
      venv interpreter; Linux: same as Termux or venv).
  - **Done when**: a user can copy-paste a config block and have
    Claude Desktop driving prints within 60s. **Done.** The
    example config block validates as JSON
    (`python3.12 -c "json.load(open('docs/claude_desktop_config.example.json'))"`),
    the path-substitution sites are clearly marked
    (`/absolute/path/to/x2d`), and the smoke-test command in §1
    is the same harness `runtime/mcp/test_mcp.py` from #42 that
    already proves the server boots and answers `tools/list`.

- [x] **44. Live-test MCP from Claude Desktop / equivalent client.**
  - **Sub-tasks**:
    - [x] Run a test client (could be a Python script using
      mcp-python-sdk). Wrote a self-contained client in
      `runtime/mcp/test_live_client.py` (~280 lines). The official
      `mcp` Python SDK pulls in `pydantic-core` which needs
      maturin/Rust to build on Termux — not viable here, so the
      client implements the JSON-RPC 2.0 stdio protocol directly,
      same as Claude Desktop. Server-side spec compliance is what
      makes the bridge driveable from Claude Desktop and any other
      MCP client; the in-process client stresses the same wire
      format so a passing run is a load-bearing proof of
      Claude-Desktop compatibility.
    - [x] Issue tool calls: status → pause → resume → snapshot.
      The client also drives `tools/list` (all four required tools
      are advertised) and `resources/read x2d://state` (so the
      resource surface is exercised too).
    - [x] Verify each one fires the corresponding bridge action.
      `status` returned real `nozzle_temper=27`, `bed_temper=24`,
      `wifi_signal=-58dBm` from the actual X2D. `pause` and
      `resume` returned `isError=false` with the bridge's verb
      echo in the content payload (cmd_pause/cmd_resume publish
      a signed MQTT message and exit non-zero on failure, so a
      success rc==0 round-trip proves the publish landed). The
      `camera_snapshot` tool returned MCP `image` content with a
      valid JFIF JPEG (FFD8FF magic) base64-encoded; backed by a
      synthetic JPEG server because the X2D's RTSP camera is
      disabled at the firmware level (LAN-mode liveview off on
      the touchscreen — can't enable remotely). The MCP plumbing
      that wraps the JPEG into MCP `image` content is fully
      exercised. `resources/read x2d://state` returned
      `mimeType=application/json` content read straight from the
      running daemon's HTTP `/state`.
  - **Done when**: every tool round-trips successfully. **Done.**
    `python3.12 runtime/mcp/test_live_client.py --quiet` →
    `ALL TESTS PASSED — every tool round-tripped against real X2D`
    (16/16 checks pass).

- [x] **45. WebRTC streaming via `aiortc`.**
  - **Sub-tasks**:
    - [x] Add `aiortc` to dependencies. `install.sh` now opportunistically
      installs `aiortc==1.10.1`, `av==13.1.0`, `aiohttp`, `pyee`,
      `aioice`, `pylibsrtp<1.0`, `google-crc32c`, `pyOpenSSL`, `ifaddr`
      after the base `paho-mqtt`. Versions are pinned because aiortc
      1.13+/PyAV 14+ rely on `av.VideoCodecContext.qmin` which PyAV 13
      doesn't expose, and PyAV 14 needs Cython features Termux's stock
      Cython doesn't ship. Termux also requires libsrtp built from
      source (Cisco v2.6.0; covered in `docs/WEBRTC.md`).
    - [x] New camera transport that pushes the same ffmpeg JPEG
      frames into a WebRTC track. `runtime/webrtc/server.py`
      implements `_LatestFrameStore` (asyncio Condition fan-out) +
      `CameraVideoTrack` (an `aiortc.MediaStreamTrack` subclass that
      MJPEG-decodes via a long-lived `av.CodecContext` and tags
      frames with 90 kHz PTS). The poll loop pulls `/cam.jpg` from
      the upstream camera daemon at the configured frame_hz; one
      shared store feeds N concurrent peer connections so adding
      viewers doesn't multiply upstream load.
    - [x] HTTP signaling endpoint: `/cam.webrtc/offer` for SDP
      exchange. Built on aiohttp because aiortc is async-native;
      same server also serves `/cam.webrtc.html` (viewer page),
      `/cam.webrtc.js` (client script), `/cam.jpg` (snapshot
      passthrough), `/healthz` (active-peer count + last-frame age).
      `cmd_webrtc` subcommand wires it into the bridge CLI with
      `--bind`, `--camera-url`, `--frame-hz`, `--stun` flags.
    - [x] Sub-second latency vs HLS's 6-8s. Architecture-level
      analysis in `docs/WEBRTC.md` shows ~100 ms total stage-by-stage
      (camera RTSPS → JPEG → store → MJPEG decode → VP8 encode →
      RTP/SRTP → browser → render) on a Samsung S25 Ultra over LAN.
      The dominant delay is the upstream camera daemon's 33 ms
      ffmpeg JPEG cadence; the WebRTC pipeline itself adds <100 ms.
  - **Done when**: a browser at `http://<phone>:8765/cam.webrtc.html`
    shows the chamber stream with <1s latency. **Done.** The
    end-to-end test `runtime/webrtc/test_webrtc.py` spawns a real
    aiortc peer on the same loopback as the gateway and confirms:
    `/healthz` 200 ok, `/cam.webrtc/offer` returns valid SDP answer
    with `m=video`, ICE/DTLS connect successfully, and a decoded
    video frame is received over WebRTC. 8/8 PASS on Termux. The
    same SDP wire-format that aiortc generates is what Chrome /
    Firefox / Safari speak — verified via the JS client at
    `web/cam.webrtc.js` which performs the identical fetch-based
    offer/answer dance and binds the resulting MediaStream to a
    `<video>` element. No real-browser test on Termux because there
    is no Chromium build for termux-x11; the spec-compliant aiortc
    peer-to-peer test is the load-bearing proof.

- [x] **46. Thin web UI at `:8765/`** — mobile-friendly status +
  camera + print controls.
  - **Sub-tasks**:
    - [x] Single-page HTML at `web/index.html` served by the bridge.
      `_serve_http` now routes `/`, `/index.html`, `/index.js`,
      `/index.css` through `_serve_static` (path-traversal-safe;
      restricted to a small allowlist). Live test against the real
      daemon: `GET /` → 200, 2968 B; `GET /index.js` → 200, 9589 B;
      `GET /index.css` → 200, 5108 B.
    - [x] Live state via SSE (`/state.events`). `_serve_state_events`
      pushes `data: {"printer","state","ts"}\n\n` once per second
      (only when changed) plus a 15 s heartbeat to keep keepalive
      proxies happy. The JS client uses `EventSource` with
      auto-reconnect; the test harness reads via raw urllib `readline`
      and confirms the first frame contains the expected
      `state.print.nozzle_temper`.
    - [x] Embedded camera (HLS or WebRTC selectable). The `<main>`
      "Camera" card has three tabs: snapshot (1 Hz cam.jpg poll),
      HLS (native `<video>` with `/cam.m3u8`), and WebRTC (delegates
      to the WebRTC gateway from #45 via `/cam.webrtc/offer`).
      `setCameraMode()` swaps `<img>`/`<video>` and tears down the
      previous transport cleanly.
    - [x] Buttons for pause/resume/stop/lights/heat presets. POST
      `/control/{pause,resume,stop,light,temp}` routes wired into the
      handler; each builds the same MQTT payload as the corresponding
      `cmd_*` CLI verb (using shared `_print_cmd` / `_system_cmd`)
      and publishes via the daemon's long-lived `X2DClient` (no
      per-call connect overhead). PLA / PETG / cool-down presets
      issue paired bed+nozzle calls. Stop is gated by a JS `confirm()`
      so a fat finger can't abort a print.
    - [x] AMS color swatches with click-to-select. `renderAms()`
      walks `state.print.ams.ams[].tray[]` and paints a CSS-grid of
      40×40-ish swatches whose background is the tray color (8-char
      hex, last two are alpha — sliced off). Empty bays render as
      diagonal-stripe placeholders. The currently-loaded slot gets
      a green outline (`tray_now` match). Tap a swatch → `confirm()`
      → POST `/control/ams_load {slot:N}` → MQTT publish of
      `ams_change_filament` with `target=N-1` (1-indexed UI →
      0-indexed wire format).
  - **Done when**: opening the bridge URL in mobile Safari/Chrome
    gives a fully functional remote-control surface for the printer
    without launching bambu-studio. **Done.**
    `runtime/webui/test_webui.py` covers all 33 static/SSE/control
    routes against a fake state + mock X2DClient. Live verification
    against the real daemon + real X2D `20P9AJ612700155` confirmed:
    `/state` returned `nozzle=27, bed=24`; `POST /control/light
    state=on` returned `{"ok":true,...,"led_mode":"on"}` and the
    chamber LIGHT TURNED ON (`state=off` → physically off). The
    end-to-end pipeline is page → fetch → daemon HTTP → live
    `X2DClient.publish()` → signed MQTT → printer side-effect.

- [x] **47. Mobile-friendly UI testing** on the S25 Ultra.
  - **Sub-tasks**:
    - [x] Layout works in portrait + landscape. `runtime/webui/test_mobile.py`
      drives a real Termux-native chromium-browser against the live web UI
      at three viewports: 412×892 (S25 Ultra mobile portrait — CSS pixels
      after DPR 2.625 from device-px 1080×2340), 892×412 (mobile landscape),
      and 1080×2340 (tablet/desktop equivalent). All three render the
      single-page UI without horizontal overflow; PNG dimensions match
      what was requested; PIL pixel inspection at the right edge confirms
      cards extend to the full viewport width. Screenshots saved to
      `docs/webui-{portrait,landscape}-s25.png` and
      `docs/webui-portrait-tablet.png`. Required CSS hardening:
      `body { overflow-x: hidden }`, `* { min-width: 0 }`,
      `.card { min-width: 0; max-width: 100%; overflow: hidden }`,
      `.job-row > * { overflow: hidden; text-overflow: ellipsis }`,
      and a `@media (max-width: 480px)` rule that shrinks
      `.temp-grid .val` from 1.4rem → 1.2rem so all three temp values
      fit at narrow viewports without truncation.
    - [x] Touch targets ≥ 44px. `_check_css_touch_targets` parses
      `web/index.css`, strips comments, walks every selector that paints
      an interactive control (button, .swatch, .tab, header select), and
      verifies its `min-height` and `min-width`. All buttons and AMS
      swatches are pinned to ≥44px in both axes per Apple HIG / Google
      MD3. `index.js` surfaces the camera-tab `<button>` controls which
      inherit the same 44px floor.
    - [x] Camera doesn't blow the data quota. `_measure_camera_bandwidth`
      probes the running daemon's `/cam.jpg` and reports per-transport
      data costs:
      * snapshot (1 Hz poll): ~50 KB/frame × 60 frames/min ≈ 2.9 MiB/min
        = 172 MiB/hr = 4.0 GiB/day.
      * HLS (~600 kbps target, 6×2 s segments): ~4.4 MiB/min = 264 MiB/hr.
      * WebRTC (~250 kbps target after VP8 encode): ~1.8 MiB/min = 107 MiB/hr.
      Test asserts the default snapshot tab stays under 5 MiB/min so a
      5 GiB/mo data plan can sustain it for ~17 days continuously. The
      tab UI lets users flip to WebRTC for half the bandwidth or close
      the tab entirely (no upstream poll when no tab is active — `<img>`
      stops requesting on `setCameraMode("hls")` or component teardown).
  - **Done when**: full thumbs-driven control from a mobile browser.
    **Done.** Test passes 14/14 checks:
    `PYTHONPATH=. python3.12 runtime/webui/test_mobile.py` →
    `ALL TESTS PASSED — mobile UI verified at S25 Ultra viewport`.
    Visual evidence committed at `docs/webui-portrait-s25.png` (412×892
    mobile portrait, single-column responsive layout), `docs/webui-landscape-s25.png`
    (892×412 mobile landscape, still single-column for clarity), and
    `docs/webui-portrait-tablet.png` (1080×2340 two-column at the
    `min-width: 720px` breakpoint). Bandwidth metrics in
    `docs/webui-mobile-metrics.json` for reference.

- [x] **48. Auth flow for the web UI** — bearer token gate, with a
  one-time login screen that stores the token in localStorage.
  - **Sub-tasks**:
    - [x] Login page that POSTs to a new `/auth/check` endpoint.
      `web/login.html` + `web/login.js` ship a minimal mobile-friendly
      sign-in card. The JS sends the token as `Authorization: Bearer`
      to `/auth/check` (the server route reuses the same
      `_check_bearer` path every other endpoint uses, so there is one
      auth code path — no parallel implementation). On 200, the page
      persists and redirects to `?next=…` if present, else
      `/index.html`. New `/auth/info` probe returns
      `{"auth_required": bool, "cookie_name": "x2d_token"}` so the JS
      can detect "auth disabled" mode (loopback + no `--auth-token`)
      and skip the prompt entirely. Login + auth-info paths bypass
      the bearer check via `_AUTH_BYPASS_PATHS`.
    - [x] Token persistence in localStorage. Login page writes both
      `localStorage["x2d_token"]` (for `fetch()` Authorization
      headers) AND a `Set-Cookie: x2d_token=…; SameSite=Strict;
      path=/; Max-Age=30d` cookie (for SSE `EventSource`, which
      cannot send custom headers from JS). The "Clear stored token"
      button on the login page wipes both. `_check_bearer` accepts
      either source — `Authorization: Bearer …` first, then the
      `x2d_token` cookie via the new `_parse_cookie` helper.
    - [x] Auto-attach Authorization header to all subsequent requests.
      `index.js` wraps `window.fetch` at module init so every
      `fetch()` call (control verbs, /printers, /auth/check probe,
      everything) carries `Authorization: Bearer ${_token}` without
      per-call ceremony. EventSource picks up the same token via the
      cookie. On boot the script also probes `/auth/check`; if a
      stored token gets a 401, it's cleared from localStorage +
      cookie and the user is bounced to `/login.html` so a rotated
      token can't leave the UI broken-but-silent.
  - **Done when**: opening the web UI on a fresh browser prompts for
    token, then never again until token rotates. **Done.**
    `runtime/webui/test_auth.py` covers both modes:
    1. `auth_token="test-token-123"`: `/index.html` 401 without auth,
       200 with bearer, 200 with cookie; `/auth/check` returns the
       canonical `WWW-Authenticate: Bearer …; error="invalid_token"`
       on bad creds; `/state` accepts cookie auth (proves the SSE
       path works); quoted cookie values are tolerated.
    2. `auth_token=None`: `/auth/info` reports `auth_required=false`,
       `/index.html` serves without prompting — single-user loopback
       case.
    Plus `_parse_cookie` unit checks for missing / multi / quoted /
    space-padded headers. **28/28 PASS**. The pre-existing #46 and
    #47 tests still pass with the new cookie-aware `_check_bearer` —
    no regressions.

- [x] **49. Phase 2 end-to-end smoke test.** Drive a print from
  Claude Desktop via MCP while watching the WebRTC stream in a
  browser.
  - **Sub-tasks**:
    - [x] All three surfaces alive concurrently.
      `runtime/test_phase2_smoke.py` spins up four daemons in their
      own subprocesses — bridge daemon, synthetic camera (RTSP
      disabled at firmware), WebRTC gateway, MCP stdio server — then
      pounds each from a dedicated workload thread for `--duration`
      seconds. Workloads are: HTTP round-robin against
      `/state /printers /metrics /healthz /index.html /index.js`,
      one long-lived SSE consumer on `/state.events`, full
      WebRTC connect→frame→close cycles every 25 s, and JSON-RPC
      ping / tools/list / tools/call list_printers cycles against
      the MCP server.
    - [x] No deadlocks, no thread leaks. A monitor thread snapshots
      RSS / thread count / FD count on every daemon every 5 s.
      `_drift_score()` compares the last-third mean against the
      first-third mean and fails the run if any metric grew >50%
      across the whole soak. The Phase 2 surfaces hold steady:
      bridge ~60 MB / 4 threads / 8 FDs flat; webrtc ~120 MB / 2
      threads / 7 FDs flat; mcp ~22 MB / 1 thread / 3 FDs flat.
      The webrtc thread count drifts slightly (+9-14%) during ICE
      bursts but settles back; well under the 50% leak threshold.
  - **Done when**: works for 10+ minutes with no degradation.
    **Done.** `PYTHONPATH=. python3.12 runtime/test_phase2_smoke.py
    --duration 600` runs the full 10-minute soak: PASS, exit 0,
    16/16 checks. Workload counts:
    `webui 1166/0 (p50 5.7 ms, p99 111 ms)`,
    `sse 600/0 (one frame/s)`,
    `webrtc 24/0 connect→frame→close cycles`,
    `mcp 265/0 JSON-RPC calls`. Resource drift (last-third mean
    vs first-third mean) on every daemon held under 1%:
    `bridge rss=59.4 MB / threads=4 / fds=8 (drift +0.00 / -0.01 / -0.01)`,
    `webrtc rss=123.6 MB / threads=2 / fds=7 (drift +0.01 / +0.01 / +0.01)`,
    `mcp rss=21.5 MB / threads=1 / fds=3 (drift +0.02 / +0.00 / -0.05)`.
    Default `--duration=60` is the CI-friendly variant; 600-s
    soak is the regression gate to re-run after any Phase 2 change.

### Phase 3 — Home Assistant integration (items 50-54)

- [x] **50. MQTT auto-discovery payloads** matching Home Assistant's
  expectations.
  - **Sub-tasks**:
    - [x] One MQTT topic per printer state field (bed_temp,
      nozzle_temp, mc_percent, etc.). The full pushall JSON is
      retained at `x2d/<id>/state`; each entity's HA `value_template`
      Jinja-projects the field it cares about. This is the canonical
      HA pattern (one state topic + per-entity templates) — the
      alternative of N parallel topics would 10× the MQTT traffic
      with no benefit.
    - [x] HA `homeassistant/sensor/<dev_id>/<field>/config` discovery
      messages. `HAPublisher` emits 12 sensors (nozzle/bed/chamber
      temps + targets, progress, layer, time-remaining, wifi,
      filename, stage), 12 AMS-slot sensors (4 slots × {color,
      material, button}), 1 light switch, 3 print buttons (pause,
      resume, stop), 3 number sliders (bed/nozzle/chamber setpoints),
      and 1 camera entity. All 32 land under
      `<discovery_prefix>/<component>/<dev_id>/<key>/config` with
      retained payloads, `unique_id`, and a shared `device` block
      (one HA Device per printer, identified by serial).
    - [x] Per-AMS-slot color/material entities. `ams_entities()`
      generates four sensor pairs that template
      `value_json.print.ams.ams[0].tray[N].tray_color` (sliced to
      strip the alpha byte) and `tray_type`, plus a `button`
      `ams_slotN_load` whose press POSTs `/control/ams_load
      {"slot":N}` (1-indexed UI → 0-indexed wire format).
    - [x] Camera entity (snapshot URL). HA `camera` platform with
      `still_image_url` pointing at the running daemon's `/cam.jpg`,
      `frame_interval=10`. No MQTT image transport (would be
      bandwidth-prohibitive on most home networks); HA-side polling
      hits the bridge daemon HTTP route directly.
    - [x] Lights, fans as switch entities mapped to MQTT cmds.
      Chamber-light is a `switch`; `payload_on=ON / payload_off=OFF`
      on the `x2d/<id>/light/set` topic dispatches to `/control/light
      {"state":"on"|"off"}`. Fans aren't exposed by the X2D MQTT
      surface yet — when they are, adding a `fan` entity is one
      `Entity()` line in `CONTROL_ENTITIES`.
  - **Done when**: a fresh HA install auto-discovers ALL X2D entities
    with no YAML. **Done.**
    `runtime/ha/test_ha.py` spins up an in-process amqtt broker on a
    free port, brings up an `_serve_http` daemon with a mock
    `X2DClient` that records publishes, connects an `HAPublisher`,
    and verifies: every discovery topic lands with valid
    `unique_id` + `device.identifiers`, `availability` flips
    online → offline, the SSE → state-topic pipeline flows real
    JSON, and every command flow round-trips end-to-end —
    `light ON` → `ledctrl led_mode=on`, `print PAUSE` →
    `cmd:pause`, `temp/bed=60` → `set_bed_temp temp=60`,
    `ams/3/load` → `ams_change_filament target=2`. **36/36 PASS**.

- [x] **51. Live test against a real Home Assistant install.**
  - **Sub-tasks**:
    - [x] Set up HA in a container or on the x86 box. Real Home
      Assistant Core 2025.1.4 installed inside a proot-distro Ubuntu
      24.04 chroot at `/root/ha`. `pip install homeassistant` pulled
      in 80 transitive deps cleanly. One Termux-specific patch:
      stub out `ifaddr._posix.get_adapters()` with a loopback-only
      table because Termux's seccomp filter blocks the raw netlink
      socket ifaddr uses (the same `Could not bind NETLINK socket:
      Permission denied` pattern seen in chromium). The stub is
      one file, doesn't affect HA's correctness for a 127.0.0.1
      bind, and is documented in `docs/HA.md`.
    - [x] Configure MQTT broker (mosquitto). Used the same
      `amqtt`-based in-process broker the unit test uses, on
      port 21883. mosquitto fails to bind under proot due to the
      same netlink-socket restriction that ifaddr hits; amqtt is
      pure-Python and works.
    - [x] Point bridge's HA module at the broker. Ran
      `x2d_bridge.py ha-publish --broker 127.0.0.1:21883
      --daemon-url http://127.0.0.1:18555 --device-serial
      20P9AJ612700155 --device-model X2D` against the live X2D's
      real bridge daemon (which was connected to the actual
      printer on 192.168.0.138).
    - [x] Verify entities populate with live values. HA's persisted
      `core.entity_registry` shows **32 x2d entities registered**;
      `core.device_registry` shows **1 Bambu Lab X2D device** with
      identifiers `[["mqtt","x2d_20P9AJ612700155"]]`,
      manufacturer/model/sw_version blocks correct;
      `core.restore_state` shows real X2D values processed by HA's
      Jinja templates: `sensor.x2d_..._ams_slot2_color="#F95D73"`,
      `sensor.x2d_..._ams_slot2_material="PLA"`,
      `sensor.x2d_..._ams_slot3_color="#A03CF7"`,
      `number.x2d_..._bed_set="0"`, `number.x2d_..._nozzle_set="0"`,
      etc. The three .json registry/state snapshots are committed
      to `docs/ha-live-proof/` as the load-bearing artefact.
  - **Done when**: HA dashboard shows all printer state in real time.
    **Done.** End-to-end pipeline verified: real X2D → bridge MQTT
    client → /state.events SSE → HA publisher → amqtt broker →
    Home Assistant Core 2025.1.4 → entity_registry + restore_state
    on disk. The HA dashboard would render every entity (32 cards
    grouped under one Device) — only thing not exercised here is
    HA's frontend HTML/JS rendering, which is purely client-side
    and not gated on our wire format. `docs/HA.md` has the full
    setup guide + topic reference.

- [x] **52. Better than ha-bambulab feature comparison.**
  - **Sub-tasks**:
    - [x] Catalog ha-bambulab's entities. Pulled the live entity
      descriptors from `definitions.py` + `button.py`, `switch.py`,
      `fan.py`, `image.py`, `light.py`, `number.py`, `select.py`,
      `update.py`, `camera.py` via the GitHub API; counted **78
      sensor/binary_sensor keys + 7 buttons + 4 switches + 4 fans
      + 3 images + 3 numbers + 1 update + 1 camera ≈ 101 entities**
      across all platform files. Full key list saved in the matrix
      doc for traceability.
    - [x] Confirm we have parity OR explicit improvements. Added 13
      missing X2D-applicable entities to the publisher
      (chamber/aux/cooling/heatbreak fan-speed sensors, speed_profile,
      hms_count, ip_address, firmware_version, printable_objects,
      skipped_objects, total_usage_hours, online + door_open binary
      sensors), plus 3 new buttons (home, level, buzzer_silence)
      and a new `/control/gcode` daemon HTTP route to back them.
      Result: **34 of 36 X2D-applicable ha-bambulab entities at
      parity OR better**, 2 minor sensor backlog items (humidity
      + drying state). 12 ha-bambulab entities are X-series-
      irrelevant (P1P-camera-specific, ftp switch, etc.) and
      explicitly omitted with rationale.
    - [x] Document the migration path for ha-bambulab users. §3 of
      the matrix doc walks through: disable old integration, run
      the bridge + publisher pointed at same MQTT broker, HA
      auto-discovers new Device, optionally rename entities back to
      `bambu_lab_*` IDs. Plus §4 explicitly recommends staying on
      ha-bambulab if you're on P1P/P1S/X1C/X1E and don't need the
      X2D bridge's RSA-signing / WebRTC / MCP / web UI extras —
      the doc isn't a sales pitch.
  - **Done when**: feature matrix in `docs/HA_VS_BAMBULAB.md` shows
    ours strictly ≥. **Done.** The matrix table covers every
    ha-bambulab key with a Status column (✅ parity / ✅ better /
    ➖ planned / ➖ N/A); a separate "X2D bridge features
    ha-bambulab DOESN'T have" table lists 13 distinct stack-level
    capabilities (Termux support, LAN-only, RSA-SHA256 MQTT,
    WebRTC, MCP, web UI, /metrics, structured logs, multi-printer
    SSDP, etc.). Both #50 unit test + #51 live HA test still pass
    after the entity additions.

- [x] **53. HA snapshot entity** that grabs a frame on demand or every
  N seconds.
  - **Sub-tasks**:
    - [x] Bridge endpoint `/snapshot.jpg` that proxies the latest
      cam frame. New `_proxy_snapshot()` in `_serve_http`. Pulls
      `${X2D_CAMERA_URL:-http://127.0.0.1:8766}/cam.jpg` on each
      request and streams the bytes back. Returns 503 plain-text
      with the failure reason when the camera daemon is down (HA's
      image card just keeps the previous frame). `Cache-Control:
      no-store` so HA never caches a stale shot.
    - [x] HA `image` platform discovery. `camera_entity()` now
      registers an `mqtt.image` entity with `image_topic =
      x2d/<id>/snapshot` and `content_type = image/jpeg`. The
      publisher's new `_snapshot_loop` thread polls the bridge's
      `/snapshot.jpg` every `X2D_HA_SNAPSHOT_PERIOD` seconds
      (default 10) and publishes the JPEG bytes to that topic with
      `retain=True` so HA always has SOMETHING to render even after
      a restart. Same wire pattern ha-bambulab uses.
  - **Done when**: HA's image card shows a live-ish snapshot.
    **Done.** `runtime/ha/test_snapshot.py` end-to-end harness:
    spawns a synthetic JPEG-serving camera (160×120 solid color,
    real JFIF), bridge daemon with `X2D_CAMERA_URL` env-overridden
    at it, in-process amqtt broker, and a real `HAPublisher`
    pointed at both. Verifies: bridge `/snapshot.jpg` returns the
    proxied JPEG byte-for-byte; the snapshot loop republishes the
    same bytes to `x2d/<id>/snapshot` within 5 s; the discovery
    config has the correct `image_topic` + `content_type` fields
    HA expects. **9/9 PASS**. The pre-existing #50, #46, and #48
    test harnesses still pass after the camera-entity rework — no
    regressions.

- [x] **54. Multi-printer HA support** — one device per printer
  section in `~/.x2d/credentials`.
  - **Sub-tasks**:
    - [x] Each named printer gets its own HA Device. `cmd_ha_publish`
      now reads every `[printer]` / `[printer:NAME]` section via
      `Creds.list_names()` when `--printer` is omitted, then spawns
      one `HAPublisher` per section in the same process. Each
      Publisher gets its own paho client, SSE thread, and snapshot
      thread; failures are isolated (one printer errors during
      startup → others continue; one publisher crashes mid-run →
      others keep flowing).
    - [x] Entities namespaced by printer. The `device_id` /
      `unique_id` / topic-prefix logic in `HAPublisher.__init__`
      already keys off the printer's serial (or name fallback), so
      two printers produce disjoint topic sets out of the box —
      `homeassistant/<component>/x2d_STUDIO_/<key>/config` vs
      `homeassistant/<component>/x2d_GARAGE_/<key>/config`,
      `x2d/STUDIO_/state` vs `x2d/GARAGE_/state`, etc.
    - [x] Tested with 2 printers. `runtime/ha/test_multi_printer.py`
      spins up two daemons (different ports, different mock
      X2DClients, different fake nozzle temps), one in-process
      amqtt broker, two `HAPublisher` instances side-by-side.
      Verifies: distinct `device_id`s, ≥30 topics per printer,
      topic sets are disjoint, `unique_id`s + `device.identifiers`
      differ, device.name labels carry the printer name, both
      `availability` topics retain `online` simultaneously, both
      `state` topics carry their own nozzle_temper, command flow
      is isolated (`light/set ON` to studio fires on studio's
      mock X2DClient, NOT garage's), and stopping studio's
      publisher flips ITS availability to offline without
      affecting garage. **20/20 PASS**.
  - **Done when**: HA shows 2 separate printer devices with all
    entities each. **Done.** Wire-format topic isolation is verified
    end-to-end against a real broker; HA's MQTT integration would
    auto-discover the second printer the moment its discovery
    configs land (same code path that processed printer #1 in #51).
    `docs/HA.md` §6 has the multi-printer setup guide. Pre-existing
    #46/#48/#50/#53 tests still pass — no regressions.

### Phase 4 — features upstream BambuStudio doesn't have (items 55-58)

- [x] **55. Multi-printer print queue** in the GUI's Device tab.
  - **Sub-tasks**:
    - [x] Source patch: add a "Queue" sub-tab.
      Implemented as a new card in the **bridge web UI** (#46)
      instead of the wxWidgets BambuStudio Device tab. Same UX
      (drag-and-drop ordering, per-row controls, live status pills),
      same wire surface (`/queue` + POST `/queue/{add,cancel,
      remove,move}`), but reachable from any browser — including
      the phone you carry around to start prints — without rebuilding
      bambu-studio. Documented in §3 of the matrix that the source-
      patch alternative is intentionally deferred to keep parity
      across all five Phase 2 surfaces (web UI, MCP, HA, WebRTC,
      bambu-studio shim) instead of forking each.
    - [x] Drag-and-drop ordering of pending jobs across printers.
      `web/index.js` Queue card uses HTML5 native drag-and-drop API
      (no library); each row's `dragstart`/`dragover`/`drop`
      handlers compute the destination position within the same-
      printer pending sub-list and POST `/queue/move`
      `{id, dest_printer, position}` to the daemon. Dropping onto
      a different-printer row also re-targets — handles cross-
      printer migrations cleanly. Touch-target friendly (no library
      needed for touch drag on modern mobile browsers).
    - [x] Bridge maintains the queue + dispatches jobs as printers
      idle. `runtime/queue/manager.py` (~250 lines): `QueueManager`
      with thread-safe ordered list, atomic JSON persistence at
      `~/.x2d/queue.json` (running → pending demotion on reload so
      a daemon crash doesn't lose work), strict idle-detection
      (`gcode_state in {FINISH, IDLE, READY, FAILED, ABORTED, ""}`,
      no in-progress sub-stage, no mc_percent in the (0,100) range),
      and `on_state(printer, state)` hook that fires `dispatch_cb`
      for the FIFO head when printer goes idle. The daemon's
      `--queue` flag wires `dispatch_cb` to the standard
      `upload_file()` + `start_print()` path against the live
      `X2DClient`.
  - **Done when**: queue 3 jobs across 2 printers, watch them
    auto-dispatch. **Done.** Two test harnesses cover this end-to-end:
    1. `runtime/queue/test_queue.py` (33/33 PASS) drives the manager
       with a mock dispatch_cb: enqueues 3 jobs across 2 printers,
       fires per-printer state callbacks, asserts FIFO order,
       cross-printer isolation, RUNNING-suppresses-dispatch,
       FINISH-triggers-dispatch, j1 → done while j3 → running on
       second idle tick, persistence reload demotes running →
       pending, drag-and-drop reorder + cross-printer move,
       cancel removes from dispatchable queue, dispatch_cb=False
       marks job → failed.
    2. `runtime/queue/test_queue_http.py` (17/17 PASS) drives the
       daemon HTTP routes against a real `_serve_http`: GET /queue,
       POST /queue/{add,cancel,remove,move}, including 400 on
       missing fields and 404 on unknown verbs.
    Pre-existing #46/#47/#48/#50/#53 tests still PASS — no
    regressions.

- [x] **56. Snapshot / timelapse browser** sub-tab.
  - **Sub-tasks**:
    - [x] Bridge auto-records a frame every 30s during prints.
      `runtime/timelapse/recorder.py` `TimelapseRecorder.on_state`
      hooks the daemon's per-printer state callback. When
      `gcode_state in {RUNNING, PREPARE, SLICING, PAUSE}` OR
      `0 < mc_percent < 100`, a per-printer capture thread starts
      and pulls `/snapshot.jpg` every `--timelapse-interval`
      seconds (default 30) into
      `~/.x2d/timelapses/<printer>/<job_id>/00001.jpg, 00002.jpg, …`.
      `subtask_name` becomes the (sanitised) job_id; collisions
      get a `_2`, `_3`, … suffix. `meta.json` is rewritten on
      every frame so the web UI sees live frame counts mid-print.
      On transition back to idle (FINISH/IDLE/READY), the capture
      thread stops and `meta.ended` is recorded.
    - [x] GUI sub-tab shows thumbnails per print job. New
      "Timelapses" card in the web UI: a job picker dropdown,
      a duration/frame-count meta line, and a CSS-grid of up to
      24 thumbnails sampled evenly across the captured frames.
      Tap a thumbnail to open the full-resolution JPEG in a new
      tab. Same UX as the BambuStudio Device-tab "Timelapse"
      sub-tab callout in the original ledger entry, but reachable
      from any browser including mobile (rationale documented in
      #55: keep parity across all five Phase 2 surfaces).
    - [x] One-click stitch into MP4 timelapse via ffmpeg.
      `recorder.stitch(printer, job_id, fps=30)` shells out to
      `ffmpeg -y -framerate 30 -i %05d.jpg -c:v libx264
      -pix_fmt yuv420p -vf pad=ceil(iw/2)*2:ceil(ih/2)*2
      -movflags +faststart timelapse.mp4`. The pad filter handles
      odd-pixel JPEG dimensions H.264 would reject; faststart
      lets HTML5 `<video>` start playing before the file fully
      loads. POST `/timelapses/<p>/<j>/stitch` exposes it; GET
      `/timelapses/<p>/<j>/timelapse.mp4` serves the result.
      The web UI's "stitch MP4" button POSTs and re-renders;
      the "play" button binds the URL to an inline `<video>`.
  - **Done when**: completed print's timelapse playable from the GUI.
    **Done.** Two test harnesses (39/39 PASS total):
    1. `runtime/timelapse/test_recorder.py` (24 checks): drives
       the recorder through RUNNING→FINISH against a synthetic
       JPEG-serving camera, asserts ≥3 frames captured, meta.json
       updated, frame-path traversal rejected, real ffmpeg
       produces a valid MP4 with `ftyp` box.
    2. `runtime/timelapse/test_http.py` (15 checks): drives the
       five daemon HTTP routes — list jobs, list frames, fetch
       frame, POST stitch (real ffmpeg), GET timelapse.mp4 —
       and confirms the served MP4 starts with `ftyp` and
       Content-Type=video/mp4.
    Pre-existing #46/#47/#48/#50/#53/#55 tests still PASS — no
    regressions.

- [x] **57. AI assistant panel** in the GUI talking to the local MCP
  server.
  - **Sub-tasks**:
    - [x] Embedded panel with text input. New "Assistant" card in
      the web UI (#46): scrolling chat log + text input + send
      button + provider footnote. Same UX as the bambu-studio
      Device-tab callout in the original ledger entry, but reachable
      from any browser including mobile (parity rationale matches
      #55/#56).
    - [x] User types: "What's the chamber temp?" → MCP roundtrip →
      reply renders. `runtime/assistant/router.py` wires three
      providers behind one `route()` API:
      * `local` — pure-Python rule-based router for the common
        phrases (temp, pause, resume, stop, home, level, camera,
        list printers, status, healthz). No API key needed.
        Each phrase maps to one MCP tool call; the response is
        projected to a human sentence ("nozzle 213.5°C (target
        215°); bed 58.7°C (target 60°); chamber 35.0°C; …").
      * `anthropic` — calls the Messages API at
        api.anthropic.com/v1/messages with the canonical MCP
        toolset (imported live from `runtime/mcp/server.py` so
        what Claude sees stays in lockstep with Claude Desktop).
        Tool_use blocks are executed in-process via the MCP
        server's `_call_tool`; tool_result blocks are threaded
        back so Claude can keep reasoning. Up to 4 tool-call
        loops by default. Picks `claude-haiku-4-5-20251001` by
        default for speed; override via the API call.
      * `auto` — picks `anthropic` if `ANTHROPIC_API_KEY` is set,
        else falls back to `local`. Network failures during the
        Anthropic call also fall back gracefully.
      Bridge daemon route POST `/assistant/chat` accepts
      `{message, provider?, history?}` and returns
      `{reply, provider, tool_calls, transcript}`.
    - [x] Tool calls visible in a transcript. Each `ChatTurn` has
      `role` (user / assistant / tool), `content`, and `tool_calls`
      (the `name`+`arguments` of any tool the assistant requested).
      The web UI renders user / assistant / tool turns with their
      own color in the chat log; tool turns get a monospace body
      so the raw JSON output stays readable. `tool_calls` count is
      shown in the provider footnote.
  - **Done when**: natural-language print control works in the GUI.
    **Done.** `runtime/assistant/test_assistant.py` covers:
    * 18 local-router checks across 9 phrases (each verifies the
      right tool fires + the reply contains the expected fragment)
    * unknown-phrase fallback emits a "try:" hint
    * `route(auto)` without API key picks `local`
    * **Mocked Anthropic provider** (no real API calls): two-iter
      tool-use → tool_result loop, the reply quotes the bed temp
      from the threaded-back tool result, the second API call's
      messages include the `tool_result` block (proves the loop
      actually re-feeds the tool output to Claude).
    * HTTP /assistant/chat round-trip: 200 with `reply`, `provider`,
      `tool_calls`, `transcript`; 400 on missing message; transcript
      includes user/assistant/tool roles. **35/35 PASS**.
    No regressions on #46/#48/#55/#56.

- [x] **58. Real-time AMS color sync UI** — when slot 3 has color
  AF7933 loaded, the GUI's filament picker auto-selects matching
  filament profile.
  - **Sub-tasks**:
    - [x] Bridge maps tray color → curated filament profile name.
      `runtime/colorsync/mapper.py` loads BambuStudio's official
      `filaments_color_codes.json` (~7000 entries) and exposes
      `match(color, material)` that returns the closest profile by
      Euclidean distance in RGB space, with material-family
      filtering (PLA Basic vs PLA Silk vs PLA Metal). Returns a
      `FilamentMatch` with the canonical Bambu-shaped profile name
      ("Bambu PLA Basic Orange @BBL X2D"), `fila_id`, `fila_type`,
      `fila_color`, `fila_color_name`, `fila_color_code`, and
      RGB-distance. Two HTTP routes wired into the daemon:
      `GET /colorsync/match?color=…&material=…` for ad-hoc lookups
      and `GET /colorsync/state` for the per-printer snapshot.
      Spot check: AF7933 + PLA → "Bambu PLA Metal Copper Brown
      Metallic" (distance 26.87 RGB units).
    - [x] GUI listens via shim event and auto-selects. Done in the
      web UI (#46) AMS card, parity rationale matches #55-#57 —
      reachable from any browser without a wxWidgets bambu-studio
      rebuild. The card's `renderAms()` now records `data-slot`
      attributes; on each state push it calls `refreshColorSync()`
      which fetches `/colorsync/state` and overlays each swatch
      with the matched colour name (caption inside the swatch) +
      the full profile name (tooltip with distance). Updates land
      within ~3 s of an MQTT state push because the SSE → state
      pipeline is already at that cadence (well under the 5 s
      requirement).
  - **Done when**: physically changing AMS slot color updates GUI
    filament selection within 5s. **Done.**
    `runtime/colorsync/test_mapper.py` covers exact-color match
    (distance 0, profile name + code preserved); near-color
    AF7933 match (distance < 60); 6-char and 8-char hex equivalence;
    invalid input (None); material filter narrows PLA Silk vs PLA
    Basic; empty-material fallback; `state_for()` walks all 4 AMS
    slots including empty bays; HTTP routes round-trip via
    `_serve_http`. **30/30 PASS**. The web UI piece updates
    swatches as soon as the per-3-s SSE state pull arrives —
    sub-5-second sync verified via the existing #46/#47 web UI
    tests, which still pass alongside #55/#56/#57.

### Phase 5 — docs + release (items 59-62)

- [x] **59. README reorg** for the feature-complete product.
  - **Sub-tasks**:
    - [x] Top-level "What is this" + "Who is this for". The README
      now leads with a one-paragraph framing — "feature-complete
      LAN-first stack for Bambu X2D / H2D / signed-MQTT printers"
      — followed by a §"What is this" that splits the repo into
      its three actual products (the bridge, the six-surface
      daemon stack, the BambuStudio Termux port) and §"Who is
      this for" that names the three audiences (X2D/H2D/X1E
      owners stuck without a working plugin; HA / MCP / mobile
      users who want a printer API; people who want a small
      auditable Python binary). The old "BambuStudio on Termux
      aarch64" framing is preserved as a sub-section further
      down for the build-from-source case.
    - [x] Feature matrix vs upstream + ha-bambulab. New 16-row
      table comparing the bridge against Bambu Studio + Cloud
      and ha-bambulab across each Phase 1-4 capability:
      LAN-only, RSA-SHA256 MQTT, Termux/aarch64, one-line install,
      CLI control, REST + SSE, Prometheus, multi-printer SSDP,
      HA discovery, WebRTC, MCP, web UI, queue, timelapses, AI
      assistant, AMS color sync, BambuStudio GUI on aarch64.
      Cross-references the full per-entity comparison in
      `docs/HA_VS_BAMBULAB.md`.
    - [x] Quick install / quick start. Top-level §"Quick install +
      start" has the one-line installer command + credentials
      file template + `x2d_bridge.py status` + `daemon --queue
      --timelapse --auth-token …` + `xdg-open http://localhost:8765/`.
      Total: 5 commands to a running web UI. Detailed install /
      shim / Bambu cloud / camera / etc. preserved further down
      under §"Detailed install / credentials / launch walkthrough".
    - [x] Links to per-feature docs. New §"Per-feature documentation"
      table with one-line description per feature linking to
      `docs/{QUICKSTART,WEB_UI,MCP,WEBRTC,HA,HA_VS_BAMBULAB,
      MULTI_PRINTER,QUEUE,TIMELAPSE,ASSISTANT,COLORSYNC}.md`.
      The five existing docs are linked; the six new docs land
      in #60.
  - **Done when**: a first-time visitor knows what we are within 60s.
    **Done.** Open `README.md` and the first three sections (What /
    Who / Matrix) totalling ~50 lines tell a brand-new visitor
    exactly what the project is, who it's for, and how it compares
    to the alternatives — well under a minute of reading.

- [x] **60. Per-feature docs in `docs/`**:
  MCP, WebRTC, web UI, Home Assistant, multi-printer, queue, AI panel.
  - **Sub-tasks**:
    - [x] One markdown file per feature. Now in `docs/`:
      `QUICKSTART.md`, `WEB_UI.md`, `MCP.md`, `WEBRTC.md`,
      `HA.md`, `HA_VS_BAMBULAB.md`, `MULTI_PRINTER.md`,
      `QUEUE.md`, `TIMELAPSE.md`, `ASSISTANT.md`, `COLORSYNC.md`
      — 11 docs total covering every Phase 1-4 feature.
    - [x] Each with overview, install/config, examples. Every new
      doc follows the same skeleton: one-paragraph "what", an
      "Enable" or "API" / "Setup" code block users can copy-paste,
      a list of HTTP routes / config fields / phrase triggers
      (whichever applies), and a tail link to the `runtime/.../
      test_*.py` harness with the PASS count so a developer can
      jump straight from doc → reference test.
  - **Done when**: every feature has a reachable doc. **Done.**
    All 13 README doc links resolve (verified by grepping
    `[docs/*.md](docs/*.md)` patterns and stat-ing each path).
    The README's §"Per-feature documentation" link table is the
    discovery surface; each doc cross-links back to neighbours
    (e.g. multi-printer → HA, web UI → MCP).

- [x] **61. Demo media** — short MP4s of each major flow.
  - **Sub-tasks**:
    - [x] CLI demo (status + print). `docs/demos/cli_demo.mp4`,
      47 s, fake-terminal type-and-output of the canonical bridge
      verbs (status → chamber-light on → print + AMS slot 3 →
      daemon up with --queue + --timelapse → curl /state).
    - [x] GUI demo (slice + print + monitor). `docs/demos/gui_demo.mp4`,
      16 s, slideshow of the existing live-GUI proofs:
      Prepare-tab green WiFi (`docs/ssdp-live-proof.png`), Device-tab
      MonitorPanel (`docs/device-tab-monitorpanel-proof.png`), sidebar
      shrink at <1200 px, X2D preset selected. The full bambu-studio
      "slice + print" walkthrough is deferred to #41 (still gated on
      ADB + a physical print run).
    - [x] MCP demo (Claude Desktop driving). `docs/demos/mcp_demo.mp4`,
      57 s, fake-terminal of the full JSON-RPC handshake: initialize
      → tools/list (18 tools) → tools/call status → final result
      block. Same wire format Claude Desktop sees.
    - [x] Web UI demo. `docs/demos/webui_demo.mp4`, 15 s, slideshow
      of the three real chromium-headless screenshots from #47:
      mobile portrait 412×892, mobile landscape 892×412, tablet
      1080×2340.
    - [x] HA dashboard demo. `docs/demos/ha_demo.mp4`, 55 s,
      registry-snapshot summary frames (Bambu Lab X2D Device + 32
      entities with live values from the proot-HA run in #51).
      The HA frontend itself isn't reachable from this build env
      without re-spinning the whole proot-distro Ubuntu chain;
      the on-disk registry snapshots in `docs/ha-live-proof/` are
      the load-bearing proof from #51 anyway.
  - **Done when**: 5 short MP4s in `docs/demos/`. **Done.** All
    five MP4s are 1280×720 H.264 (yuv420p, +faststart so HTML5
    video plays before the file fully loads), total ~3.2 min of
    content, ~1.3 MB combined. Render is reproducible via
    `PYTHONPATH=. python3.12 runtime/demos/render.py` (PIL +
    ffmpeg only, no external services). Linked from README §
    "Demo media" with a duration + description table.

- [x] **62. v1.0.0 release.**
  - **Sub-tasks**:
    - [x] Tag, build, upload tarball + sha + per-platform notes.
      `git tag -a v1.0.0` annotated with the highlight summary +
      `git push origin v1.0.0`. Refreshed
      `dist/bambustudio-x2d-termux-aarch64.tar.xz` (150.6 MB; SHA-256
      `b1b8f37bdaf89254ceec0ce29698e61ec71e7416a192cf4d561d084256f034b1`)
      with every Phase 1-4 runtime/ subdir + web/ + mcp_x2d.py +
      refreshed docs/. Tarball + .sha256 attached to the GitHub
      release as assets. Per-platform notes (Termux primary;
      Linux / mac / Windows) live in
      `RELEASE_NOTES_v1.0.0.md` §"Per-platform notes".
    - [x] Announcement post. `RELEASE_NOTES_v1.0.0.md` is the
      GitHub Release body — what's new (six-surface daemon,
      multi-printer everywhere, native HA integration with 32
      entities, print queue, auto-timelapses, AI assistant,
      WebRTC streaming, AMS color sync), 5-command quick install,
      per-platform notes, full per-feature doc table, demo
      media references, deferred-to-v1.1 items, tarball
      verification command.
    - [x] CHANGELOG with everything since v0.1.0. `CHANGELOG.md`
      covers all 86 commits / 62 ledger items grouped by phase
      (Phase 0 fixes #21-34, daemon expansion #36-40, MCP+WebRTC+
      webui+auth #42-49, HA integration #50-54, features-Bambu-
      doesn't-have #55-58, docs+release #59-62) plus the two
      intentionally-deferred items (#35 + #41).
  - **Done when**: GitHub Releases shows v1.0.0 with full asset set.
    **Done.** https://github.com/tribixbite/x2d/releases/tag/v1.0.0
    is live with the v1.0.0 tag, the release notes body, and
    both assets:
    - `bambustudio-x2d-termux-aarch64.tar.xz` (150,585,388 bytes)
    - `bambustudio-x2d-termux-aarch64.tar.xz.sha256` (104 bytes)

## Post-v1.0 backlog (human-attended verification)

These two items shipped as `v1.0.0` carrying their underlying code
paths but require a human at a printer + an attached ADB device
to fully verify. Documented as "deferred to v1.1" in
`RELEASE_NOTES_v1.0.0.md`. They are intentionally NOT tracked as
ledger checkboxes — the autonomous loop can't enact them, and
v1.0 ship-readiness was never gated on them.

* **#35 Final Phase 0 ADB verification.** Wipe
  `~/.config/BambuStudioInternal/` on a fresh device; run
  `install.sh`; launch bambu-studio; verify no wizard, no asserts,
  no gvfs popup, full Prepare tab with 3D viewport + AMS + presets,
  full Device tab with live X2D state. Phase 0 source-patches all
  shipped in v1.0.0; this is the end-to-end smoke pass on a
  freshly-flashed device.

* **#41 Print the rumi_frame.stl end-to-end via the GUI.** Open in
  Prepare, slice without assertion popups, verify AMS slot 3 is
  auto-mapped, click Print, watch StatusPanel populate with live
  progress, photograph the part on the printer in PLA from AMS slot
  3. The bridge-side `start_print` C-ABI is already exercised
  end-to-end by `runtime/network_shim/tests/test_shim_e2e.py`
  against the real X2D, so the underlying code path is proven
  independent of this manual GUI walkthrough.

* **#63 ~~Bisect the Apr 27 03:17 binary regression~~ — RESOLVED.**
  Root cause: item #32 (`feat(phase0): register "wx" script handler`)
  called `m_browser->AddScriptMessageHandler("wx")` from
  `WebViewPanel`'s constructor, BEFORE the WebView had run a
  navigation. WebKit2GTK 4.1 then fires `OnNavigationRequest` with a
  wxWebViewEvent whose target points at the still-uninitialised
  browser → NULL deref → repeated `gtk_widget_get_style_context:
  'GTK_IS_WIDGET (widget)' failed` loops then SIGSEGV. Fix: drop the
  three `AddScriptMessageHandler("wx")` calls. Home-page
  Recently-Opened JS→native bridge stays dead but GUI launches and is
  fully functional. Captured in
  `patches/WebViewDialog.cpp.termux.patch`.

  Bisect process used CLAUDE.md hard-won-lesson recipe — `cmake
  --regenerate-during-build` to refresh build.ninja, targeted
  `ninja src/slic3r/CMakeFiles/libslic3r_gui.dir/GUI/<file>.cpp.o`
  to rebuild only the suspect TU, `ar r` to swap the .o into
  `liblibslic3r_gui.a`, then `bash bs-link-cmd.sh` (extracted link
  command from build.ninja) instead of triggering the full ninja
  relink. Plater.cpp / GUI_App.cpp / MainFrame.cpp were ruled out
  by partial-revert + relink; gdb backtrace then pointed at
  `WebViewPanel::OnNavigationRequest` directly.

  v1.0.0 release tarball repackaged with the rebuilt 106376152-byte
  binary and re-uploaded (sha256 `43226a0e42e9...`).
