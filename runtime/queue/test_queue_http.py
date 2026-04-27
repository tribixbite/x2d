"""End-to-end test for the bridge daemon's HTTP queue routes (#55).

Spins up `_serve_http` with a real `QueueManager` (mock dispatch_cb)
and verifies the four POST verbs round-trip to the manager + the
GET /queue snapshot reflects the live state.

Routes covered:
  GET  /queue                         → {"jobs": [...]}
  POST /queue/add  {gcode, printer}   → returns the new job
  POST /queue/cancel {id}             → marks pending → cancelled
  POST /queue/move {id, position}     → re-orders
  POST /queue/remove {id}             → deletes
"""

from __future__ import annotations

import json
import socket
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
from runtime.queue.manager import QueueManager


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url, body=None):
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: payload = json.loads(e.read() or b"{}")
        except Exception: payload = {"_raw": True}
        return e.code, payload


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read() or b"{}")


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

    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory() as tmp:
        # Manager with a no-op dispatch_cb so jobs stay pending.
        mgr = QueueManager(dispatch_cb=lambda j: True,
                            path=Path(tmp) / "queue.json")
        threading.Thread(
            target=x2d_bridge._serve_http,
            kwargs={
                "bind":          f"127.0.0.1:{port}",
                "get_state":     lambda _p: {"print": {"nozzle_temper": 27.0}},
                "get_last_ts":   lambda _p: time.time() - 1,
                "max_staleness": 30.0,
                "auth_token":    None,
                "printer_names": ["studio", "garage"],
                "clients":       {"studio": object(), "garage": object()},
                "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
                "queue_mgr":     mgr,
            },
            daemon=True, name="qhttp-test",
        ).start()
        time.sleep(0.4)

        # 1. empty GET /queue
        s, body = _get(base + "/queue")
        check("GET /queue empty list", s == 200 and body == {"jobs": []},
              detail=str(body))

        # 2. add 3 jobs
        s, body = _post(base + "/queue/add",
                        {"printer": "studio", "gcode": "/tmp/a.3mf",
                         "slot": 1, "label": "a"})
        check("POST /queue/add #1 returns 200", s == 200, str(s))
        check("add response includes job id",
              "job" in body and "id" in body["job"], str(body))
        a_id = body["job"]["id"]

        s, body = _post(base + "/queue/add",
                        {"printer": "garage", "gcode": "/tmp/b.3mf",
                         "slot": 2, "label": "b"})
        check("POST /queue/add #2 returns 200", s == 200, str(s))
        b_id = body["job"]["id"]

        s, body = _post(base + "/queue/add",
                        {"printer": "studio", "gcode": "/tmp/c.3mf",
                         "slot": 1, "label": "c"})
        check("POST /queue/add #3 returns 200", s == 200, str(s))
        c_id = body["job"]["id"]

        # 3. snapshot via GET /queue
        s, body = _get(base + "/queue")
        check("GET /queue lists 3 jobs",
              s == 200 and len(body.get("jobs", [])) == 3,
              detail=str(body))

        # 4. POST /queue/add without gcode → 400
        s, body = _post(base + "/queue/add", {"printer": "studio"})
        check("POST /queue/add without gcode returns 400",
              s == 400, str(s))

        # 5. POST /queue/move c to head of studio
        s, body = _post(base + "/queue/move",
                        {"id": c_id, "position": 0})
        check("POST /queue/move returns 200", s == 200 and body.get("ok"),
              str(body))
        # Verify order
        studio_jobs = [j for j in mgr.list() if j.printer == "studio"]
        check("after move: studio FIFO is [c, a]",
              [j.label for j in studio_jobs] == ["c", "a"],
              detail=str([j.label for j in studio_jobs]))

        # 6. POST /queue/move b to studio
        s, body = _post(base + "/queue/move",
                        {"id": b_id, "dest_printer": "studio",
                         "position": 1})
        check("POST /queue/move dest_printer returns 200",
              s == 200 and body.get("ok"), str(body))
        studio_jobs = [j for j in mgr.list() if j.printer == "studio"]
        check("after cross-printer move: studio = [c, b, a]",
              [j.label for j in studio_jobs] == ["c", "b", "a"],
              detail=str([j.label for j in studio_jobs]))

        # 7. POST /queue/cancel a
        s, body = _post(base + "/queue/cancel", {"id": a_id})
        check("POST /queue/cancel returns 200 + ok=True",
              s == 200 and body.get("ok"), str(body))
        a_after = mgr.get(a_id)
        check("cancelled job status=cancelled",
              a_after and a_after.status == "cancelled",
              detail=str(a_after))

        # 8. POST /queue/cancel a second time → ok=False (already cancelled)
        s, body = _post(base + "/queue/cancel", {"id": a_id})
        check("repeat cancel returns ok=False",
              s == 200 and body.get("ok") is False, str(body))

        # 9. POST /queue/remove a
        s, body = _post(base + "/queue/remove", {"id": a_id})
        check("POST /queue/remove returns 200 + ok=True",
              s == 200 and body.get("ok"), str(body))
        check("removed job is gone from list",
              mgr.get(a_id) is None)

        # 10. POST /queue/<unknown>
        s, body = _post(base + "/queue/launch", {})
        check("POST /queue/<unknown> returns 404", s == 404, str(s))

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print(f"\nALL TESTS PASSED — queue HTTP routes (#55)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
