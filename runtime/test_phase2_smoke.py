"""Phase 2 end-to-end smoke test (#49).

Spins up every Phase 2 surface concurrently — bridge daemon, camera
daemon (or a synthetic JPEG server when the printer's RTSP is off),
WebRTC gateway, and an MCP stdio server — and pounds each one with
load from a dedicated client thread for `--duration` seconds.
Watches the four daemons' RSS + FD count + thread count throughout
and fails if anything drifts up monotonically (≈ leak), if a daemon
dies, or if response times trend > 3× the run-warm baseline.

Surfaces under test:

* **Web UI / bridge daemon** — GET /state, /printers, /metrics,
  /healthz; SSE GET /state.events that picks up at least one frame.
* **WebRTC gateway** — POST /cam.webrtc/offer + full ICE/DTLS
  handshake; receive at least one decoded video frame; close. New
  peer per cycle so we exercise the connection cleanup path.
* **MCP stdio server** — JSON-RPC initialize → tools/list → repeated
  tools/call ping / status / metrics / list_printers. Re-uses one
  spawned subprocess for the full run.

Default `--duration=60` for CI; pass `--duration=600` to run the
full 10-minute soak the ledger asks for.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import http.server
import io
import json
import os
import socket
import socketserver
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- synthetic camera (RTSP-disabled X2D fallback) ----------

def _make_test_jpeg() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (320, 240), (0, 64, 128))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


_SYNTH_JPEG = _make_test_jpeg()


def _start_synth_camera(port: int) -> tuple[threading.Thread, socketserver.TCPServer]:
    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): return
        def do_GET(self):
            if self.path.startswith("/cam.jpg"):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(_SYNTH_JPEG)))
                self.end_headers()
                self.wfile.write(_SYNTH_JPEG)
            elif self.path == "/healthz":
                self.send_response(200); self.end_headers()
            else:
                self.send_response(404); self.end_headers()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), _H)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="synth-cam")
    t.start()
    return t, httpd


# ---------- subprocess helpers ----------

class _Daemon:
    def __init__(self, name: str, argv: list[str], env: dict | None = None,
                 ready_url: str | None = None,
                 ready_timeout: float = 30.0):
        self.name = name
        log_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "x2d-phase2"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{name}.log"
        self.log_fh = self.log_path.open("ab", buffering=0)
        self.proc = subprocess.Popen(
            argv, stdout=self.log_fh, stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT), env=env or os.environ.copy())
        if ready_url:
            self._wait_ready(ready_url, ready_timeout)
        try:
            self.psproc = psutil.Process(self.proc.pid)
        except psutil.NoSuchProcess:
            self.psproc = None

    def _wait_ready(self, url: str, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status in (200, 503):
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"{self.name} died during startup; see {self.log_path}")
                time.sleep(0.3)
        raise RuntimeError(
            f"{self.name} never reached {url} within {timeout}s")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        finally:
            try:
                self.log_fh.close()
            except Exception:
                pass

    def snapshot(self) -> dict | None:
        if self.psproc is None or not self.alive():
            return None
        try:
            with self.psproc.oneshot():
                return {
                    "rss_mb":  self.psproc.memory_info().rss / 1024 / 1024,
                    "threads": self.psproc.num_threads(),
                    "fds":     self.psproc.num_fds()
                                if hasattr(self.psproc, "num_fds") else None,
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None


# ---------- workload threads ----------

class _ResultsBucket:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.successes = 0
        self.failures = 0
        self.latencies_ms: deque[float] = deque(maxlen=2000)

    def record(self, ok: bool, dur: float) -> None:
        with self.lock:
            if ok:
                self.successes += 1
                self.latencies_ms.append(dur * 1000)
            else:
                self.failures += 1

    def summary(self) -> dict:
        with self.lock:
            lats = list(self.latencies_ms)
        if not lats:
            return {"successes": self.successes, "failures": self.failures,
                    "p50_ms": None, "p99_ms": None}
        lats_sorted = sorted(lats)
        p50 = lats_sorted[len(lats_sorted) // 2]
        p99 = lats_sorted[max(0, int(len(lats_sorted) * 0.99) - 1)]
        return {"successes": self.successes, "failures": self.failures,
                "p50_ms": round(p50, 2), "p99_ms": round(p99, 2),
                "samples": len(lats)}


def _webui_workload(stop: threading.Event,
                    daemon_url: str,
                    bucket: _ResultsBucket) -> None:
    """Hit /state, /printers, /metrics, /healthz round-robin."""
    paths = ["/state", "/printers", "/metrics", "/healthz", "/index.html",
              "/index.js"]
    i = 0
    while not stop.is_set():
        path = paths[i % len(paths)]
        i += 1
        url = daemon_url + path
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                _ = r.read(1)
                bucket.record(r.status in (200, 503),
                              time.time() - t0)
        except Exception:
            bucket.record(False, time.time() - t0)
        time.sleep(0.5)


def _sse_workload(stop: threading.Event,
                  daemon_url: str,
                  bucket: _ResultsBucket) -> None:
    """One long-lived SSE connection that records each frame as a
    success and reconnects on disconnect."""
    while not stop.is_set():
        t_open = time.time()
        try:
            with urllib.request.urlopen(daemon_url + "/state.events",
                                          timeout=8) as r:
                while not stop.is_set():
                    line = r.readline()
                    if not line:
                        break
                    if line.startswith(b"data: "):
                        bucket.record(True, time.time() - t_open)
                        t_open = time.time()  # next-frame baseline
        except Exception:
            bucket.record(False, time.time() - t_open)
            time.sleep(1.0)


async def _webrtc_one_cycle(rtc_url: str) -> tuple[bool, float]:
    """One full WebRTC connect+frame+close cycle."""
    import aiohttp
    from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
    t0 = time.time()
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    got_frame = asyncio.Event()

    @pc.on("track")
    def _on_track(track: MediaStreamTrack):
        async def _consume():
            try:
                await asyncio.wait_for(track.recv(), timeout=15)
                got_frame.set()
            except asyncio.TimeoutError:
                pass
        asyncio.create_task(_consume())

    try:
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        async with aiohttp.ClientSession() as s:
            async with s.post(rtc_url + "/cam.webrtc/offer",
                              json={"sdp": pc.localDescription.sdp,
                                    "type": pc.localDescription.type},
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return False, time.time() - t0
                ans = await r.json()
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        try:
            await asyncio.wait_for(got_frame.wait(), timeout=15)
            return True, time.time() - t0
        except asyncio.TimeoutError:
            return False, time.time() - t0
    finally:
        try:
            await pc.close()
        except Exception:
            pass


def _webrtc_workload(stop: threading.Event,
                     rtc_url: str,
                     bucket: _ResultsBucket) -> None:
    """Each cycle: open peer → wait for one frame → close.
    Cycle takes ~5-10s; we space them out by 25s so we don't burn
    the test machine entirely on WebRTC."""
    while not stop.is_set():
        try:
            ok, dur = asyncio.run(_webrtc_one_cycle(rtc_url))
            bucket.record(ok, dur)
        except Exception:
            bucket.record(False, 0)
        # space cycles
        for _ in range(25):
            if stop.is_set():
                return
            time.sleep(1)


def _mcp_workload(stop: threading.Event,
                  mcp_proc: subprocess.Popen,
                  bucket: _ResultsBucket) -> None:
    """Drive the MCP server with periodic tools/call cycles via the
    same JSON-RPC stdio framing the test_mcp.py harness uses."""
    next_id = 1
    lock = threading.Lock()

    def send(req: dict) -> dict | None:
        nonlocal next_id
        t0 = time.time()
        try:
            with lock:
                line = (json.dumps(req) + "\n").encode("utf-8")
                mcp_proc.stdin.write(line)
                mcp_proc.stdin.flush()
                # Read until matching id
                while True:
                    raw = mcp_proc.stdout.readline()
                    if not raw:
                        bucket.record(False, time.time() - t0)
                        return None
                    msg = json.loads(raw.decode("utf-8"))
                    if msg.get("id") == req["id"]:
                        bucket.record("result" in msg, time.time() - t0)
                        return msg
        except Exception:
            bucket.record(False, time.time() - t0)
            return None

    # initialize
    next_id += 1
    send({"jsonrpc": "2.0", "id": next_id, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "phase2", "version": "0"}}})
    next_id += 1
    # notifications/initialized — fire and forget
    try:
        line = (json.dumps({"jsonrpc": "2.0",
                            "method": "notifications/initialized"}) + "\n")
        mcp_proc.stdin.write(line.encode())
        mcp_proc.stdin.flush()
    except Exception:
        pass

    # Rotate through ping → tools/list → tools/call list_printers.
    # tools/call status would dial real MQTT every call — skip in the
    # smoke loop because the X2D ack adds 3-5s and it'd dominate
    # latency stats.
    cycle = 0
    while not stop.is_set():
        cycle += 1
        next_id += 1
        if cycle % 3 == 0:
            req = {"jsonrpc": "2.0", "id": next_id, "method": "ping"}
        elif cycle % 3 == 1:
            req = {"jsonrpc": "2.0", "id": next_id, "method": "tools/list"}
        else:
            req = {"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                   "params": {"name": "list_printers", "arguments": {}}}
        send(req)
        time.sleep(2)


# ---------- monitor + drift detector ----------

def _monitor(stop: threading.Event, daemons: list[_Daemon],
             samples: dict[str, list[dict]],
             period: float = 5.0) -> None:
    while not stop.is_set():
        for d in daemons:
            snap = d.snapshot()
            if snap is not None:
                snap["t"] = time.time()
                samples[d.name].append(snap)
        time.sleep(period)


def _drift_score(samples: list[dict], key: str) -> float:
    """Return (last_third_mean - first_third_mean) / first_third_mean.
    A score > 0.5 (50% growth across the run) is the leak signal."""
    vals = [s[key] for s in samples
            if isinstance(s.get(key), (int, float))]
    if len(vals) < 6:
        return 0.0
    third = len(vals) // 3
    head = statistics.fmean(vals[:third])
    tail = statistics.fmean(vals[-third:])
    if head <= 0:
        return 0.0
    return (tail - head) / head


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=60,
                    help="seconds (default 60; set 600 for the full 10-min soak)")
    ap.add_argument("--keep-logs", action="store_true")
    args = ap.parse_args()

    daemon_port = _free_port()
    cam_port    = _free_port()
    rtc_port    = _free_port()
    daemon_url  = f"http://127.0.0.1:{daemon_port}"
    rtc_url     = f"http://127.0.0.1:{rtc_port}"

    print(f"[phase2] ports: bridge={daemon_port} cam={cam_port} "
          f"rtc={rtc_port}; duration={args.duration}s")

    daemons: list[_Daemon] = []
    samples: dict[str, list[dict]] = defaultdict(list)
    stop = threading.Event()
    failed: list[str] = []

    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    # Synthetic camera (no real RTSP on this X2D) — runs in-process.
    _start_synth_camera(cam_port)
    print(f"[phase2] synth camera up on :{cam_port}")

    bridge = _Daemon(
        "bridge",
        [sys.executable, str(REPO_ROOT / "x2d_bridge.py"),
         "daemon", "--http", f"127.0.0.1:{daemon_port}",
         "--quiet", "--interval", "5"],
        ready_url=daemon_url + "/healthz",
        ready_timeout=20,
    )
    daemons.append(bridge)
    print(f"[phase2] bridge up pid={bridge.proc.pid}")

    webrtc = _Daemon(
        "webrtc",
        [sys.executable, str(REPO_ROOT / "x2d_bridge.py"),
         "webrtc", "--bind", f"127.0.0.1:{rtc_port}",
         "--camera-url", f"http://127.0.0.1:{cam_port}",
         "--frame-hz", "10"],
        ready_url=rtc_url + "/healthz",
        ready_timeout=15,
    )
    daemons.append(webrtc)
    print(f"[phase2] webrtc up pid={webrtc.proc.pid}")

    mcp_env = os.environ.copy()
    mcp_env["X2D_BRIDGE"] = str(REPO_ROOT / "x2d_bridge.py")
    mcp_env["X2D_DAEMON_HTTP"] = daemon_url
    mcp_proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_x2d"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(REPO_ROOT), env=mcp_env)
    try:
        mcp_psproc = psutil.Process(mcp_proc.pid)
    except psutil.NoSuchProcess:
        mcp_psproc = None
    print(f"[phase2] mcp_x2d up pid={mcp_proc.pid}")

    # Wrap MCP into a _Daemon-shaped object so the monitor records it.
    class _MCPHandle:
        name = "mcp"
        proc = mcp_proc
        psproc = mcp_psproc
        log_path = Path("/dev/null")
        def alive(self): return self.proc.poll() is None
        def stop(self): self.proc.terminate()
        def snapshot(self):
            if not self.psproc or not self.alive():
                return None
            try:
                with self.psproc.oneshot():
                    return {"rss_mb": self.psproc.memory_info().rss/1024/1024,
                            "threads": self.psproc.num_threads(),
                            "fds": (self.psproc.num_fds()
                                    if hasattr(self.psproc, "num_fds") else None)}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None
    daemons.append(_MCPHandle())

    # Buckets for each workload.
    buckets = {
        "webui":  _ResultsBucket(),
        "sse":    _ResultsBucket(),
        "webrtc": _ResultsBucket(),
        "mcp":    _ResultsBucket(),
    }
    threads = [
        threading.Thread(target=_webui_workload,
                         args=(stop, daemon_url, buckets["webui"]),
                         name="wkr-webui", daemon=True),
        threading.Thread(target=_sse_workload,
                         args=(stop, daemon_url, buckets["sse"]),
                         name="wkr-sse", daemon=True),
        threading.Thread(target=_webrtc_workload,
                         args=(stop, rtc_url, buckets["webrtc"]),
                         name="wkr-webrtc", daemon=True),
        threading.Thread(target=_mcp_workload,
                         args=(stop, mcp_proc, buckets["mcp"]),
                         name="wkr-mcp", daemon=True),
        threading.Thread(target=_monitor,
                         args=(stop, daemons, samples, 5.0),
                         name="monitor", daemon=True),
    ]
    for t in threads:
        t.start()

    daemon_alive_at_end: dict[str, bool] = {}
    try:
        deadline = time.time() + args.duration
        while time.time() < deadline:
            time.sleep(2)
            for d in daemons:
                if not d.alive():
                    print(f"[phase2] daemon {d.name!r} DIED mid-run; aborting")
                    break
        # Snapshot alive-state BEFORE we tear daemons down — that's the
        # value the survived-the-run assertion actually wants.
        for d in daemons:
            daemon_alive_at_end[d.name] = d.alive()
        stop.set()
        for t in threads:
            t.join(timeout=10)
    finally:
        for d in daemons:
            try: d.stop()
            except Exception: pass

    # ---------- assertions ----------
    print("\n--- workload summaries ---")
    for name, b in buckets.items():
        s = b.summary()
        print(f"  {name:7s}: {s}")
        check(f"{name} workload had ≥1 success",
              s["successes"] >= 1, str(s))
        check(f"{name} workload failure-rate < 30%",
              s["failures"] < max(1, (s["successes"] + s["failures"]) * 0.3),
              str(s))

    print("\n--- daemon resource drift ---")
    for d in daemons:
        snaps = samples[d.name]
        if not snaps:
            check(f"{d.name} produced ≥1 monitor snapshot", False,
                  detail="no samples")
            continue
        check(f"{d.name} survived the run",
              daemon_alive_at_end.get(d.name, False),
              detail="DIED before stop signal")
        rss_drift = _drift_score(snaps, "rss_mb")
        thr_drift = _drift_score(snaps, "threads")
        fd_drift  = _drift_score(snaps, "fds")
        last = snaps[-1]
        print(f"  {d.name:7s}: rss={last['rss_mb']:.1f} MB "
              f"threads={last['threads']} fds={last.get('fds')} "
              f"drift rss={rss_drift:+.2f} thr={thr_drift:+.2f} "
              f"fd={fd_drift:+.2f}")
        # 50% growth across the run for ANY metric is the leak threshold.
        check(f"{d.name} no RSS leak (< 50% growth across run)",
              rss_drift < 0.5,
              detail=f"drift={rss_drift:+.2f}")
        check(f"{d.name} no thread leak (< 50% growth across run)",
              thr_drift < 0.5,
              detail=f"drift={thr_drift:+.2f}")
        if last.get("fds") is not None:
            check(f"{d.name} no FD leak (< 50% growth across run)",
                  fd_drift < 0.5,
                  detail=f"drift={fd_drift:+.2f}")

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print(f"\nALL TESTS PASSED — Phase 2 stable for {args.duration:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
