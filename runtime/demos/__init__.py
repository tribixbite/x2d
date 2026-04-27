"""Demo media renderer (item #61).

Generates the five demo MP4s catalogued in IMPROVEMENTS.md by
synthesising frames with PIL and stitching them with ffmpeg. No
real GUI / no real terminal recording required, so the renderer
is fully reproducible in CI and on any device with PIL + ffmpeg.

Run:

    PYTHONPATH=. python3.12 runtime/demos/render.py

Outputs land in `docs/demos/` (committed to the repo so users can
play them straight from GitHub).
"""
