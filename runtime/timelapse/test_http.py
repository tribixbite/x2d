"""HTTP integration test for the timelapse routes (item #56).

Walks the daemon side of the API:
  GET  /timelapses                                 → {"jobs":[...]}
  GET  /timelapses/<printer>/<job>                 → {"frames":[...], ...}
  GET  /timelapses/<printer>/<job>/<frame>.jpg     → JPEG bytes
  POST /timelapses/<printer>/<job>/stitch          → ffmpeg → mp4
  GET  /timelapses/<printer>/<job>/timelapse.mp4   → mp4 bytes

A real `TimelapseRecorder` is wired into `_serve_http` with a
synthetic snapshot URL; we drive on_state through RUNNING → FINISH
to populate frames before exercising the routes.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import x2d_bridge
from runtime.timelapse.recorder import TimelapseRecorder


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_jpeg() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (160, 120), (40, 80, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


_FRAME = _make_jpeg()


def _start_camera(port: int):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): return
        def do_GET(self):
            if self.path.startswith("/snapshot.jpg") \
               or self.path.startswith("/cam.jpg"):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(_FRAME)))
                self.end_headers()
                self.wfile.write(_FRAME)
            else:
                self.send_response(404); self.end_headers()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name=f"snap-{port}").start()


def main() -> int:
    failed: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    cam_port = _free_port()
    bridge_port = _free_port()
    _start_camera(cam_port)

    with tempfile.TemporaryDirectory() as tmp:
        rec = TimelapseRecorder(
            snapshot_url=f"http://127.0.0.1:{cam_port}/snapshot.jpg",
            root=Path(tmp) / "tl",
            interval_s=0.3)

        threading.Thread(
            target=x2d_bridge._serve_http,
            kwargs={
                "bind":          f"127.0.0.1:{bridge_port}",
                "get_state":     lambda _p: {"print": {"nozzle_temper": 27.0}},
                "get_last_ts":   lambda _p: time.time() - 1,
                "max_staleness": 30.0,
                "auth_token":    None,
                "printer_names": ["studio"],
                "clients":       {"studio": object()},
                "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
                "queue_mgr":     None,
                "timelapse_rec": rec,
            },
            daemon=True, name="tl-http-bridge",
        ).start()
        time.sleep(0.4)

        # Drive a print: RUNNING for ~2s, then FINISH.
        rec.on_state("studio", {"print": {"gcode_state": "RUNNING",
                                            "mc_percent": 50,
                                            "subtask_name": "demo.gcode.3mf"}})
        time.sleep(2.0)
        rec.on_state("studio", {"print": {"gcode_state": "FINISH",
                                            "mc_percent": 100,
                                            "subtask_name": "demo.gcode.3mf"}})
        time.sleep(0.3)

        base = f"http://127.0.0.1:{bridge_port}"

        # ---- GET /timelapses ----
        with urllib.request.urlopen(base + "/timelapses", timeout=5) as r:
            data = json.loads(r.read())
        check("GET /timelapses status 200", r.status == 200)
        check("/timelapses returns 1 job",
              len(data.get("jobs", [])) == 1, detail=str(data))
        if not data.get("jobs"):
            print("FAIL: no jobs"); return 1
        job = data["jobs"][0]
        check("job.printer = studio",
              job["printer"] == "studio", detail=str(job))
        check("job.subtask_name persisted",
              job["subtask_name"] == "demo.gcode.3mf", detail=str(job))
        check("job.frame_count >= 3",
              job["frame_count"] >= 3, detail=str(job["frame_count"]))

        printer = job["printer"]
        job_id  = job["job_id"]

        # ---- GET /timelapses/<p>/<j> ----
        url = (f"{base}/timelapses/{urllib.parse.quote(printer)}/"
               f"{urllib.parse.quote(job_id)}")
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        check(f"GET {url} status 200", r.status == 200)
        check("frames list returned",
              len(data.get("frames", [])) == job["frame_count"],
              detail=str(data))

        # ---- GET frame ----
        frame = data["frames"][0]
        with urllib.request.urlopen(f"{url}/{frame}", timeout=5) as r:
            body = r.read()
        check(f"GET .../<frame> status 200 + JFIF",
              r.status == 200 and body[:3] == b"\xff\xd8\xff",
              detail=f"len={len(body)} head={body[:8]!r}")
        check("frame size matches synthetic JPEG",
              body == _FRAME)

        # ---- frame traversal denied ----
        try:
            urllib.request.urlopen(f"{url}/../meta.json", timeout=5)
            check("traversal returns 404", False,
                  detail="unexpected 200")
        except urllib.error.HTTPError as e:
            check("traversal returns 404", e.code == 404,
                  detail=str(e.code))

        # ---- POST stitch ----
        if shutil.which("ffmpeg") is None:
            print("[skip] ffmpeg missing — cannot exercise stitch path")
        else:
            req = urllib.request.Request(f"{url}/stitch", method="POST",
                                          data=json.dumps({"fps": 24}).encode(),
                                          headers={"Content-Type":
                                                    "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                stitch = json.loads(r.read())
            check("POST /stitch status 200", r.status == 200, detail=str(stitch))
            check("stitch result ok=True", stitch.get("ok"),
                  detail=str(stitch))
            check("stitch frames matches",
                  stitch.get("frames") == job["frame_count"],
                  detail=str(stitch))

            # ---- GET timelapse.mp4 ----
            with urllib.request.urlopen(f"{url}/timelapse.mp4",
                                          timeout=5) as r:
                mp4 = r.read()
            check("GET timelapse.mp4 status 200",
                  r.status == 200 and r.headers.get("Content-Type")
                  == "video/mp4")
            check("mp4 has ftyp box at offset 4",
                  mp4[4:8] == b"ftyp", detail=str(mp4[:12]))

        rec.stop_all()

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — timelapse HTTP routes (#56)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
