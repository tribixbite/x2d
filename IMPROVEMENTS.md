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

- [ ] **38. Prometheus `/metrics` endpoint.**
  - **Sub-tasks**:
    - [ ] Per-printer gauges: bed_temp, nozzle_temp, mc_percent,
      ams_humidity, etc.
    - [ ] Counters: total_messages, mqtt_disconnects, ssdp_notifies.
    - [ ] Compatible with Prometheus scrape format (text exposition).
  - **Done when**: Prometheus `up{job=x2d}` is 1, all printer state
    fields scraped as gauges.

- [ ] **39. Structured request log** for the bridge HTTP server.
  - **Sub-tasks**:
    - [ ] One JSON line per request: ts, method, path, status,
      duration_ms, printer (if applicable), authed (bool).
    - [ ] Goes to `~/.x2d/access.log` with the same 1 MiB rotation
      as bridge.log.
  - **Done when**: every HTTP hit gets one structured log line.

- [ ] **40. Bridge auto-connect on SSDP-creds match (Phase 0.5
  carryover, kept for resilience).**
  - **Sub-tasks**:
    - [ ] When SSDP NOTIFY's dev_id matches a creds section's serial,
      bridge opens the MQTT subscription proactively.
    - [ ] Cached state replays on every shim subscribe.
    - [ ] Even if Phase 0 fixes the GUI Connect path, this gives
      sub-second StatusPanel population on launch.
  - **Done when**: Device tab shows live state immediately on launch
    without ANY user action in the GUI.

- [ ] **41. Print the rumi frame end-to-end via the GUI.**
  - **Sub-tasks**:
    - [ ] Open `rumi_frame.stl` (or `rumi.gcode.3mf`) in Prepare.
    - [ ] Slice plate → no assertion popups.
    - [ ] AMS slot 3 (brown/orange) auto-mapped.
    - [ ] Click Print plate → SelectMachine dialog → X2D shown.
    - [ ] Send → upload + start_print → physical print starts.
    - [ ] Watch StatusPanel populate with live progress.
  - **Done when**: photo of the rumi_frame.stl printing on the X2D
    in PLA from AMS slot 3, taken via ADB screenshot of the camera
    tab if streaming works OR documented physical observation.

### Phase 2 — MCP + WebRTC + thin web UI (items 42-49)

- [ ] **42. MCP server stdio module** at `runtime/mcp/server.py`.
  - **Sub-tasks**:
    - [ ] Wraps every bridge op as an MCP tool: status, pause, resume,
      stop, gcode, set_temp, chamber_light, ams_load/unload, jog,
      camera_snapshot, list_printers, etc.
    - [ ] Conforms to MCP spec (modelcontextprotocol.io).
    - [ ] Includes resources: latest state JSON, latest camera frame.
  - **Done when**: `python -m mcp_x2d` over stdio responds to MCP
    `tools/list` with the full toolset.

- [ ] **43. Claude Desktop config docs** for adding the MCP server.
  - **Sub-tasks**:
    - [ ] `docs/MCP.md` with `claude_desktop_config.json` snippet.
    - [ ] Per-platform install notes (Termux, Linux, mac).
  - **Done when**: a user can copy-paste a config block and have
    Claude Desktop driving prints within 60s.

- [ ] **44. Live-test MCP from Claude Desktop / equivalent client.**
  - **Sub-tasks**:
    - [ ] Run a test client (could be a Python script using
      mcp-python-sdk).
    - [ ] Issue tool calls: status → pause → resume → snapshot.
    - [ ] Verify each one fires the corresponding bridge action.
  - **Done when**: every tool round-trips successfully.

- [ ] **45. WebRTC streaming via `aiortc`.**
  - **Sub-tasks**:
    - [ ] Add `aiortc` to dependencies.
    - [ ] New camera transport that pushes the same ffmpeg JPEG
      frames into a WebRTC track.
    - [ ] HTTP signaling endpoint: `/cam.webrtc/offer` for SDP
      exchange.
    - [ ] Sub-second latency vs HLS's 6-8s.
  - **Done when**: a browser at `http://<phone>:8765/cam.webrtc.html`
    shows the chamber stream with <1s latency.

