# Thin web UI

The bridge daemon serves a mobile-friendly remote-control surface at
`http://<host>:8765/`. No build step, no framework — three static
files in `web/` totalling ~22 KB.

## Layout

| Card             | What it shows / does |
|------------------|----------------------|
| Header           | printer name, connection pill, last-update timestamp, printer dropdown (multi-printer) |
| Temperatures     | nozzle, bed, chamber — live via SSE |
| Job              | filename, % progress bar, current/total layer, ETA |
| Camera           | three transport tabs: snapshot poll / native HLS / WebRTC |
| Controls         | pause / resume / stop, light on / off / flashing, heat presets (PLA / PETG / cool down) |
| AMS slots        | per-slot color swatches with material + matched filament profile name (#58) |
| Assistant        | chat panel powered by the local MCP toolset (#57) |
| Print queue      | drag-and-drop reorder, per-row cancel (#55) |
| Timelapses       | job picker, sampled thumbnails, "stitch MP4" + "play" (#56) |
| Bridge log       | last 60 actions performed via the UI |

## Start

```bash
python3.12 x2d_bridge.py daemon \
    --http       0.0.0.0:8765 \
    --queue \
    --timelapse \
    --auth-token "$(openssl rand -hex 32)"
```

Then in any modern browser: `http://<host>:8765/`. On first visit,
the login page (`/login.html`) prompts for the token; it's persisted
to `localStorage` + a `x2d_token` cookie (the latter is what
EventSource uses since the JS API can't set headers on SSE).

For multi-printer setups, the header gains a printer dropdown that
re-targets every card.

## HTTP routes the UI uses

```
GET  /                      → /index.html
GET  /index.html /index.js /index.css
GET  /login.html /login.js
GET  /auth/info             → {auth_required, cookie_name}
GET  /auth/check            → 200 if bearer/cookie auth works
GET  /printers              → ["", "studio", "garage", ...]
GET  /state.events          → SSE: state JSON every 1s
GET  /state                 → snapshot of latest state
GET  /healthz               → uptime probe
GET  /metrics               → Prometheus exposition
POST /control/{pause,resume,stop,light,temp,ams_load,gcode}
GET  /cam.jpg /cam.mjpeg /cam.m3u8 /cam.webrtc.html
GET  /snapshot.jpg          → proxy of /cam.jpg for HA's image card
GET  /queue                 → list of pending+running jobs
POST /queue/{add,cancel,remove,move}
GET  /timelapses
GET  /timelapses/<p>/<j>    → frame list
GET  /timelapses/<p>/<j>/<NNNN>.jpg
POST /timelapses/<p>/<j>/stitch
GET  /timelapses/<p>/<j>/timelapse.mp4
GET  /colorsync/match?color=…&material=…
GET  /colorsync/state
POST /assistant/chat
```

## Auth

Loopback binds (127.0.0.1) stay open without `--auth-token` (the
single-user local case). Any non-loopback bind requires
`--auth-token` set; clients present it as either
`Authorization: Bearer <token>` (for fetch/curl) or `Cookie:
x2d_token=<token>` (for EventSource). See [`AUTH` section in this
doc's source](../runtime/webui/test_auth.py) for the full check
matrix — 28/28 PASS in CI.

## Mobile

The CSS uses a single-column layout below 720 CSS px (S25 Ultra
portrait at DPR 2.625 = 412 px wide → single-column kicks in).
Touch targets are ≥44 px per Apple HIG / Google MD3. Headless-
chromium screenshots at the S25 Ultra viewport committed at
`docs/webui-{portrait,landscape}-s25.png`.

## Test harnesses

```bash
PYTHONPATH=. python3.12 runtime/webui/test_webui.py     # 33/33 PASS
PYTHONPATH=. python3.12 runtime/webui/test_auth.py      # 28/28 PASS
PYTHONPATH=. python3.12 runtime/webui/test_mobile.py    # 14/14 PASS
```
