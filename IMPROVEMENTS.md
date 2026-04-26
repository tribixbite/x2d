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
    - [ ] Read NetworkAgent.{hpp,cpp} fully; enumerate every typedef and
      its calling convention.
    - [ ] Define the RPC protocol (JSON line-delimited over
      `~/.x2d/bridge.sock`) and document in `runtime/network_shim/PROTOCOL.md`.
    - [ ] Add bridge-side: `x2d_bridge.py serve` subcommand that listens
      on the socket, dispatches RPCs, emits async events for state pushes.
    - [ ] Implement the .so in `runtime/network_shim/` (CMake target). Stub
      every cloud-only entry point with proper return-by-value defaults.
      Wire LAN entry points to the socket client. Threading: shim owns a
      worker thread for the socket, marshals callbacks back via
      `g_idle_add` so they hit the GTK thread.
    - [ ] BambuStudio plugin discovery: figure out where the plugin path is
      configured (`AppConfig`? hard-coded? env var?) and either drop the
      .so at the expected path or set the discovery hint.
    - [ ] End-to-end test: launch GUI → click "Add Device" / LAN mode →
      enter creds → confirm device shows up + AMS slot data populates +
      "Print" button uploads + start succeeds. Document exact click trail
      and expected screenshots.
  - **Done when**: GUI's Devices tab shows the X2D as connected, AMS spool
    colours render in real time, clicking Print on a sliced plate actually
    starts a print on the printer.

- [ ] **2. Print-control commands in `x2d_bridge`.** Add `pause`, `resume`,
  `stop`, `home`, `level`, `set-temp <bed|nozzle> <C>`,
  `ams-unload <slot>`, `ams-load <slot>`, `jog <axis> <distance>`,
  `chamber-light <on|off>`. Each is one signed MQTT publish.
  - **Sub-tasks**:
    - [ ] Reverse-engineer each command's payload from BambuStudio source
      (search for `"command":` strings in `slic3r/GUI/DeviceCore/`).
    - [ ] Implement in `x2d_bridge.py`. One subcommand per CLI verb. Share
      payload-builder helpers.
    - [ ] Smoke-test each against a real X2D. Document expected state
      change in this file.
    - [ ] Add usage examples to README.
  - **Done when**: every command verifiably changes printer state, idle or
    mid-print as appropriate.

- [ ] **3. Camera proxy in `x2d_bridge`.** `x2d_bridge.py camera` reads
  `rtsps://<ip>:322/streaming/live/1` (auth `bblp:<code>`) via ffmpeg
  subprocess and re-emits MJPEG over `http://127.0.0.1:8766/cam.mjpeg`.
  Lets a phone browser view the print live without going through Bambu's
  cloud.
  - **Sub-tasks**:
    - [ ] Confirm RTSP endpoint via direct ffmpeg probe.
    - [ ] Implement `camera` subcommand that spawns an ffmpeg pipeline,
      tees frames into HTTP responses; survives RTSP reconnects.
    - [ ] Handle multiple concurrent viewers without re-spawning ffmpeg.
    - [ ] README: install `ffmpeg` instructions + viewer usage.
  - **Done when**: `curl http://127.0.0.1:8766/cam.mjpeg` streams real
    frames; a browser at the same URL plays smoothly for >5 minutes
    without disconnect.

- [ ] **4. CI**: GitHub Actions on every push.
  - **Sub-tasks**:
    - [ ] `.github/workflows/ci.yml` — Python lint (`ruff` or `flake8`),
      `mypy --strict` on `x2d_bridge.py`, signing roundtrip test against
      a stubbed paho-mqtt broker.
    - [ ] Verify the prebuilt tarball SHA in the release matches the file.
    - [ ] Status badge in README.
  - **Done when**: green check on every commit; fails when secrets / lint
    / sig roundtrip broken.

- [ ] **5. Sidebar shrinkability patch.** Patch BambuStudio's left-rail
  sidebar minimum widths so the GUI fits portrait phone displays without
  horizontal clip. Today the bambu MainFrame's content sums to ~1000 px
  min-width and overrides the SetSize clamp.
  - **Sub-tasks**:
    - [ ] Identify the exact panels with hard-coded `SetMinSize` in
      `Sidebar.cpp` / equivalent.
    - [ ] Conditionally relax the floor when display width < 1000.
    - [ ] Verify the rest of the layout doesn't break (filament list,
      preset combo, print/slice buttons all still reachable).
    - [ ] Add `patches/Sidebar.cpp.termux.patch`.
  - **Done when**: window fits inside 672 px wide display with no clipped
    controls.

- [ ] **6. `wxFileDialog` overlay wrapper.** Even with openbox, file
  pickers can be small / off-centre. Subclass or post-show fix-up so
  every wxFileDialog is sized to the display and centred on the parent
  frame.
  - **Sub-tasks**:
    - [ ] Decide: subclass via app-level helper vs a tiny LD_PRELOAD shim
      that hooks `gtk_file_chooser_dialog_new`.
    - [ ] Implement; verify drag still works (don't break openbox).
    - [ ] Confirm `g_file_enumerator_*` "permission denied on /" stops
      surfacing as a popup (or document that it's still informational).
  - **Done when**: opening "Import STL" / "Save Project" lands a fully
    visible file chooser at sane dimensions on a 672 px display.

- [ ] **7. Multi-printer config**. `~/.x2d/credentials` with named
  sections `[printer:studio]`, `[printer:basement]`; `x2d_bridge
  --printer studio status` selects.
  - **Sub-tasks**:
    - [ ] Update `Creds.resolve` to accept a printer-name flag.
    - [ ] Default to first section if only one exists, error if ambiguous.
    - [ ] Daemon mode: option to bind one HTTP port per printer.
    - [ ] README updated.
  - **Done when**: two printer credential sections work; `--printer
    <name>` switches.

- [ ] **8. `/healthz` endpoint.** Daemon HTTP exposes `/healthz` that
  returns 200 if MQTT connection is alive (last successful message <
  configurable threshold) and 503 otherwise. Currently `/state` may
  serve stale JSON if MQTT silently disconnected.
  - **Sub-tasks**:
    - [ ] Track `last_message_ts` in `X2DClient`.
    - [ ] Add `/healthz` handler with configurable threshold flag
      (`--max-staleness 30`).
    - [ ] On unhealthy, optionally trigger reconnect.
    - [ ] Add to README + show as Home Assistant binary_sensor example.
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