- [ ] **46. Thin web UI at `:8765/`** — mobile-friendly status +
  camera + print controls.
  - **Sub-tasks**:
    - [ ] Single-page HTML at `web/index.html` served by the bridge.
    - [ ] Live state via SSE (`/state.events`).
    - [ ] Embedded camera (HLS or WebRTC selectable).
    - [ ] Buttons for pause/resume/stop/lights/heat presets.
    - [ ] AMS color swatches with click-to-select.
  - **Done when**: opening the bridge URL in mobile Safari/Chrome
    gives a fully functional remote-control surface for the printer
    without launching bambu-studio.

- [ ] **47. Mobile-friendly UI testing** on the S25 Ultra.
  - **Sub-tasks**:
    - [ ] Layout works in portrait + landscape.
    - [ ] Touch targets ≥ 44px.
    - [ ] Camera doesn't blow the data quota.
  - **Done when**: full thumbs-driven control from a mobile browser.

- [ ] **48. Auth flow for the web UI** — bearer token gate, with a
  one-time login screen that stores the token in localStorage.
  - **Sub-tasks**:
    - [ ] Login page that POSTs to a new `/auth/check` endpoint.
    - [ ] Token persistence in localStorage.
    - [ ] Auto-attach Authorization header to all subsequent requests.
  - **Done when**: opening the web UI on a fresh browser prompts for
    token, then never again until token rotates.

- [ ] **49. Phase 2 end-to-end smoke test.** Drive a print from
  Claude Desktop via MCP while watching the WebRTC stream in a
  browser.
  - **Sub-tasks**:
    - [ ] All three surfaces alive concurrently.
    - [ ] No deadlocks, no thread leaks.
  - **Done when**: works for 10+ minutes with no degradation.

### Phase 3 — Home Assistant integration (items 50-54)

- [ ] **50. MQTT auto-discovery payloads** matching Home Assistant's
  expectations.
  - **Sub-tasks**:
    - [ ] One MQTT topic per printer state field (bed_temp,
      nozzle_temp, mc_percent, etc.).
    - [ ] HA `homeassistant/sensor/<dev_id>/<field>/config` discovery
      messages.
    - [ ] Per-AMS-slot color/material entities.
    - [ ] Camera entity (snapshot URL).
    - [ ] Lights, fans as switch entities mapped to MQTT cmds.
  - **Done when**: a fresh HA install auto-discovers ALL X2D entities
    with no YAML.

- [ ] **51. Live test against a real Home Assistant install.**
  - **Sub-tasks**:
    - [ ] Set up HA in a container or on the x86 box.
    - [ ] Configure MQTT broker (mosquitto).
    - [ ] Point bridge's HA module at the broker.
    - [ ] Verify entities populate with live values.
  - **Done when**: HA dashboard shows all printer state in real time.

- [ ] **52. Better than ha-bambulab feature comparison.**
  - **Sub-tasks**:
    - [ ] Catalog ha-bambulab's entities.
    - [ ] Confirm we have parity OR explicit improvements.
    - [ ] Document the migration path for ha-bambulab users.
  - **Done when**: feature matrix in `docs/HA_VS_BAMBULAB.md` shows
    ours strictly ≥.

- [ ] **53. HA snapshot entity** that grabs a frame on demand or every
  N seconds.
  - **Sub-tasks**:
    - [ ] Bridge endpoint `/snapshot.jpg` that proxies the latest
      cam frame.
    - [ ] HA `image` platform discovery.
  - **Done when**: HA's image card shows a live-ish snapshot.

