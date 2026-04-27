# x2d v1.0.0 — Feature-complete LAN-first stack

86 commits since v0.1.0. 62 ledger items closed. Six new daemon
surfaces, full Home Assistant integration, MCP server for Claude
Desktop, mobile web UI, multi-printer queue, auto-timelapses, AI
assistant, real-time AMS color sync.

## What's new since v0.1.0

* **Six-surface daemon** built on top of the bridge:
  REST + Server-Sent Events, Prometheus `/metrics`, structured
  JSON access log with rotation, Home Assistant MQTT
  auto-discovery, WebRTC chamber-camera streaming (~100 ms
  latency), MCP stdio server for Claude Desktop / Cursor /
  Continue, and a mobile-friendly web UI at `/` (~17 KB total
  static assets, no framework).
* **Multi-printer everywhere** — daemon, web UI, queue, HA
  publisher, MCP server. One `[printer:NAME]` section per printer
  in `~/.x2d/credentials` and every surface auto-discovers them.
  Failures are isolated per-printer.
* **Native Home Assistant integration** with **32 entities + 1
  Device per printer**, including AMS-color → Bambu-profile
  auto-resolve via the official `filaments_color_codes.json`
  (~7000 entries). Live-tested against real Home Assistant Core
  2025.1.4.
* **Print queue** with HTML5 native drag-and-drop reorder,
  cross-printer move, file-backed FIFO at `~/.x2d/queue.json`,
  crash-safe (running → pending demotion on reload). Web UI
  Queue card.
* **Auto-timelapses**: per-printer state-driven JPEG capture
  every 30 s during prints, one-click ffmpeg stitch into MP4,
  inline `<video>` playback in the web UI.
* **AI assistant in the web UI** that calls the same MCP toolset
  Claude Desktop sees. Three providers: pure-Python `local`
  router (no API key), `anthropic` (with the canonical MCP tool
  loop), and `auto`.
* **WebRTC streaming** at sub-second latency vs HLS's 6-8 s.
  Browser viewer at `/cam.webrtc.html`. Built on aiortc + aiohttp.

Full per-feature breakdown in [CHANGELOG.md](https://github.com/tribixbite/x2d/blob/main/CHANGELOG.md).

## Quick install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)

# Configure
cat >~/.x2d/credentials <<'EOF'
[printer]
ip     = 192.168.1.42
code   = 12345678
serial = 03ABC0001234567
EOF

# Run
x2d_bridge.py daemon --http 0.0.0.0:8765 \
    --queue --timelapse \
    --auth-token "$(openssl rand -hex 32)"

# Open the web UI
xdg-open http://localhost:8765/
```

## Per-platform notes

### Termux (Android, aarch64) — primary target

The full stack runs natively. WebRTC needs `libsrtp` built from
source (covered in [`docs/WEBRTC.md`](https://github.com/tribixbite/x2d/blob/main/docs/WEBRTC.md));
`install.sh` does this automatically. `aiortc==1.10.1` and
`av==13.1.0` are pinned because newer versions need PyAV 14
features that don't build on Termux's stock Cython.

The BambuStudio Termux GUI port is in this tarball as
`bin/bambu-studio` (77 MB stripped) — same six source patches
plus the LD_PRELOAD shim.

### Linux (x86_64 / aarch64 desktop)

`x2d_bridge.py daemon` + `python -m mcp_x2d` + `runtime/ha/publisher.py`
all work as-is. WebRTC stack installs cleanly via the standard
`pip install aiortc` (no source-build of libsrtp needed). The
BambuStudio binary is Termux-specific — on desktop Linux, install
upstream BambuStudio v02.06.00.51 normally and point its Network
Plugin at the bridge daemon via the LAN-mode "physical printer"
config flow.

### macOS

Same as Linux. MCP integration with Claude Desktop is documented
in [`docs/MCP.md`](https://github.com/tribixbite/x2d/blob/main/docs/MCP.md).
The `claude_desktop_config.json` example block is at
[`docs/claude_desktop_config.example.json`](https://github.com/tribixbite/x2d/blob/main/docs/claude_desktop_config.example.json).

### Windows

The bridge daemon runs under WSL2 or native Python 3.12. WebRTC
is supported. The BambuStudio binary is Linux-only — use upstream
Windows BambuStudio + the bridge in WSL2 / a homelab VM.

## Documentation

| Topic | Doc |
|---|---|
| Quick start          | [`docs/QUICKSTART.md`](https://github.com/tribixbite/x2d/blob/main/docs/QUICKSTART.md) |
| Web UI               | [`docs/WEB_UI.md`](https://github.com/tribixbite/x2d/blob/main/docs/WEB_UI.md) |
| MCP server           | [`docs/MCP.md`](https://github.com/tribixbite/x2d/blob/main/docs/MCP.md) |
| WebRTC streaming     | [`docs/WEBRTC.md`](https://github.com/tribixbite/x2d/blob/main/docs/WEBRTC.md) |
| Home Assistant       | [`docs/HA.md`](https://github.com/tribixbite/x2d/blob/main/docs/HA.md) |
| HA vs ha-bambulab    | [`docs/HA_VS_BAMBULAB.md`](https://github.com/tribixbite/x2d/blob/main/docs/HA_VS_BAMBULAB.md) |
| Multi-printer setup  | [`docs/MULTI_PRINTER.md`](https://github.com/tribixbite/x2d/blob/main/docs/MULTI_PRINTER.md) |
| Print queue          | [`docs/QUEUE.md`](https://github.com/tribixbite/x2d/blob/main/docs/QUEUE.md) |
| Timelapse browser    | [`docs/TIMELAPSE.md`](https://github.com/tribixbite/x2d/blob/main/docs/TIMELAPSE.md) |
| AI assistant         | [`docs/ASSISTANT.md`](https://github.com/tribixbite/x2d/blob/main/docs/ASSISTANT.md) |
| AMS color sync       | [`docs/COLORSYNC.md`](https://github.com/tribixbite/x2d/blob/main/docs/COLORSYNC.md) |

## Demo media

Five short MP4s in [`docs/demos/`](https://github.com/tribixbite/x2d/tree/main/docs/demos)
walking through the CLI, GUI, MCP, web UI, and HA dashboard flows.
Total ~3.2 minutes. Reproducible via
`runtime/demos/render.py`.

## What's deferred to v1.1

Two items intentionally past this release because they require
physical-print-time + an attached ADB device:

* **#35 Final Phase 0 ADB verification** — manual zero-papercut
  walkthrough on a fresh `~/.config/BambuStudioInternal/`.
* **#41 Print the rumi frame end-to-end via the GUI** — full
  slice-and-print run from BambuStudio. The underlying
  `start_print` C-ABI is already exercised by
  `runtime/network_shim/tests/test_shim_e2e.py` against the real
  X2D, so the code path is proven; only the GUI walkthrough is
  pending.

## Verifying the tarball

```bash
sha256sum -c bambustudio-x2d-termux-aarch64.tar.xz.sha256
```

Expected SHA-256 is in the attached `.sha256` asset.

## Thanks

Built and tested on a Samsung S25 Ultra (Termux, aarch64) against
a real Bambu Lab X2D running Jan-2025+ firmware (RSA-SHA256 signed
MQTT).
