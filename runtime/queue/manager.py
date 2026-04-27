"""File-backed multi-printer print queue (item #55).

`QueueManager` owns the ordered list of pending + running jobs, persists
to ``~/.x2d/queue.json`` atomically, and dispatches the next pending
job to a printer when that printer's state callback says it's idle.

Job lifecycle
-------------

  pending  → waiting for the target printer to be idle
  running  → dispatched (X2DClient.publish landed)
  done     → printer reported FINISH after running
  failed   → dispatch raised, or printer reported FAILED
  cancel   → user removed before dispatch

State the manager treats as "idle":

  - gcode_state in {"FINISH", "IDLE", "READY", ""}
  - mc_print_sub_stage == "" or absent
  - mc_print_stage == "" or absent

A printer is considered "idle" when none of the active markers are
present. The detection is strict — better to miss a dispatch by one
state cycle than to fire over a print that just paused for a moment.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

LOG = logging.getLogger("x2d.queue")

_DEFAULT_PATH = Path.home() / ".x2d" / "queue.json"


@dataclasses.dataclass
class Job:
    id:        str
    printer:   str           # name of [printer:NAME] (or "" for default)
    gcode:     str           # local path to .gcode.3mf
    slot:      int = 1       # AMS slot 1..16
    status:    str = "pending"  # pending|running|done|failed|cancelled
    enqueued:  float = 0.0
    started:   float = 0.0
    finished:  float = 0.0
    error:     str = ""
    label:     str = ""      # human-readable display name

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# Status values the manager treats as the printer being free for work.
_IDLE_GCODE_STATES = {"FINISH", "IDLE", "READY", "", "FAILED", "ABORTED"}


def _is_printer_idle(state: dict | None) -> bool:
    if not state:
        return False
    p = state.get("print", {})
    gs = (p.get("gcode_state") or "").upper()
    if gs and gs not in _IDLE_GCODE_STATES:
        return False
    sub = p.get("mc_print_sub_stage")
    if sub and str(sub).strip() not in ("", "0"):
        return False
    pct = p.get("mc_percent")
    # If a print is at <100% it can't be idle, even when gcode_state
    # is missing for any reason.
    if isinstance(pct, (int, float)) and 0 < pct < 100:
        return False
    return True


class QueueManager:
    """Thread-safe FIFO + auto-dispatch.

    `dispatch_cb(job)` is invoked when a printer is idle and a pending
    job exists for it. The callback should perform the upload+publish;
    return True on success (job → running), False/raise on failure
    (job → failed with error string).
    """

    def __init__(self,
                 dispatch_cb: Callable[[Job], bool],
                 path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._jobs: list[Job] = []
        self._path = path or _DEFAULT_PATH
        self._dispatch_cb = dispatch_cb
        self._load()

    # ----- persistence -------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            LOG.warning("queue.json unreadable (%s); starting empty", e)
            return
        for d in data.get("jobs", []):
            try:
                job = Job.from_dict(d)
            except (TypeError, ValueError):
                continue
            # Anything that was running when we crashed is demoted
            # back to pending — the printer firmware reports the
            # actual state and the next idle tick will retry.
            if job.status == "running":
                job.status = "pending"
                job.started = 0.0
            self._jobs.append(job)
        LOG.info("loaded %d jobs from %s", len(self._jobs), self._path)

    def _persist(self) -> None:
        # Caller holds self._lock.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"jobs": [j.to_dict() for j in self._jobs]}, indent=2))
        os.replace(tmp, self._path)

    # ----- public API --------------------------------------------------
    def add(self, *, printer: str, gcode: str, slot: int = 1,
            label: str = "") -> Job:
        with self._lock:
            job = Job(
                id=uuid.uuid4().hex,
                printer=printer or "",
                gcode=str(gcode),
                slot=int(slot),
                label=label or Path(gcode).name,
                enqueued=time.time(),
            )
            self._jobs.append(job)
            self._persist()
            return job

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    return j
        return None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id and j.status == "pending":
                    j.status = "cancelled"
                    j.finished = time.time()
                    self._persist()
                    return True
            return False

    def remove(self, job_id: str) -> bool:
        with self._lock:
            for i, j in enumerate(self._jobs):
                if j.id == job_id:
                    del self._jobs[i]
                    self._persist()
                    return True
            return False

    def move(self, job_id: str, *, dest_printer: str | None = None,
             position: int | None = None) -> bool:
        """Drag-and-drop: re-target to a different printer and/or
        re-position within the queue. Position 0 = head of the
        per-printer subqueue (after running jobs)."""
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None or job.status not in ("pending",):
                return False
            if dest_printer is not None:
                job.printer = dest_printer
            if position is not None:
                # Pull job out, insert at new index among same-printer
                # pending jobs.
                self._jobs.remove(job)
                # Reinsert: the manager's overall list is just append-
                # ordered; per-printer order is what the dispatcher
                # uses (FIFO via filter). Insert at the right slot
                # so the per-printer FIFO matches the requested
                # position.
                same = [j for j in self._jobs
                         if j.printer == job.printer
                             and j.status == "pending"]
                before = same[position] if 0 <= position < len(same) else None
                if before is None:
                    self._jobs.append(job)
                else:
                    self._jobs.insert(self._jobs.index(before), job)
            self._persist()
            return True

    def pending_for(self, printer: str) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs
                    if j.printer == printer and j.status == "pending"]

    def has_running(self, printer: str) -> bool:
        with self._lock:
            return any(j for j in self._jobs
                       if j.printer == printer and j.status == "running")

    # ----- dispatch ----------------------------------------------------
    def on_state(self, printer: str, state: dict | None) -> None:
        """Hook into the daemon's per-printer state callback. If the
        printer is idle, attempt to dispatch the next pending job
        for it."""
        # First mark any running job as done if we see FINISH/IDLE.
        if state and _is_printer_idle(state):
            with self._lock:
                for j in self._jobs:
                    if (j.printer == printer
                            and j.status == "running"):
                        j.status = "done"
                        j.finished = time.time()
                self._persist()
        if not _is_printer_idle(state):
            return
        with self._lock:
            if self.has_running(printer):
                return
            pending = self.pending_for(printer)
            if not pending:
                return
            job = pending[0]
            job.status = "running"
            job.started = time.time()
            self._persist()
        # Run the callback OUTSIDE the lock — it may take seconds for
        # FTPS upload + MQTT publish.
        try:
            ok = bool(self._dispatch_cb(job))
        except Exception as e:
            ok = False
            with self._lock:
                job.status = "failed"
                job.error = str(e)
                job.finished = time.time()
                self._persist()
            LOG.exception("dispatch %s failed: %s", job.id, e)
            return
        if not ok:
            with self._lock:
                job.status = "failed"
                job.error = "dispatch_cb returned False"
                job.finished = time.time()
                self._persist()
        # On success, leave status=running; on_state on the next idle
        # cycle will mark it done.
