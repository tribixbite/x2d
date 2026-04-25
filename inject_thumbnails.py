#!/usr/bin/env python3
"""Inject placeholder thumbnails into a Bambu .gcode.3mf so the X2D firmware accepts the import.

The bs-bionic CLI's slice path can't generate previews on Termux (glfw needs X11/Wayland;
neither is running and OSMesa-backend GLFW initialization still fails). The X2D firmware
historically rejects 3MFs that lack `Metadata/plate_1.png` even though the file would slice
fine — and `_rels/.rels` references the thumbnail relationships, so dangling refs are an
import-validator failure.

This script renders a simple monochrome top-down silhouette of the STL onto each required
PNG slot. Resolutions match official Bambu output (plate_1, top_1, pick_1, plate_no_light_1
at 512×512 RGBA; plate_1_small at 128×128 RGBA). The firmware uses these as on-screen
preview cards — content fidelity doesn't matter, only that the bytes parse as PNG.

Usage:
  inject_thumbnails.py --3mf <file.gcode.3mf> --stl <model.stl>

Modifies the .3mf in-place (safe — writes to a temp file then moves).
"""
from __future__ import annotations
import argparse, io, struct, sys, tempfile, zipfile
from pathlib import Path

# numpy-stl is pulled in by the user's existing slicer pipeline
from stl import mesh as stl_mesh  # type: ignore
from PIL import Image, ImageDraw  # type: ignore
import numpy as np


def render_silhouette(stl_path: Path, size: tuple[int, int]) -> Image.Image:
    """Render a top-down silhouette of the STL at the requested resolution.

    Projects all triangles onto the XY plane and rasterizes them as filled polygons.
    Background is transparent; the part is rendered as semi-translucent white over a
    dark gray bed-card border, so the X2D's screen renders it visibly without us
    needing OpenGL or perspective projection.
    """
    m = stl_mesh.Mesh.from_file(str(stl_path))
    tris = m.vectors  # shape (N, 3, 3) — N triangles, 3 verts each, xyz
    if tris.size == 0:
        # Empty mesh: return a blank tile rather than crashing the firmware
        return Image.new("RGBA", size, (40, 40, 40, 255))

    # Bbox in XY for fitting the part into a square viewport with 8% padding
    xy = tris[:, :, :2].reshape(-1, 2)
    xmin, ymin = xy.min(axis=0)
    xmax, ymax = xy.max(axis=0)
    span = max(xmax - xmin, ymax - ymin) or 1.0
    pad = 0.08
    scale = (1.0 - 2 * pad) * size[0] / span
    cx_world = (xmin + xmax) / 2.0
    cy_world = (ymin + ymax) / 2.0
    cx_pix, cy_pix = size[0] / 2.0, size[1] / 2.0

    img = Image.new("RGBA", size, (40, 40, 40, 255))  # bed gray
    draw = ImageDraw.Draw(img, "RGBA")
    # Bambu green tint over part — matches their default thumbnail aesthetic
    fill = (0x01, 0x80, 0x01, 200)
    for tri in tris:
        pts = []
        for v in tri:
            px = cx_pix + (v[0] - cx_world) * scale
            # PIL y-axis is top-down; STL y-axis is bottom-up — flip
            py = cy_pix - (v[1] - cy_world) * scale
            pts.append((px, py))
        draw.polygon(pts, fill=fill)
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--3mf", dest="threemf", required=True, type=Path)
    ap.add_argument("--stl", required=True, type=Path)
    args = ap.parse_args()

    if not args.threemf.exists():
        print(f"error: {args.threemf} missing", file=sys.stderr)
        return 1
    if not args.stl.exists():
        print(f"error: {args.stl} missing", file=sys.stderr)
        return 1

    big = render_silhouette(args.stl, (512, 512))
    small = render_silhouette(args.stl, (128, 128))

    def png_bytes(im: Image.Image) -> bytes:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    big_png = png_bytes(big)
    small_png = png_bytes(small)

    # The 5 PNG slots Bambu Studio produces. plate_no_light_1 is the bed-only render
    # (we reuse the same silhouette since we have no separate "lights off" pass);
    # top_1 is from-above; pick_1 is the colored object map (we reuse silhouette so
    # the firmware sees a non-empty PNG without needing a real face-id render).
    files = {
        "Metadata/plate_1.png": big_png,
        "Metadata/plate_1_small.png": small_png,
        "Metadata/plate_no_light_1.png": big_png,
        "Metadata/top_1.png": big_png,
        "Metadata/pick_1.png": big_png,
    }

    # Rewrite the 3MF: copy all entries except any pre-existing thumbnail names,
    # then append our PNGs. This avoids zipfile's lack of in-place file replacement.
    tmp = args.threemf.with_suffix(args.threemf.suffix + ".tmp")
    with zipfile.ZipFile(args.threemf, "r") as src, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename in files:
                continue  # will write fresh below
            dst.writestr(item, src.read(item.filename))
        for name, data in files.items():
            dst.writestr(name, data)
    tmp.replace(args.threemf)

    print(f"injected 5 thumbnails into {args.threemf} "
          f"(plate_1.png={len(big_png)}B, plate_1_small.png={len(small_png)}B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
