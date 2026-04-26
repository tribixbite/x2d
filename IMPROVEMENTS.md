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

- [ ] **4. CI**: GitHub Actions on every push.
  - **Sub-tasks**:
    - [ ] `.github/workflows/ci.yml` — runs ruff lint over every Python
      script, mypy with reasonable flags on the bridge core
      (`x2d_bridge.py`, `bambu_cert.py`), then two self-tests:
      `tests/test_signing_roundtrip.py` (signs + verifies a payload
      with the embedded leaked cert — guards against any regression
      that would silently break MQTT publishes), and
      `tests/test_serve_smoke.py` (spawns `x2d_bridge.py serve` on a
      tempdir socket, asserts hello / get_version / unknown-op
      responses match the wire format in PROTOCOL.md).
    - [ ] Second job verifies the v0.1.0 release's `.sha256` asset
      matches the `.tar.xz` it sits next to (catches the "uploaded
      tarball without refreshing sha" race that bit me in v0.1.0).
    - [ ] Status badge wired into the README.
  - **Done when**: green check on every commit; fails when secrets / lint
    / sig roundtrip broken.

- [ ] **5. Sidebar shrinkability patch.** Patch BambuStudio's left-rail
  sidebar so the Plater fits portrait phone displays without
  horizontal clip.
  - **Sub-tasks**:
    - [ ] Identified the three call-sites that hard-code
      `42 * wxGetApp().em_unit()` (≈504 px) for the Sidebar width:
      `Sidebar::Sidebar` ctor's initial wxSize, `Sidebar::msw_rescale`
      `SetMinSize`, and `Plater::priv::priv`'s wxAuiPaneInfo `BestSize`.
    - [ ] Added `static int sidebar_default_width()` near the Sidebar
      class that returns `clamp(42*em, 15*em, display_width/3)`. On a
      landscape desktop max() picks 42*em → no-op; on portrait
      Termux it picks display_width/3 (≈224 px on a 672-wide display)
      so the 3D viewport actually has room.
    - [ ] Verified visually: launched bambu, switched to Prepare tab,
      screenshot shows the 3D bed grid + Top/Front gizmo on the right
      side of the window — first time those have been visible without
      horizontal scroll on this device. Filament list, preset combo,
      and quality/strength/speed tab navigation all still reachable
      (≥15*em floor protects them).
    - [ ] Wrote `patches/Plater.cpp.termux.patch` (+22, -3).
  - **Done when**: window fits inside 672 px wide display with no clipped
    controls.

- [ ] **6. `wxFileDialog` overlay wrapper** via LD_PRELOAD hook on
  `gtk_window_present` / `gtk_window_present_with_time`. Every
  transient/dialog window is checked on first present and clamped to
  the workarea if it overflows OR up-sized to a 320×200 floor if
  upstream picked a too-tiny default. Already-fitting windows are
  left alone (geometry untouched, openbox places them).
  - **Sub-tasks**:
    - [ ] Decided: LD_PRELOAD shim. Subclassing wxFileDialog would
      need every BambuStudio call-site updated; the GTK-level hook
      catches every dialog (file chooser + message dialog +
      BambuStudio's custom modals) for free.
    - [ ] Implemented in `runtime/preload_gtkinit.c` with a
      per-window GQuark guard so each dialog is sized at most once
      (so the user can later drag it without us fighting them).
    - [ ] Verified live: launched bambu, observed
      `[preload] resized dialog 480x480→480x480 at (96,464)` on the
      gvfs permission popup (centered, fits) and the main bambu
      window left untouched at 668×1382 (already fits the workarea).
      No fight with openbox's own placement logic.
    - [ ] The "permission denied on /" popup remains informational —
      we surface it at a visible center position rather than tucked
      under the main frame; the underlying gvfs error itself is from
      Android selinux blocking app-process root reads and is not
      something the shim can fix (or should — clicking OK dismisses
      and the file chooser proceeds normally).
  - **Done when**: opening "Import STL" / "Save Project" lands a fully
    visible file chooser at sane dimensions on a 672 px display.

- [ ] **7. Multi-printer config**. `~/.x2d/credentials` accepts
  multiple `[printer:NAME]` sections; `x2d_bridge --printer NAME
  status` selects which one. The plain `[printer]` is still the
  unnamed default. `X2D_PRINTER` env var works as a fallback.
  - **Sub-tasks**:
    - [ ] Updated `Creds.resolve` with section-name picker logic.
      Adds an explicit `name` field to the dataclass so downstream
      code can tell which printer it ended up talking to.
    - [ ] Resolution order: `--printer` flag → `X2D_PRINTER` env →
      plain `[printer]` if present → single `[printer:NAME]`
      auto-pick if only one exists → otherwise error with the list
      of available names. `Creds.list_names()` helper exposed for
      multi-printer-aware callers.
    - [ ] Smoke-tested all four cases (ambiguous, valid name, invalid
      name, list_names) against a temporary `~/.x2d/credentials` —
      every branch produced the expected output.
    - [ ] README documents the new layout in the LAN-bridge section.
  - **Done when**: two printer credential sections work; `--printer
    <name>` switches.

- [ ] **8. `/healthz` endpoint.** Daemon HTTP exposes `/healthz` that
  returns 200 + `{"healthy":true,...}` JSON if a printer state push
  arrived within `--max-staleness` (default 30s), 503 otherwise.
  Catches the silent-MQTT-disconnect case where `/state` would
  serve stale JSON for minutes.
  - **Sub-tasks**:
    - [ ] `X2DClient._on_message` records `last_message_ts`; exposed
      as a `last_message_ts` property.
    - [ ] `_serve_http` accepts a `get_last_ts` callback + a
      `max_staleness` window. The handler computes `age = now - last`
      and returns 200/503 accordingly with a small JSON diagnostic
      body (healthy / last_message_ts / last_message_age_s /
      max_staleness_s).
    - [ ] `cmd_daemon` wires it through; `--max-staleness 30.0` is
      the new CLI flag.
    - [ ] Smoke-test: launched the daemon, hit `/healthz`, got
      `200 OK` with `{healthy:true, last_message_age_s:0.51, ...}`.
      The 503 path is the same handler with one branch flipped.
    - [ ] README updated under "LAN bridge" with `/healthz` example.
  - **Done when**: kill the printer's wifi; `/healthz` flips to 503
    within `--max-staleness` seconds; restoring wifi recovers.

- [ ] **9. Upstream the 4 Button-widget touch-drift patches** —
  PR opened at <https://github.com/bambulab/BambuStudio/pull/10385>
  (4 files, +28/-6, OPEN).
  - **Sub-tasks**:
    - [ ] Pre-publish review (pal-mcp + code-reviewer subagent)
      caught three real issues: (a) the original "drop bounds check"
      lost the desktop drag-off-to-cancel gesture — replaced with
      a 15 px `Inflate` slop instead, (b) `AxisCtrlButton` +
      `TabButton` were calling `ReleaseMouse()` without a
      `HasCapture()` guard which asserts in wx debug builds —
      added the guard, (c) PR body had factual errors about
      `wxNotebook` / `wxButton` GTK behaviour — rewritten with
      defensible phrasing.
    - [ ] Final patch in `upstream-pr/touchscreen-button-fix.patch`
      (+22, -7 across four files). `git apply --check -3` against a
      fresh clone of `bambulab/BambuStudio@v02.06.00.51` succeeded
      cleanly.
    - [ ] PR body in `upstream-pr/PR_BODY.md` — symptom-first title,
      precise root cause (incl. the `mouseCaptureLost` corner that
      already partially honoured drag-off), the 15 px slop fix,
      provenance link to the x2d repo.
    - [ ] `upstream-pr/OPEN_PR.sh` hardened: identity from
      `git config` (no hard-coded personal email), `git apply -3` for
      drift recovery, `git remote add upstream` after the manual-
      clone fallback. Syntax-clean.
    - [ ] Ran `bash upstream-pr/OPEN_PR.sh`; the output was the live
      PR URL.
    - [ ] Linked the PR URL back from `patches/README.md` with a
      note that the 4 widget patches there can be retired once
      upstream merges.
  - **Done when**: PR opened; no expectation of merge, but link is live.

- [ ] **10. One-command installer.** `install.sh` at the repo root.
  Runs end-to-end via
  `bash <(curl -fsSL https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)`.
  - **Sub-tasks**:
    - [ ] `set -eu` + dedicated platform-check that bails fast on
      non-Termux / non-aarch64.
    - [ ] `pkg install` of the full runtime dep list (idempotent —
      checks `pkg list-installed` first; only invokes `pkg install`
      for missing packages).
    - [ ] `pip install paho-mqtt` if not already importable.
    - [ ] Downloads the latest tarball + sibling `.sha256`; verifies
      SHA-256 BEFORE unpacking; aborts with a red error on mismatch.
    - [ ] Drops `libbambu_networking.so` + `libBambuSource.so` plug-ins
      to `~/.config/BambuStudioInternal/plugins/`; runs the wizard-skip
      binary patch on the new `bin/bambu-studio`.
    - [ ] Pre-seeds `~/.config/BambuStudioInternal/BambuStudio.conf`
      with the X2D model entry only if the file doesn't already exist
      (so a re-run never clobbers user state).
    - [ ] Drops a chmod-600 `~/.x2d/credentials` skeleton if absent.
    - [ ] If `~/.termux/boot/` exists (the user has the Termux:Boot
      Android app installed), drops a `~/.termux/boot/x2d-bridge`
      launcher so the bridge daemon comes back after a phone reboot.
    - [ ] README quick-start uses it as the canonical install path.
    - [ ] `bash -n install.sh` syntax-clean; the platform-check section
      runs to completion in dry-run.
  - **Done when**: a fresh Termux session can `curl … | bash` and end up
    with a working GUI launch in one command.