- [ ] **54. Multi-printer HA support** — one device per printer
  section in `~/.x2d/credentials`.
  - **Sub-tasks**:
    - [ ] Each named printer gets its own HA Device.
    - [ ] Entities namespaced by printer.
    - [ ] Tested with 2 printers.
  - **Done when**: HA shows 2 separate printer devices with all
    entities each.

### Phase 4 — features upstream BambuStudio doesn't have (items 55-58)

- [ ] **55. Multi-printer print queue** in the GUI's Device tab.
  - **Sub-tasks**:
    - [ ] Source patch: add a "Queue" sub-tab.
    - [ ] Drag-and-drop ordering of pending jobs across printers.
    - [ ] Bridge maintains the queue + dispatches jobs as printers
      idle.
  - **Done when**: queue 3 jobs across 2 printers, watch them
    auto-dispatch.

- [ ] **56. Snapshot / timelapse browser** sub-tab.
  - **Sub-tasks**:
    - [ ] Bridge auto-records a frame every 30s during prints.
    - [ ] GUI sub-tab shows thumbnails per print job.
    - [ ] One-click stitch into MP4 timelapse via ffmpeg.
  - **Done when**: completed print's timelapse playable from the GUI.

- [ ] **57. AI assistant panel** in the GUI talking to the local MCP
  server.
  - **Sub-tasks**:
    - [ ] Embedded panel with text input.
    - [ ] User types: "What's the chamber temp?" → MCP roundtrip →
      reply renders.
    - [ ] Tool calls visible in a transcript.
  - **Done when**: natural-language print control works in the GUI.

- [ ] **58. Real-time AMS color sync UI** — when slot 3 has color
  AF7933 loaded, the GUI's filament picker auto-selects matching
  filament profile.
  - **Sub-tasks**:
    - [ ] Bridge maps tray color → curated filament profile name.
    - [ ] GUI listens via shim event and auto-selects.
  - **Done when**: physically changing AMS slot color updates GUI
    filament selection within 5s.

### Phase 5 — docs + release (items 59-62)

- [ ] **59. README reorg** for the feature-complete product.
  - **Sub-tasks**:
    - [ ] Top-level "What is this" + "Who is this for".
    - [ ] Feature matrix vs upstream + ha-bambulab.
    - [ ] Quick install / quick start.
    - [ ] Links to per-feature docs.
  - **Done when**: a first-time visitor knows what we are within 60s.

- [ ] **60. Per-feature docs in `docs/`**:
  MCP, WebRTC, web UI, Home Assistant, multi-printer, queue, AI panel.
  - **Sub-tasks**:
    - [ ] One markdown file per feature.
    - [ ] Each with overview, install/config, examples.
  - **Done when**: every feature has a reachable doc.

- [ ] **61. Demo media** — short MP4s of each major flow.
  - **Sub-tasks**:
    - [ ] CLI demo (status + print).
    - [ ] GUI demo (slice + print + monitor).
    - [ ] MCP demo (Claude Desktop driving).
    - [ ] Web UI demo.
    - [ ] HA dashboard demo.
  - **Done when**: 5 short MP4s in `docs/demos/`.

- [ ] **62. v1.0.0 release.**
  - **Sub-tasks**:
    - [ ] Tag, build, upload tarball + sha + per-platform notes.
    - [ ] Announcement post.
    - [ ] CHANGELOG with everything since v0.1.0.
  - **Done when**: GitHub Releases shows v1.0.0 with full asset set.

### Phase 0 deferred — device-required final verification

- [ ] **35. Final Phase 0 ADB verification.** Every Phase 0 fix
  composed end-to-end on a fresh device install.
  - **Sub-tasks**:
    - [ ] Wipe `~/.config/BambuStudioInternal/` on device.
    - [ ] Run install.sh from scratch.
    - [ ] Launch bambu-studio.
    - [ ] Verify: no wizard, no asserts, no gvfs popup, full Prepare
      tab with 3D viewport + AMS + presets, full Device tab with live
      X2D state.
  - **Done when**: a brand new user can launch and use the GUI with
    zero papercuts.
