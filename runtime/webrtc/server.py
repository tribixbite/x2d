"""aiortc-based WebRTC server (item #45).

Pulls JPEG frames from the existing camera daemon (running on a separate
port — typically 8766) and re-publishes them as an MJPEG-decoded
``av.VideoFrame`` stream over WebRTC. Browsers connect via:

* GET ``/cam.webrtc.html`` — the viewer page (static)
* GET ``/cam.webrtc.js``   — the client signaling script (static)
* POST ``/cam.webrtc/offer`` — SDP offer/answer exchange
* GET ``/cam.jpg``         — proxied snapshot for fallback
* GET ``/healthz``         — JSON liveness check

Sub-second latency goal: JPEG → ``av.CodecContext.decode()`` →
``VideoFrame`` → aiortc encoder (typically VP8) → SRTP. The dominant
delay is the camera daemon's ffmpeg JPEG cadence (~33 ms at 30 fps);
the WebRTC pipeline itself adds <100 ms in normal conditions.

Usage::

    python3.12 x2d_bridge.py camera --bind 127.0.0.1:8766 &
    python3.12 x2d_bridge.py webrtc --bind 127.0.0.1:8765 \\
        --camera-url http://127.0.0.1:8766
    # browser → http://localhost:8765/cam.webrtc.html

Env vars:

* ``X2D_WEBRTC_FRAME_HZ``  — JPEG poll rate (default 30)
* ``X2D_WEBRTC_ICE_STUN``  — STUN server URL list, comma-separated
                             (default ``stun:stun.l.google.com:19302``)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

import av  # for VideoFrame + MJPEG decode
from av import VideoFrame

LOG = logging.getLogger("x2d.webrtc")

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"

DEFAULT_FRAME_HZ = float(os.environ.get("X2D_WEBRTC_FRAME_HZ", "30"))
DEFAULT_STUN = os.environ.get(
    "X2D_WEBRTC_ICE_STUN", "stun:stun.l.google.com:19302")


class _LatestFrameStore:
    """Single shared frame buffer the camera-poll task writes to and
    every CameraTrack reads from."""

    def __init__(self) -> None:
        self.jpeg: bytes = b""
        self.ts: float = 0.0
        self._cv = asyncio.Condition()

    async def put(self, jpeg: bytes) -> None:
        async with self._cv:
            self.jpeg = jpeg
            self.ts = time.time()
            self._cv.notify_all()

    async def wait_for_new(self, after_ts: float) -> tuple[bytes, float]:
        async with self._cv:
            while self.ts <= after_ts or not self.jpeg:
                await self._cv.wait()
            return self.jpeg, self.ts


class CameraVideoTrack(MediaStreamTrack):
    """aiortc track that emits MJPEG-decoded frames at the camera daemon's
    cadence. One per RTCPeerConnection."""

    kind = "video"

    def __init__(self, store: _LatestFrameStore, frame_hz: float) -> None:
        super().__init__()
        self._store = store
        # Stateful MJPEG decoder. PyAV reuses internal scratch buffers
        # across decode() calls so per-frame allocation is minimal.
        self._codec = av.CodecContext.create("mjpeg", "r")
        self._last_ts = 0.0
        self._pts = 0
        # 90kHz time base is the WebRTC-canonical clock for video tracks.
        self._time_base_num = 1
        self._time_base_den = 90_000
        self._tick = 1.0 / frame_hz
        self._frame_period_pts = int(self._time_base_den * self._tick)

    async def recv(self) -> VideoFrame:
        jpeg, ts = await self._store.wait_for_new(self._last_ts)
        self._last_ts = ts
        try:
            packet = av.Packet(jpeg)
            frames = self._codec.decode(packet)
        except (av.AVError, ValueError) as e:
            LOG.warning("MJPEG decode failed (%d B): %s; emitting black frame",
                        len(jpeg), e)
            frames = []
        if frames:
            frame = frames[0]
        else:
            # If decode fails, emit a 320x240 black frame so the WebRTC
            # session doesn't stall.
            import numpy as np
            frame = VideoFrame.from_ndarray(
                np.zeros((240, 320, 3), dtype="uint8"), format="rgb24")
        # Advance pts by one frame interval (90kHz units).
        self._pts += self._frame_period_pts
        frame.pts = self._pts
        from fractions import Fraction
        frame.time_base = Fraction(self._time_base_num, self._time_base_den)
        return frame


class WebRTCServer:
    def __init__(self, *, camera_url: str, frame_hz: float = DEFAULT_FRAME_HZ,
                 stun_servers: Optional[list[str]] = None) -> None:
        self.camera_url = camera_url.rstrip("/")
        self.frame_hz = frame_hz
        self.store = _LatestFrameStore()
        self.peer_connections: set[RTCPeerConnection] = set()
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self._http_session: aiohttp.ClientSession | None = None
        if stun_servers is None:
            stun_servers = [DEFAULT_STUN] if DEFAULT_STUN else []
        self._stun = stun_servers

    async def start_polling(self) -> None:
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=4))
        self._poll_task = asyncio.create_task(self._poll_loop(),
                                               name="webrtc-poll")

    async def stop(self) -> None:
        self._stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._http_session:
            await self._http_session.close()
        # Close all peer connections.
        coros = [pc.close() for pc in list(self.peer_connections)]
        await asyncio.gather(*coros, return_exceptions=True)
        self.peer_connections.clear()

    async def _poll_loop(self) -> None:
        """Pull /cam.jpg from the upstream camera daemon at frame_hz."""
        period = 1.0 / self.frame_hz
        url = f"{self.camera_url}/cam.jpg"
        backoff = period
        while not self._stop.is_set():
            t0 = time.time()
            try:
                async with self._http_session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data and len(data) > 100:
                            await self.store.put(data)
                            backoff = period
                        else:
                            LOG.debug("empty cam.jpg body (%d B)", len(data))
                    else:
                        LOG.warning("cam.jpg status=%d", resp.status)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                LOG.warning("cam.jpg fetch failed: %s", e)
                # Exponential backoff on persistent failures, capped at 5s.
                backoff = min(backoff * 1.5, 5.0)
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.0, backoff - elapsed))

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def handle_offer(self, request: web.Request) -> web.Response:
        try:
            params = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if "sdp" not in params or "type" not in params:
            return web.json_response(
                {"error": "body must include 'sdp' and 'type'"}, status=400)

        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        config = RTCConfiguration(iceServers=[
            RTCIceServer(urls=u) for u in self._stun
        ]) if self._stun else None
        pc = RTCPeerConnection(configuration=config)
        self.peer_connections.add(pc)

        @pc.on("connectionstatechange")
        async def _on_conn_state() -> None:
            LOG.info("pc state=%s (%d active)",
                     pc.connectionState, len(self.peer_connections))
            if pc.connectionState in ("failed", "closed", "disconnected"):
                self.peer_connections.discard(pc)
                try:
                    await pc.close()
                except Exception:
                    pass

        # Add the camera track as a sendonly stream.
        track = CameraVideoTrack(self.store, self.frame_hz)
        pc.addTrack(track)

        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return web.json_response({
            "sdp":  pc.localDescription.sdp,
            "type": pc.localDescription.type,
        })

    async def handle_health(self, _request: web.Request) -> web.Response:
        age = time.time() - self.store.ts if self.store.ts else float("inf")
        body = {
            "ok":             self.store.ts > 0 and age < 5.0,
            "active_peers":   len(self.peer_connections),
            "last_frame_age": None if self.store.ts == 0 else round(age, 3),
            "last_frame_b":   len(self.store.jpeg),
            "frame_hz":       self.frame_hz,
            "camera_url":     self.camera_url,
        }
        return web.json_response(body, status=200 if body["ok"] else 503)

    async def handle_camjpg(self, _request: web.Request) -> web.Response:
        if not self.store.jpeg:
            return web.Response(status=503, text="no frame yet")
        return web.Response(body=self.store.jpeg,
                             content_type="image/jpeg")

    async def handle_static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name")
        if name not in ("cam.webrtc.html", "cam.webrtc.js"):
            return web.Response(status=404)
        path = WEB_DIR / name
        if not path.exists():
            return web.Response(status=404,
                                 text=f"static asset missing: {path}")
        ctype = "text/html" if name.endswith(".html") else "application/javascript"
        return web.Response(body=path.read_bytes(), content_type=ctype)

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self.handle_health)
        app.router.add_get("/cam.jpg", self.handle_camjpg)
        app.router.add_post("/cam.webrtc/offer", self.handle_offer)
        app.router.add_get("/{name}", self.handle_static)
        # Convenience: hitting the root redirects to the viewer page.
        app.router.add_get("/", lambda r: web.HTTPFound("/cam.webrtc.html"))
        return app


async def serve(*, host: str, port: int, camera_url: str,
                frame_hz: float = DEFAULT_FRAME_HZ,
                stun_servers: Optional[list[str]] = None) -> None:
    server = WebRTCServer(camera_url=camera_url, frame_hz=frame_hz,
                           stun_servers=stun_servers)
    await server.start_polling()
    app = server.make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[x2d-webrtc] listening on http://{host}:{port}/cam.webrtc.html "
          f"(camera={camera_url}, {frame_hz} Hz)", file=sys.stderr,
          flush=True)
    try:
        # Block until cancelled (SIGTERM / SIGINT in the parent).
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
        await runner.cleanup()


def run(*, host: str, port: int, camera_url: str,
        frame_hz: float = DEFAULT_FRAME_HZ,
        stun_servers: Optional[list[str]] = None) -> int:
    """Synchronous entry point used by ``x2d_bridge.py webrtc``."""
    logging.basicConfig(level=os.environ.get("X2D_WEBRTC_LOG", "WARNING"),
                        format="[%(asctime)s] %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve(host=host, port=port, camera_url=camera_url,
                           frame_hz=frame_hz, stun_servers=stun_servers))
    except KeyboardInterrupt:
        return 130
    return 0
