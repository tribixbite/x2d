# Auto-timelapse + ffmpeg stitch

The bridge daemon auto-records a frame from the chamber camera every
30 s during active prints and saves them under
`~/.x2d/timelapses/<printer>/<job_id>/`. After the print finishes,
hit a single button in the web UI and ffmpeg stitches the frames
into an MP4 you can play inline.

## Enable

```bash
# camera daemon serves /cam.jpg on :8766
python3.12 x2d_bridge.py camera --bind 127.0.0.1:8766 &

# bridge daemon proxies /snapshot.jpg from the camera and runs the
# auto-timelapse on every printer
python3.12 x2d_bridge.py daemon --http :8765 \
    --timelapse \
    --timelapse-interval 30
```

## Lifecycle

* Print transitions OFF→ON (`gcode_state ∈ {RUNNING, PREPARE,
  SLICING, PAUSE}` OR `0 < mc_percent < 100`):
  - mkdir `~/.x2d/timelapses/<printer>/<sanitised_subtask_name>/`
  - per-printer capture thread spins up
  - polls `/snapshot.jpg` every `--timelapse-interval` s
  - writes `00001.jpg`, `00002.jpg`, …
  - rewrites `meta.json` on every frame so the UI sees live counts
* Print transitions ON→OFF: thread stops, `meta.ended` recorded.
* If the same `subtask_name` runs again later, a `_2`, `_3`, …
  suffix disambiguates.

## API

```bash
# List all recorded jobs
curl http://127.0.0.1:8765/timelapses

# Per-job frame list + status
curl http://127.0.0.1:8765/timelapses/studio/rumi_frame.gcode.3mf

# Fetch one frame
curl http://127.0.0.1:8765/timelapses/studio/rumi_frame.gcode.3mf/00001.jpg \
    > frame1.jpg

# Stitch into MP4 (long-running; ffmpeg synchronous)
curl -X POST http://127.0.0.1:8765/timelapses/studio/rumi_frame.gcode.3mf/stitch \
    -H 'Content-Type: application/json' \
    -d '{"fps": 30}'

# Fetch the stitched MP4
curl http://127.0.0.1:8765/timelapses/studio/rumi_frame.gcode.3mf/timelapse.mp4 \
    > timelapse.mp4
```

## Web UI

The "Timelapses" card has a job picker dropdown, a meta line
(N frames · duration · MP4 size), a CSS-grid of up to 24 sampled
thumbnails, "stitch MP4" and "play" buttons. The play button binds
the URL to an inline HTML5 `<video>`.

## ffmpeg invocation

```
ffmpeg -y \
    -framerate 30 \
    -i %05d.jpg \
    -c:v libx264 -pix_fmt yuv420p \
    -vf pad=ceil(iw/2)*2:ceil(ih/2)*2 \
    -movflags +faststart \
    timelapse.mp4
```

The pad filter handles odd-pixel JPEGs that H.264 would reject;
`+faststart` lets HTML5 `<video>` start playing before the file
fully loads.

## Path traversal safety

`frame_path()` resolves the requested frame path and verifies it's
inside the timelapse root via `Path.resolve().relative_to(root)`.
Any `..` segment fails the check with 404.

## Test harnesses

```bash
PYTHONPATH=. python3.12 runtime/timelapse/test_recorder.py  # 24/24 PASS
PYTHONPATH=. python3.12 runtime/timelapse/test_http.py      # 15/15 PASS
```

The recorder test drives a synthetic JPEG-serving camera through
RUNNING→FINISH transitions and verifies real ffmpeg produces a
valid MP4 with `ftyp` box. The HTTP test round-trips every route
including the stitch path.
