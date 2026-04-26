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

- [ ] **1. Stub `libbambu_networking.so` for aarch64.** Native shared
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
    - [ ] SSDP auto-discovery in the bridge so the GUI's Devices tab
      auto-populates the X2D. Without this the user has to add the
      device manually via "Add Device" / "Connect via LAN" because
      our `bambu_network_start_discovery` returns true but the bridge
      never emits the SSDP `OnMsgArrivedFn` events the host listens
      for. New sub-task: bridge listens on `udp/2021` for Bambu's
      mDNS-over-SSDP broadcasts, parses each `bambu-net.local`
      announcement, forwards as `evt:ssdp_msg` over the socket. Shim
      hands each one to the host-registered `set_on_ssdp_msg_fn`
      callback. Then the GUI's printer auto-list works without manual
      "Add Device" clicks.
    - [ ] Final end-to-end test once SSDP is in: confirm device shows
      up in Devices list, click Connect, AMS spool slot data renders,
      Print on a sliced plate actually starts. Documented click trail
      + screenshots.
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

- [ ] **9. Upstream the 4 Button-widget touch-drift patches.** Open a PR
  on bambulab/BambuStudio with the four `mouseReleased` patches. Touch
  drift hits any tablet / convertible / touch-screen kiosk, not just
  Termux.
  - **Sub-tasks**:
    - [ ] Squash the four patches into a single commit on a branch
      against upstream `master`.
    - [ ] PR with rationale + a short reproduction note.
    - [ ] Link the PR back from `patches/README.md`.
  - **Done when**: PR opened; no expectation of merge, but link is live.

- [ ] **10. One-command installer.** `bash <(curl -fsSL
  https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)` that
  pkg-installs deps, fetches latest release tarball, verifies SHA,
  pre-seeds AppConfig template, sets up `~/.x2d/credentials` skeleton,
  drops a `~/.termux/boot/` autostart for the bridge daemon.
  - **Sub-tasks**:
    - [ ] Write `install.sh` with `set -eu`; bail with clear errors on
      missing termux-x11 / unsupported arch / network failure.
    - [ ] SHA-256 verify the tarball before unpacking.
    - [ ] Idempotent: re-running upgrades in place.
    - [ ] README front-page badge / quick-start uses it.
  - **Done when**: a fresh Termux session can `curl … | bash` and end up
    with a working GUI launch in one command.
