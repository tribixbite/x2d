"""Auto-recording timelapse capture (item #56).

The recorder hooks the daemon's per-printer state callback. When a
print is running it pulls /snapshot.jpg from the bridge daemon every
N seconds and stores it under
``~/.x2d/timelapses/<printer>/<job_id>/<seq>.jpg``. When the print
finishes (gcode_state → FINISH/IDLE), the recorder writes a tiny
``meta.json`` so the web UI can render thumbnails + offer a
one-click ffmpeg stitch into an MP4.
"""
