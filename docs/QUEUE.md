# Multi-printer print queue

A file-backed FIFO queue per printer with auto-dispatch. When a
printer transitions to idle (gcode_state ∈ {FINISH, IDLE, READY,
FAILED, ABORTED}), the daemon pulls the head of that printer's
pending sub-list and runs `upload_file()` + `start_print()` in one
shot. Crash-safe: jobs persist to `~/.x2d/queue.json` and any
`running` jobs are demoted back to `pending` on reload so a daemon
restart doesn't silently lose work.

## Enable

```bash
python3.12 x2d_bridge.py daemon --http :8765 --queue
```

That's it. Queue state lives at `~/.x2d/queue.json`.

## Job lifecycle

```
pending  → waiting for the target printer to be idle
running  → dispatched (X2DClient.publish landed)
done     → printer reported FINISH after running
failed   → dispatch raised, or printer reported FAILED
cancelled→ user removed before dispatch
```

## API

```bash
# List
curl http://127.0.0.1:8765/queue

# Add a job
curl -X POST http://127.0.0.1:8765/queue/add \
    -H 'Content-Type: application/json' \
    -d '{"printer":"studio","gcode":"/path/to/job.gcode.3mf","slot":3,"label":"job1"}'

# Cancel a pending job
curl -X POST http://127.0.0.1:8765/queue/cancel \
    -H 'Content-Type: application/json' \
    -d '{"id":"<job-id>"}'

# Drag-and-drop reorder (move job to position 0 in its printer's queue)
curl -X POST http://127.0.0.1:8765/queue/move \
    -H 'Content-Type: application/json' \
    -d '{"id":"<job-id>","position":0}'

# Cross-printer move
curl -X POST http://127.0.0.1:8765/queue/move \
    -H 'Content-Type: application/json' \
    -d '{"id":"<job-id>","dest_printer":"garage","position":0}'

# Permanent delete (use after cancel if you want it gone)
curl -X POST http://127.0.0.1:8765/queue/remove \
    -H 'Content-Type: application/json' \
    -d '{"id":"<job-id>"}'
```

## Web UI

The "Print queue" card in the web UI gives the same surface with
HTML5 native drag-and-drop (no library) and per-row cancel buttons.
Polled at 3 Hz so auto-dispatch is visible in real time.

## Idle-detection rules

A printer is "free for the next job" when ALL of:

* `gcode_state` ∈ `{FINISH, IDLE, READY, "", FAILED, ABORTED}`
* `mc_print_sub_stage` is empty / "0"
* `mc_percent` is not in (0, 100) — i.e. either ≤0 or ≥100

This is intentionally strict — better to miss a dispatch by one
state cycle than to fire over a print that just paused for a moment.

## Test harnesses

```bash
PYTHONPATH=. python3.12 runtime/queue/test_queue.py        # 33/33 PASS
PYTHONPATH=. python3.12 runtime/queue/test_queue_http.py   # 17/17 PASS
```

The first drives the manager directly with a mock dispatch_cb (FIFO
order, persistence reload, drag-and-drop reorder, cross-printer
move, cancel, dispatch_cb=False → failed). The second exercises
every HTTP route round-trip including 400/404 error paths.
