"""End-to-end test for the timelapse recorder (item #56).

Synthetic camera serves a JPEG; recorder polls it every 0.3 s during
"running" state; we drive on_state through RUNNING → FINISH and
verify:

* frames captured under ~/.x2d/timelapses/<printer>/<job>/NNNN.jpg
* meta.json updated (subtask_name, frame_count, started, ended)
* list_jobs() / list_frames() return what we wrote
* stitch() runs ffmpeg → timelapse.mp4 with H.264, mp4_ready True
* one-click roundtrip: a completed job's mp4 plays back (header bytes)
"""

from __future__ import annotations

import http.server
import io
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from runtime.timelapse.recorder import (
    TimelapseRecorder, _is_print_active)


def _make_jpeg(seed: int = 0) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (320, 240), (50 + seed % 200, 80, 150))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


_FRAME = _make_jpeg(0)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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

    # ---- _is_print_active unit checks ----
    check("active: gcode_state=RUNNING → True",
          _is_print_active({"print": {"gcode_state": "RUNNING"}}))
    check("active: gcode_state=PREPARE → True",
          _is_print_active({"print": {"gcode_state": "PREPARE"}}))
    check("active: gcode_state=FINISH → False",
          not _is_print_active({"print": {"gcode_state": "FINISH"}}))
    check("active: percent=42 → True",
          _is_print_active({"print": {"mc_percent": 42}}))
    check("active: percent=100 → False",
          not _is_print_active({"print": {"mc_percent": 100}}))
    check("active: empty state → False",
          not _is_print_active({}))

    cam_port = _free_port()
    _start_camera(cam_port)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "timelapses"
        rec = TimelapseRecorder(
            snapshot_url=f"http://127.0.0.1:{cam_port}/snapshot.jpg",
            root=root,
            interval_s=0.3)

        # No jobs yet
        check("list_jobs() empty initially",
              rec.list_jobs() == [])

        # Drive state: RUNNING for ~2s, then FINISH
        running_state = {"print": {"gcode_state": "RUNNING",
                                    "mc_percent": 50,
                                    "subtask_name": "rumi_frame.gcode.3mf"}}
        finish_state  = {"print": {"gcode_state": "FINISH",
                                    "mc_percent": 100,
                                    "subtask_name": "rumi_frame.gcode.3mf"}}

        rec.on_state("studio", running_state)
        # Allow capture to fire several times.
        time.sleep(2.0)
        rec.on_state("studio", finish_state)
        time.sleep(0.4)

        jobs = rec.list_jobs()
        check("list_jobs() returns 1 job after run",
              len(jobs) == 1, detail=str(jobs))
        if not jobs:
            print("FAILED early"); return 1
        job = jobs[0]
        check("job.printer=studio", job["printer"] == "studio",
              detail=str(job))
        check("job.subtask_name persisted",
              job["subtask_name"] == "rumi_frame.gcode.3mf",
              detail=str(job))
        check("job.frame_count >= 3",
              job["frame_count"] >= 3, detail=f"got {job['frame_count']}")
        check("job.started > 0", job["started"] > 0)
        check("job.ended > job.started",
              job["ended"] > job["started"],
              detail=f"started={job['started']} ended={job['ended']}")

        frames = rec.list_frames("studio", job["job_id"])
        check("list_frames() count matches frame_count",
              len(frames) == job["frame_count"],
              detail=f"frames={frames} meta={job['frame_count']}")
        check("first frame is 00001.jpg",
              frames and frames[0] == "00001.jpg",
              detail=str(frames[:3]))

        # Frame-path traversal safety
        bad = rec.frame_path("studio", job["job_id"], "../../etc/passwd")
        check("frame_path rejects traversal", bad is None,
              detail=str(bad))

        # Real frame fetch
        ok_path = rec.frame_path("studio", job["job_id"], frames[0])
        check("frame_path returns existing JPEG",
              ok_path is not None and ok_path.exists())
        if ok_path:
            head = ok_path.read_bytes()[:4]
            check("frame is valid JFIF", head[:3] == b"\xff\xd8\xff",
                  detail=str(head))

        # ---- stitch via ffmpeg ----
        result = rec.stitch("studio", job["job_id"])
        if shutil.which("ffmpeg") is None:
            check("stitch reports ffmpeg missing gracefully",
                  result["ok"] is False
                  and "ffmpeg" in result.get("error", "").lower())
        else:
            check("stitch returns ok=True", result["ok"],
                  detail=str(result))
            check("stitch frames count matches list_frames()",
                  result["frames"] == len(frames))
            check("stitch mp4 size > 0", result["size"] > 0,
                  detail=str(result))
            mp4 = rec.mp4_path("studio", job["job_id"])
            check("mp4_path returns existing file",
                  mp4 is not None and mp4.exists())
            if mp4:
                head = mp4.read_bytes()[:8]
                # MP4 ftyp box starts at offset 4
                check("mp4 starts with ftyp box",
                      head[4:8] == b"ftyp",
                      detail=str(head))
            jobs_after = rec.list_jobs()
            check("jobs[0].mp4_ready=True after stitch",
                  jobs_after[0]["mp4_ready"], detail=str(jobs_after[0]))

        rec.stop_all()

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — timelapse recorder (#56)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
