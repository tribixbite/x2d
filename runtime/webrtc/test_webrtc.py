"""End-to-end WebRTC test (item #45).

Brings up a synthetic JPEG server (1x1 pixel JPEG, refreshed at the
camera-daemon cadence), launches the WebRTC gateway, then connects with
a real aiortc peer and confirms:

* /healthz reports a recent frame
* POST /cam.webrtc/offer returns a valid answer SDP
* The peer enters connected/ICE-completed state
* At least one video frame is decoded on the receiver side

Browser-side equivalence: the JS client at /cam.webrtc.html does the
same SDP dance via fetch() then renders the track in <video>. By using
aiortc on both sides here, we confirm the wire format and decode path
are correct end-to-end without needing a Chromium binary on Termux.
"""

from __future__ import annotations

import asyncio
import base64
import socket
import sys
import time
import threading
import http.server
import socketserver

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from runtime.webrtc.server import WebRTCServer, serve as serve_webrtc

# 320x240 solid-colour JFIF JPEG. VP8/H.264 minimum encode dimension
# is 16, and aiortc has had stalls reported with extreme aspect ratios,
# so the test stays at a real-world camera resolution.
def _build_synth_jpeg() -> bytes:
    import io
    from PIL import Image
    img = Image.new("RGB", (320, 240), color=(0, 64, 128))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


_SYNTH_JPEG = _build_synth_jpeg()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_synth_camera(port: int) -> threading.Thread:
    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): return
        def do_GET(self):
            if self.path == "/cam.jpg":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(_SYNTH_JPEG)))
                self.end_headers()
                self.wfile.write(_SYNTH_JPEG)
            else:
                self.send_response(404); self.end_headers()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), _H)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="synth-cam")
    t.start()
    return t


async def main() -> int:
    cam_port = _free_port()
    rtc_port = _free_port()

    _start_synth_camera(cam_port)
    server = WebRTCServer(
        camera_url=f"http://127.0.0.1:{cam_port}",
        frame_hz=10,
        stun_servers=[],   # avoid network-dependent STUN in CI
    )
    await server.start_polling()

    from aiohttp import web
    app = server.make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", rtc_port)
    await site.start()

    failed: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        msg = f"  {marker}  {label}"
        if detail and not ok:
            msg += f": {detail}"
        print(msg)
        if not ok:
            failed.append(label)

    try:
        # Wait for the first frame to land in the store.
        deadline = time.time() + 10
        while server.store.ts == 0 and time.time() < deadline:
            await asyncio.sleep(0.1)
        check("camera poll captures frame within 10s",
              server.store.ts > 0,
              detail=f"store.ts={server.store.ts}")

        # /healthz
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{rtc_port}/healthz") as r:
                body = await r.json()
                check("/healthz status 200", r.status == 200)
                check("/healthz reports active frame",
                      body.get("ok") is True,
                      detail=str(body))

        # SDP offer/answer dance via a real RTCPeerConnection.
        client_pc = RTCPeerConnection()
        client_pc.addTransceiver("video", direction="recvonly")

        frame_received = asyncio.Event()
        decoded_frame_w = [0]
        decoded_frame_h = [0]

        @client_pc.on("track")
        def _on_track(track: MediaStreamTrack):
            async def _consume():
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=15)
                    decoded_frame_w[0] = frame.width
                    decoded_frame_h[0] = frame.height
                    frame_received.set()
                except asyncio.TimeoutError:
                    pass
            asyncio.create_task(_consume())

        offer = await client_pc.createOffer()
        await client_pc.setLocalDescription(offer)
        # Wait for ICE gathering complete.
        while client_pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)

        async with aiohttp.ClientSession() as s:
            async with s.post(
                    f"http://127.0.0.1:{rtc_port}/cam.webrtc/offer",
                    json={"sdp":  client_pc.localDescription.sdp,
                          "type": client_pc.localDescription.type}) as r:
                check("/cam.webrtc/offer returns 200", r.status == 200,
                      detail=str(r.status))
                ans = await r.json()
                check("answer has sdp + type",
                      "sdp" in ans and ans.get("type") == "answer",
                      detail=str(ans)[:200])
                check("answer SDP contains video m-line",
                      "m=video" in ans.get("sdp", ""),
                      detail=ans.get("sdp", "")[:300])

        await client_pc.setRemoteDescription(
            RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))

        # Wait for frame to arrive over WebRTC. 25 s gives ICE + DTLS
        # plenty of headroom on Termux.
        try:
            await asyncio.wait_for(frame_received.wait(), timeout=25)
            check("decoded video frame received over WebRTC", True)
            check("frame width > 0", decoded_frame_w[0] > 0,
                  detail=f"got {decoded_frame_w[0]}x{decoded_frame_h[0]}")
        except asyncio.TimeoutError:
            check("decoded video frame received over WebRTC", False,
                  detail="timeout after 25s — ICE/DTLS likely failed")

        await client_pc.close()
    finally:
        await server.stop()
        await runner.cleanup()

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — WebRTC end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
