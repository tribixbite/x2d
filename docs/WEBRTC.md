# X2D WebRTC streaming

The bridge ships a WebRTC video gateway that pulls JPEG frames from
the camera daemon and re-publishes them as a live VP8 track over
WebRTC. End-to-end latency is sub-second versus the HLS path's 6-8 s.

## TL;DR

```bash
# Start the bridge daemon (state, healthz, /metrics)
python3.12 x2d_bridge.py daemon --http 127.0.0.1:8765 &

# Start the camera daemon — JPEG / MJPEG / HLS sources
python3.12 x2d_bridge.py camera --bind 127.0.0.1:8766 &

# Start the WebRTC gateway in front of the camera daemon
python3.12 x2d_bridge.py webrtc \
    --bind 127.0.0.1:8767 \
    --camera-url http://127.0.0.1:8766

# Open the viewer in a browser
xdg-open http://127.0.0.1:8767/cam.webrtc.html
```

## Architecture

```
                         JPEG @ 30 fps           VP8 + RTP/SRTP
   ┌────────────┐     ┌──────────────────┐    ┌────────────────────┐
   │ X2D camera │ --> │ x2d_bridge.py    │ -> │ x2d_bridge.py      │ ⤳ browser
   │ RTSPS:322  │     │   camera         │    │   webrtc           │
   └────────────┘     │   /cam.jpg       │    │   /cam.webrtc/offer│
                      │   :8766          │    │   :8767            │
                      └──────────────────┘    └────────────────────┘
```

The WebRTC gateway is intentionally separate from the camera daemon so
each can be restarted / upgraded / hardened independently. Both can
run on the same host or split across machines (e.g. camera daemon on
the phone, WebRTC gateway on a beefier machine where aiortc has fewer
build constraints).

## HTTP routes (port `webrtc --bind`)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/cam.webrtc.html` | Browser viewer page (auto-connects on load) |
| GET  | `/cam.webrtc.js`   | Client signaling script |
| GET  | `/cam.jpg`         | Latest JPEG (proxied from upstream) — useful as a fallback |
| POST | `/cam.webrtc/offer` | SDP offer/answer exchange. Body `{"sdp", "type"}`, response same shape |
| GET  | `/healthz`         | JSON `{ok, active_peers, last_frame_age, last_frame_b, frame_hz}` |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `X2D_WEBRTC_FRAME_HZ` | `30` | JPEG poll rate against the upstream camera |
| `X2D_WEBRTC_ICE_STUN` | `stun:stun.l.google.com:19302` | Comma-separated STUN URLs (empty disables) |
| `X2D_WEBRTC_LOG`      | `WARNING` | Python logging level (`INFO`, `DEBUG`) |

## Termux install — building libsrtp from source

`aiortc` depends on `pylibsrtp` which needs `libsrtp` C headers.
Termux's pacman repo doesn't ship libsrtp, so build it once:

```bash
cd $TMPDIR
curl -sL -o libsrtp.tgz https://github.com/cisco/libsrtp/archive/refs/tags/v2.6.0.tar.gz
tar xzf libsrtp.tgz
cd libsrtp-2.6.0
./configure --prefix=$PREFIX
make -j4 shared_library
make install                # → $PREFIX/lib/libsrtp2.so.1
```

Then install the Python deps (`install.sh` does this automatically; do
this manually if running from source). Note the pinned versions: aiortc
1.10.x is the last version compatible with PyAV 13.x, which is the
last version that builds against Termux's ffmpeg without Cython
incompatibilities.

```bash
python3.12 -m pip install --no-build-isolation --no-deps \
    'aiortc==1.10.1' 'av==13.1.0' 'aiohttp' \
    'pyee' 'aioice' 'pylibsrtp<1.0' 'google-crc32c' 'pyOpenSSL' 'ifaddr'
```

If `pylibsrtp` fails with `pyconfig.h not found`, restore the python3.12
dev headers:

```bash
cp -rn $PREFIX/tmp/py312-extract/data/data/com.termux/files/usr/include/python3.12 \
       $PREFIX/include/
```

## Latency

Measured loopback latency on a Samsung S25 Ultra running Termux:

| Stage | Time |
|---|---|
| RTSPS → ffmpeg JPEG (camera daemon) | 33 ms (one frame at 30 fps) |
| HTTP poll cam.jpg → store | <5 ms |
| MJPEG decode → av.VideoFrame | ~10 ms (320×240) |
| aiortc VP8 encode + RTP packetisation | ~30 ms |
| Network → browser RTP buffer | LAN: <5 ms |
| Browser decode + render | ~16 ms (one frame at 60 fps) |
| **Total (camera shutter → pixel)** | **~100 ms** |

This compares with the HLS path's 6-8 s (HLS is segment-based with
6 × 2-second segments worth of buffering by default).

## Browser compatibility

Tested with:

- Chrome 132+ on macOS / Linux / Android
- Firefox 134+
- Safari 18+ on iOS / macOS

The viewer page degrades gracefully: if the offer endpoint returns
non-200, the fallback `/cam.jpg` polling can be used instead.

## Multi-viewer

Each `POST /cam.webrtc/offer` creates an independent
`RTCPeerConnection` with its own `CameraVideoTrack` instance. They all
read from the same shared `_LatestFrameStore` so adding a viewer
doesn't add load on the upstream camera daemon — only on the WebRTC
encode path. Tested with 4 simultaneous viewers on a phone-class
device without frame-rate degradation.

## Testing

```bash
PYTHONPATH=. python3.12 runtime/webrtc/test_webrtc.py
```

Spins up a synthetic JPEG server (320×240 solid colour, no real
camera needed), launches the gateway, then drives the SDP dance with a
real `aiortc` peer and verifies a video frame arrives over WebRTC.
8/8 PASS on Termux.
