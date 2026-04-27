"""End-to-end test for the multi-printer queue manager (item #55).

Spins up a `QueueManager` with a fake `dispatch_cb` that records
every call, then drives state-update callbacks for two printers and
verifies:

* enqueue returns a job with status=pending
* state=running on a printer suppresses dispatch (queue waits)
* state=idle triggers dispatch of head-of-queue
* dispatch_cb is called exactly once per job
* job → running on dispatch, then → done on next idle tick
* per-printer FIFO ordering preserved
* cross-printer isolation (idle on A doesn't fire B's queue)
* persistence: re-loading from disk demotes running → pending so
  nothing gets lost on restart
* drag-and-drop reorder (move) honoured for pending jobs
* cancel removes from dispatchable queue
* "queue 3 jobs across 2 printers, watch them auto-dispatch" full
  scenario from the ledger's Done-when criterion
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from runtime.queue.manager import QueueManager, Job, _is_printer_idle


def _state(stage: str = "", percent: float = 0,
           gcode_state: str = "") -> dict:
    return {"print": {"gcode_state": gcode_state,
                       "mc_print_sub_stage": stage,
                       "mc_percent": percent}}


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

    # ----- _is_printer_idle unit checks -----
    check("_is_printer_idle: no state → False",
          not _is_printer_idle(None))
    check("_is_printer_idle: empty state → False",
          not _is_printer_idle({}))
    check("_is_printer_idle: gcode_state=RUNNING → False",
          not _is_printer_idle(_state(gcode_state="RUNNING")))
    check("_is_printer_idle: gcode_state=PAUSE → False",
          not _is_printer_idle(_state(gcode_state="PAUSE")))
    check("_is_printer_idle: gcode_state=FINISH → True",
          _is_printer_idle(_state(gcode_state="FINISH")))
    check("_is_printer_idle: gcode_state=IDLE → True",
          _is_printer_idle(_state(gcode_state="IDLE")))
    check("_is_printer_idle: percent=42 → False (mid-print)",
          not _is_printer_idle(_state(percent=42)))
    check("_is_printer_idle: percent=100 + gcode_state=FINISH → True",
          _is_printer_idle(_state(gcode_state="FINISH", percent=100)))
    check("_is_printer_idle: stage='heating_bed' → False",
          not _is_printer_idle(_state(stage="heating_bed")))

    # ----- 1. setup -----
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "queue.json"
        dispatched: list[Job] = []
        def cb(job: Job) -> bool:
            dispatched.append(job)
            return True

        m = QueueManager(dispatch_cb=cb, path=path)

        # ----- 2. enqueue 3 jobs across 2 printers -----
        j1 = m.add(printer="studio", gcode="/tmp/job1.3mf", slot=1, label="job1")
        j2 = m.add(printer="garage", gcode="/tmp/job2.3mf", slot=2, label="job2")
        j3 = m.add(printer="studio", gcode="/tmp/job3.3mf", slot=1, label="job3")
        check("enqueued 3 jobs", len(m.list()) == 3,
              detail=str(len(m.list())))
        check("studio has 2 pending",
              len(m.pending_for("studio")) == 2)
        check("garage has 1 pending",
              len(m.pending_for("garage")) == 1)

        # ----- 3. running state suppresses dispatch -----
        m.on_state("studio", _state(gcode_state="RUNNING", percent=50))
        check("studio RUNNING → no dispatch",
              len(dispatched) == 0)

        # ----- 4. idle triggers dispatch of FIFO head -----
        m.on_state("studio", _state(gcode_state="FINISH"))
        check("studio FINISH → 1 dispatch",
              len(dispatched) == 1, detail=str(len(dispatched)))
        if dispatched:
            check("dispatch was studio job1 (FIFO head)",
                  dispatched[0].id == j1.id,
                  detail=f"got {dispatched[0].label}")

        # ----- 5. job j1 now running, second idle tick doesn't redispatch -----
        m.on_state("studio", _state(gcode_state="FINISH"))
        # First, j1 should be marked done; then j3 (next pending) dispatched.
        check("after second idle tick: 2 dispatches total (j1 done + j3)",
              len(dispatched) == 2,
              detail=str([d.label for d in dispatched]))
        if len(dispatched) >= 2:
            check("second dispatch is studio job3",
                  dispatched[1].id == j3.id,
                  detail=f"got {dispatched[1].label}")

        # ----- 6. cross-printer isolation -----
        # garage is still untouched
        check("garage not yet dispatched",
              not any(d.printer == "garage" for d in dispatched))
        # idle on garage triggers garage's job
        m.on_state("garage", _state(gcode_state="FINISH"))
        check("garage idle → garage job2 dispatched",
              any(d.id == j2.id for d in dispatched),
              detail=str([d.label for d in dispatched]))

        # ----- 7. job state transitions -----
        # By now: j1 done (the second studio FINISH tick marked it
        # complete before dispatching j3); j3 running; j2 running on
        # garage's first FINISH tick.
        all_jobs = {j.id: j for j in m.list()}
        check("j1 status=done after second idle tick",
              all_jobs[j1.id].status == "done",
              detail=all_jobs[j1.id].status)
        check("j2 status=running", all_jobs[j2.id].status == "running",
              detail=all_jobs[j2.id].status)
        check("j3 status=running", all_jobs[j3.id].status == "running",
              detail=all_jobs[j3.id].status)

        # ----- 8. completing j2 marks it done -----
        m.on_state("garage", _state(gcode_state="FINISH"))
        all_jobs = {j.id: j for j in m.list()}
        check("j2 status=done after second garage idle",
              all_jobs[j2.id].status == "done",
              detail=all_jobs[j2.id].status)

        # ----- 9. persistence: snapshot path, demote running on reload -----
        # Mark a fresh job running to test the demotion path
        j4 = m.add(printer="garage", gcode="/tmp/job4.3mf", label="job4")
        # Simulate a running state by directly mutating
        with m._lock:
            j4_obj = next(j for j in m._jobs if j.id == j4.id)
            j4_obj.status = "running"
            m._persist()

        m2 = QueueManager(dispatch_cb=lambda j: True, path=path)
        all_after_reload = {j.id: j for j in m2.list()}
        check("reload preserves all 4 jobs",
              len(m2.list()) == 4, detail=str(len(m2.list())))
        check("reload demotes running → pending",
              all_after_reload[j4.id].status == "pending",
              detail=all_after_reload[j4.id].status)

        # ----- 10. drag-and-drop reorder -----
        # Add three pending jobs to studio, move the last to head.
        m3 = QueueManager(dispatch_cb=lambda j: True,
                          path=Path(tmp) / "queue2.json")
        a = m3.add(printer="studio", gcode="/tmp/a.3mf", label="a")
        b = m3.add(printer="studio", gcode="/tmp/b.3mf", label="b")
        c = m3.add(printer="studio", gcode="/tmp/c.3mf", label="c")
        check("initial pending order = [a,b,c]",
              [j.label for j in m3.pending_for("studio")] == ["a","b","c"])
        m3.move(c.id, position=0)
        check("after move(c, position=0): [c,a,b]",
              [j.label for j in m3.pending_for("studio")] == ["c","a","b"])

        # ----- 11. cancel removes from dispatchable queue -----
        m3.cancel(b.id)
        check("after cancel(b): pending = [c,a]",
              [j.label for j in m3.pending_for("studio")] == ["c","a"])

        # ----- 12. cross-printer move -----
        m3.move(a.id, dest_printer="garage", position=0)
        check("after move(a) → garage: studio pending = [c]",
              [j.label for j in m3.pending_for("studio")] == ["c"])
        check("after move(a) → garage: garage pending = [a]",
              [j.label for j in m3.pending_for("garage")] == ["a"])

        # ----- 13. dispatch_cb returning False marks failed -----
        fail_dispatched = []
        def fail_cb(job):
            fail_dispatched.append(job)
            return False
        m4 = QueueManager(dispatch_cb=fail_cb, path=Path(tmp) / "queue3.json")
        f = m4.add(printer="studio", gcode="/tmp/f.3mf", label="f")
        m4.on_state("studio", _state(gcode_state="FINISH"))
        check("failed dispatch records call",
              len(fail_dispatched) == 1)
        check("job → failed when cb returns False",
              m4.get(f.id).status == "failed",
              detail=m4.get(f.id).status)

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print(f"\nALL TESTS PASSED — multi-printer queue (#55)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
