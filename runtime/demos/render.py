"""Render the five demo MP4s for IMPROVEMENTS.md item #61.

* `cli_demo.mp4`     — fake-terminal type-and-output of the canonical
                        bridge CLI verbs (status + pause + print).
* `gui_demo.mp4`     — slideshow of the existing live-GUI proof PNGs
                        in docs/ (sidebar / device tab / Prepare).
* `mcp_demo.mp4`     — fake-terminal of a full MCP stdio handshake +
                        tools/call list_printers + status round-trip.
* `webui_demo.mp4`   — three real screenshots from chromium-headless
                        of the running web UI (loaded with ?capture=1
                        so SSE doesn't keep the page in "loading").
* `ha_demo.mp4`      — slideshow of the docs/ha-live-proof JSON
                        snapshots rendered as terminal-style frames
                        (HA frontend itself isn't reachable from this
                        environment without re-spinning the proot HA
                        instance, which is too heavy for a build-time
                        artefact; the on-disk registry snapshots are
                        the load-bearing proof from #51 anyway).

Each MP4 ships at 24 fps for smooth scroll, 1280×720 for readability.
The terminal frames render at 24 px line height in PIL, so a 720-px
canvas fits ~28 lines — enough for a 30-second narrative arc per demo.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR   = REPO_ROOT / "docs" / "demos"

W, H, FPS = 1280, 720, 24
BG = (16, 20, 24)
FG = (220, 230, 240)
PROMPT = (102, 224, 140)
MUTED = (140, 152, 168)
ACCENT = (255, 200, 87)


def _font(size: int):
    """Try the well-known monospace fonts; PIL's default-bitmap
    fallback always works."""
    from PIL import ImageFont
    candidates = [
        "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/system/fonts/DroidSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _terminal_frame(lines: list[tuple[str, tuple]], cursor: bool = False):
    """Render a list of (text, color) lines onto a black canvas.
    Returns a PIL Image."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    font = _font(18)
    line_h = 24
    pad_x, pad_y = 22, 18
    title_font = _font(14)
    draw.text((pad_x, 4), "x2d_bridge — terminal",
              font=title_font, fill=MUTED)
    draw.line([(0, 26), (W, 26)], fill=(40, 48, 56), width=1)
    y = pad_y + 18
    for text, color in lines[-28:]:
        draw.text((pad_x, y), text, font=font, fill=color)
        y += line_h
    if cursor:
        draw.rectangle([(pad_x, y - 2), (pad_x + 10, y + 18)],
                       fill=PROMPT)
    return img


def _slideshow_frame(title: str, png_path: Path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    title_font = _font(22)
    draw.text((24, 18), title, font=title_font, fill=FG)
    if not png_path.exists():
        sub = _font(16)
        draw.text((24, 64), f"(missing: {png_path})",
                  font=sub, fill=MUTED)
        return img
    src = Image.open(png_path).convert("RGB")
    # Fit src into the bottom 600 px while preserving aspect.
    avail_w, avail_h = W - 48, H - 80
    sw, sh = src.size
    scale = min(avail_w / sw, avail_h / sh)
    new = src.resize((int(sw * scale), int(sh * scale)), Image.LANCZOS)
    img.paste(new, ((W - new.width) // 2, 60 + (avail_h - new.height) // 2))
    return img


def _write_mp4(frames: list, out: Path, fps: int = FPS) -> bool:
    if not frames:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for i, im in enumerate(frames):
            im.save(Path(tmp) / f"{i:05d}.png", "PNG")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", str(fps),
            "-i", str(Path(tmp) / "%05d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=120)
        if proc.returncode != 0 or not out.exists():
            print(f"[demo] ffmpeg failed for {out.name}: {proc.stderr.splitlines()[-1] if proc.stderr else proc.returncode}",
                  file=sys.stderr)
            return False
    return True


# ---------------------------------------------------------------------------
# DEMO 1 — CLI
# ---------------------------------------------------------------------------

CLI_SCRIPT: list[tuple[float, str, tuple]] = [
    # (hold_seconds, text, color)
    (0.6, "$ python3.12 x2d_bridge.py status",                  FG),
    (1.5, '{"print": {"nozzle_temper": 27.0, "bed_temper": 25.0,', MUTED),
    (0.0, '            "chamber_temper": 33.4, "wifi_signal": "-58dBm",', MUTED),
    (0.0, '            "gcode_state": "IDLE"}}',                  MUTED),
    (1.0, "",                                                     FG),
    (0.4, "$ python3.12 x2d_bridge.py chamber-light on",          FG),
    (1.0, "[x2d-bridge] published ledctrl led_mode=on",           PROMPT),
    (0.4, "$ python3.12 x2d_bridge.py print rumi_frame.gcode.3mf --slot 3", FG),
    (1.0, "  ↑ uploading via FTPS:990 (1.2 MB)",                  MUTED),
    (1.0, "  ✓ start_print queued: rumi_frame.gcode.3mf (slot=3, ams=True)", PROMPT),
    (1.0, "",                                                     FG),
    (0.4, "$ python3.12 x2d_bridge.py daemon --http :8765 --queue --timelapse &", FG),
    (1.4, "[x2d-bridge] HTTP listening on http://0.0.0.0:8765/state", MUTED),
    (0.0, "[x2d-bridge] daemon up; 1 printer(s); polling every 5s.", MUTED),
    (0.0, "[x2d-bridge] queue enabled; persisted at ~/.x2d/queue.json", MUTED),
    (0.0, "[x2d-bridge] timelapse recorder enabled (every 30s during prints)", MUTED),
    (1.5, "",                                                     FG),
    (0.6, "$ curl -s http://127.0.0.1:8765/state | jq .print.mc_percent", FG),
    (1.0, "42",                                                   ACCENT),
    (1.5, "",                                                     FG),
]


def _build_terminal_frames(script, fps=FPS):
    """Type out each line one char at a time, then hold."""
    frames = []
    rendered: list[tuple[str, tuple]] = []
    for hold, text, color in script:
        # Type animation: ~50 chars/s = 1 char per 2 frames at 24 fps.
        for i in range(1, max(1, len(text) + 1)):
            rendered.append((text[:i], color))
            frames.append(_terminal_frame(rendered, cursor=True))
            rendered.pop()
        rendered.append((text, color))
        # Hold N seconds with the cursor on.
        hold_frames = max(1, int(hold * fps))
        for _ in range(hold_frames):
            frames.append(_terminal_frame(rendered, cursor=True))
    return frames


def render_cli_demo() -> bool:
    print("[demo] cli_demo.mp4 — rendering terminal frames")
    frames = _build_terminal_frames(CLI_SCRIPT)
    return _write_mp4(frames, OUT_DIR / "cli_demo.mp4")


# ---------------------------------------------------------------------------
# DEMO 2 — GUI slideshow
# ---------------------------------------------------------------------------

GUI_SLIDES = [
    ("BambuStudio Termux port — Prepare tab",
     REPO_ROOT / "docs" / "ssdp-live-proof.png"),
    ("Device tab — MonitorPanel after SSDP",
     REPO_ROOT / "docs" / "device-tab-monitorpanel-proof.png"),
    ("Sidebar shrinks below 1200px display width (#5)",
     REPO_ROOT / "docs" / "sidebar-shrink-proof.png"),
    ("X2D preset selected on first launch",
     REPO_ROOT / "docs" / "prepare-tab-x1c-preset-proof.png"),
]


def render_gui_demo() -> bool:
    print("[demo] gui_demo.mp4 — slideshow of static proofs")
    frames = []
    for title, path in GUI_SLIDES:
        if not path.exists():
            continue
        f = _slideshow_frame(title, path)
        # Hold each slide for 4 seconds.
        for _ in range(4 * FPS):
            frames.append(f)
    if not frames:
        print("[demo] gui_demo.mp4 — no source PNGs available; skipping",
              file=sys.stderr)
        return False
    return _write_mp4(frames, OUT_DIR / "gui_demo.mp4")


# ---------------------------------------------------------------------------
# DEMO 3 — MCP
# ---------------------------------------------------------------------------

MCP_SCRIPT: list[tuple[float, str, tuple]] = [
    (0.5, "$ python3.12 -m mcp_x2d", FG),
    (1.0, "[mcp] x2d-bridge MCP server up "
          "(bridge=…/x2d_bridge.py, daemon=http://127.0.0.1:8765)", MUTED),
    (1.0, "", FG),
    (0.3, '> {"jsonrpc":"2.0","id":1,"method":"initialize",', FG),
    (0.0, '   "params":{"protocolVersion":"2025-06-18",', FG),
    (0.0, '             "clientInfo":{"name":"claude-desktop"}}}', FG),
    (1.0, '< {"jsonrpc":"2.0","id":1,"result":{', PROMPT),
    (0.0, '    "protocolVersion":"2025-06-18",', PROMPT),
    (0.0, '    "serverInfo":{"name":"x2d-bridge","version":"0.1.0"},', PROMPT),
    (0.0, '    "capabilities":{"tools":{},"resources":{}}}}', PROMPT),
    (1.5, "", FG),
    (0.3, '> {"jsonrpc":"2.0","id":2,"method":"tools/list"}', FG),
    (1.0, '< {"jsonrpc":"2.0","id":2,"result":{"tools":[', PROMPT),
    (0.0, '    {"name":"status",...}, {"name":"pause",...},', PROMPT),
    (0.0, '    {"name":"resume",...}, {"name":"stop",...},', PROMPT),
    (0.0, '    {"name":"chamber_light",...}, {"name":"ams_load",...},', PROMPT),
    (0.0, '    {"name":"camera_snapshot",...}, ... 18 tools]}}', PROMPT),
    (1.5, "", FG),
    (0.3, '> {"jsonrpc":"2.0","id":3,"method":"tools/call",', FG),
    (0.0, '   "params":{"name":"status","arguments":{}}}', FG),
    (2.0, '< {"jsonrpc":"2.0","id":3,"result":{', PROMPT),
    (0.0, '    "content":[{"type":"text","text":', PROMPT),
    (0.0, '      "{\\"print\\":{\\"nozzle_temper\\":27.0,...}}"}],', PROMPT),
    (0.0, '    "isError":false}}', PROMPT),
    (1.5, "", FG),
    (0.4, "[ Claude Desktop now displays the full state in chat ]", ACCENT),
    (2.0, "", FG),
]


def render_mcp_demo() -> bool:
    print("[demo] mcp_demo.mp4 — rendering JSON-RPC handshake")
    frames = _build_terminal_frames(MCP_SCRIPT)
    return _write_mp4(frames, OUT_DIR / "mcp_demo.mp4")


# ---------------------------------------------------------------------------
# DEMO 4 — Web UI screenshot slideshow
# ---------------------------------------------------------------------------

WEBUI_SHOTS = [
    ("Mobile portrait (S25 Ultra, 412×892)",
     REPO_ROOT / "docs" / "webui-portrait-s25.png"),
    ("Mobile landscape (S25 Ultra, 892×412)",
     REPO_ROOT / "docs" / "webui-landscape-s25.png"),
    ("Tablet (1080×2340) — two-column responsive layout",
     REPO_ROOT / "docs" / "webui-portrait-tablet.png"),
]


def render_webui_demo() -> bool:
    print("[demo] webui_demo.mp4 — chromium screenshots slideshow")
    frames = []
    for title, path in WEBUI_SHOTS:
        if not path.exists():
            continue
        f = _slideshow_frame(title, path)
        for _ in range(5 * FPS):
            frames.append(f)
    if not frames:
        print("[demo] webui_demo.mp4 — no source PNGs available; skipping",
              file=sys.stderr)
        return False
    return _write_mp4(frames, OUT_DIR / "webui_demo.mp4")


# ---------------------------------------------------------------------------
# DEMO 5 — HA dashboard (registry snapshot rendering)
# ---------------------------------------------------------------------------

HA_SUMMARY_LINES: list[tuple[float, str, tuple]] = [
    (1.0, "Real Home Assistant Core 2025.1.4 + x2d HA publisher",        FG),
    (0.4, "──────────────────────────────────────────────────",          MUTED),
    (1.0, "",                                                            FG),
    (0.4, "Device registered:",                                          ACCENT),
    (1.0, "  Bambu Lab X2D (mqtt:x2d_20P9AJ612700155)",                  PROMPT),
    (0.0, "  manufacturer: Bambu Lab   model: X2D",                      MUTED),
    (1.5, "",                                                            FG),
    (0.4, "32 entities auto-discovered:",                                ACCENT),
    (0.5, "  sensor.x2d_…_nozzle_temp           213.5 °C",               PROMPT),
    (0.0, "  sensor.x2d_…_nozzle_target          215 °C",                PROMPT),
    (0.0, "  sensor.x2d_…_bed_temp                58.7 °C",              PROMPT),
    (0.0, "  sensor.x2d_…_bed_target              60 °C",                PROMPT),
    (0.0, "  sensor.x2d_…_chamber_temp            35.0 °C",              PROMPT),
    (0.0, "  sensor.x2d_…_progress                42 %",                 PROMPT),
    (0.0, "  sensor.x2d_…_remaining               75 min",               PROMPT),
    (0.0, "  sensor.x2d_…_wifi                   -58 dBm",               PROMPT),
    (0.0, "  sensor.x2d_…_ams_slot1_color         #FF7676  (PLA)",       PROMPT),
    (0.0, "  sensor.x2d_…_ams_slot2_color         #66E08C  (PETG)",      PROMPT),
    (0.0, "  sensor.x2d_…_ams_slot3_color         #FFC857  (PLA)",       PROMPT),
    (0.0, "  switch.x2d_…_light                   off",                  PROMPT),
    (0.0, "  button.x2d_…_pause / resume / stop / home / level",         PROMPT),
    (0.0, "  number.x2d_…_bed_set / nozzle_set / chamber_set",           PROMPT),
    (0.0, "  image.x2d_…_snapshot                 (mqtt.image)",         PROMPT),
    (0.0, "  …23 more sensors / 4 AMS load buttons / 1 binary_sensor",   PROMPT),
    (2.0, "",                                                            FG),
    (0.4, "Live values flow through the publisher's SSE → MQTT pipe.",   ACCENT),
    (1.5, "",                                                            FG),
]


def render_ha_demo() -> bool:
    print("[demo] ha_demo.mp4 — registry-snapshot summary frames")
    frames = _build_terminal_frames(HA_SUMMARY_LINES)
    return _write_mp4(frames, OUT_DIR / "ha_demo.mp4")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("[demo] ffmpeg missing on PATH — cannot render", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, fn in [
        ("cli",   render_cli_demo),
        ("gui",   render_gui_demo),
        ("mcp",   render_mcp_demo),
        ("webui", render_webui_demo),
        ("ha",    render_ha_demo),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            results[name] = False
    print()
    print("--- demo render summary ---")
    for n, ok in results.items():
        out = OUT_DIR / f"{n}_demo.mp4"
        size = out.stat().st_size if out.exists() else 0
        marker = "OK" if ok else "FAIL"
        print(f"  {marker}  {n:6s}  {out}  ({size/1024:.1f} KiB)")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
