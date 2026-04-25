#!/usr/bin/env python3
"""Generate an STL of a rectangular picture-frame with text debossed (recessed)
into the bottom border.

Default frame: portrait orientation with a 67 mm (X) × 108 mm (Y) outer
rectangle and a 55.4 mm × 85.4 mm centered inner opening. Stock height is
1.2 mm. Borders: 5.8 mm on left/right, 11.3 mm on top/bottom.

Text is centered horizontally across the frame, vertically in the middle
80 % of the bottom border with the top of the glyphs adjacent to the inner
opening. Text is recessed (NOT cut all the way through) by --deboss-depth
mm (default 0.6 mm of the 1.2 mm stock). Partial-depth deboss keeps closed
glyph counters (R, A, P …) anchored to the surrounding material — through-
cuts make those counters float and drop out at print time.

Optional --top-text mirrors the same partial deboss into the top border.

CLI:
    make_frame.py --text RUMI --out rumi_frame.stl
    make_frame.py --text "HUNTR/X" --top-text "ZOEY" --out two_band.stl
    make_frame.py --text RUMI --deboss-depth 0.4 --out shallow.stl

Coordinates: STL +X right, +Y up (portrait), +Z out of frame top face.
Bottom edge at y=0. The bottom border (where TEXT goes) spans
y ∈ [0, 11.3]. Text glyph top is at y ≈ 0.9 · 11.3 = 10.17.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from stl import mesh


# ---------------------------------------------------------------------------
# Defaults — keep Bambu X2D-friendly geometry
# ---------------------------------------------------------------------------
DEFAULT_OD = (67.0, 108.0)
DEFAULT_ID = (55.4, 85.4)
DEFAULT_H = 1.2
DEFAULT_DEBOSS_DEPTH = 0.6  # mm of the 1.2 mm stock removed by text glyphs
DEFAULT_CARD_LAYER = 0.4    # mm-thick base spanning the inner opening to hold a card
DEFAULT_PX_MM = 0.1
DEFAULT_FONT = "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--text", required=True,
                   help="Text to deboss in the bottom border (e.g. RUMI)")
    p.add_argument("--top-text", default="",
                   help="Optional text for the top border (mirrored)")
    p.add_argument("--out", required=True, type=Path, help="Output STL path")
    p.add_argument("--od", type=float, nargs=2, default=DEFAULT_OD,
                   metavar=("X", "Y"),
                   help=f"Outer dimensions in mm (default {DEFAULT_OD[0]} {DEFAULT_OD[1]})")
    p.add_argument("--id", dest="id_", type=float, nargs=2, default=DEFAULT_ID,
                   metavar=("X", "Y"),
                   help=f"Inner-opening dimensions in mm (default {DEFAULT_ID[0]} {DEFAULT_ID[1]})")
    p.add_argument("--height", type=float, default=DEFAULT_H,
                   help=f"Stock thickness in Z (default {DEFAULT_H} mm)")
    p.add_argument("--deboss-depth", type=float, default=DEFAULT_DEBOSS_DEPTH,
                   help=f"Text recess depth in mm (default {DEFAULT_DEBOSS_DEPTH}). "
                        "Must be > 0 and < --height; 0 → engrave only the surface "
                        "(no extruded recess), full --height → through-cut "
                        "(reproduces the original HUNTR/X behaviour but lets closed "
                        "glyph counters float).")
    p.add_argument("--card-layer", type=float, default=DEFAULT_CARD_LAYER,
                   help=f"Thickness in mm of a thin floor that bridges the inner "
                        f"opening, holding a card in place (default {DEFAULT_CARD_LAYER}). "
                        "Set to 0 for an open frame (back-compat with the original "
                        "through-window behaviour). Must be < --height.")
    p.add_argument("--font", default=DEFAULT_FONT,
                   help="TTF font path (default DejaVu Sans Bold)")
    p.add_argument("--px-mm", type=float, default=DEFAULT_PX_MM,
                   help=f"Rasterization resolution mm/px (default {DEFAULT_PX_MM})")
    return p.parse_args()


def find_font_size(font_path: str, text: str, target_h_px: int, max_w_px: int) -> int:
    """Binary-search the largest font size whose rendered bbox fits both
    the allotted height and width in pixels."""
    lo, hi = 4, 600
    while lo < hi:
        mid = (lo + hi + 1) // 2
        f = ImageFont.truetype(font_path, mid)
        bbox = f.getbbox(text)
        if (bbox[3] - bbox[1]) <= target_h_px and (bbox[2] - bbox[0]) <= max_w_px:
            lo = mid
        else:
            hi = mid - 1
    return lo


def rasterize_text_mask(args, nx: int, ny: int, border_y: float,
                        text: str, mirror: bool) -> np.ndarray:
    """Return a boolean mask of pixels covered by `text` glyphs in STL
    orientation (row 0 = STL y=0, the bottom of the frame).

    Top-band text uses mirror=True to flip vertically so the glyphs are
    upright when the frame is held with the bottom-band text at the bottom.
    """
    img = Image.new("L", (nx, ny), 0)
    draw = ImageDraw.Draw(img)

    # Text occupies the middle 80 % of border_y, top adjacent to the ID.
    if mirror:
        # Top band: y range [OD_Y - 0.9·border_y, OD_Y - 0.1·border_y]
        band_top_stl = args.od[1] - border_y * 0.1
        band_bot_stl = args.od[1] - border_y * 0.9
    else:
        band_top_stl = border_y * 0.9
        band_bot_stl = border_y * 0.1

    # PIL row 0 = top of image = STL y = OD_Y; convert.
    pil_top_row = int(round(ny - band_top_stl / args.px_mm))
    pil_bot_row = int(round(ny - band_bot_stl / args.px_mm))
    text_h_px = pil_bot_row - pil_top_row

    # Allow 0.5 mm gutter on each side of the OD.
    max_text_w_px = int(round((args.od[0] - 1.0) / args.px_mm))
    font_size = find_font_size(args.font, text, text_h_px, max_text_w_px)
    font = ImageFont.truetype(args.font, font_size)
    bbox = font.getbbox(text)
    tw, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]

    tx = (nx - tw) // 2 - bbox[0]
    ty = pil_top_row - bbox[1]
    draw.text((tx, ty), text, font=font, fill=255)

    arr = np.array(img)
    # Threshold above mid-grey to avoid antialias-fringe single pixels that
    # would otherwise create 0.1 mm slivers the slicer can't print.
    mask = arr > 200
    # Flip into STL orientation (row 0 = STL y = 0).
    return np.flipud(mask), font_size


def build_masks(args) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Build the five boolean masks that drive the mesh:

    - floor_mask: full OD rectangle, present at z = 0..card_layer
                  (or 0..H if card_layer == 0 — frame collapses to the
                  original border-only behaviour).
    - inner_mask: the rectangular opening interior (footprint of the card
                  pocket). Exposed as the top of the floor at z = card_layer.
                  Empty when card_layer == 0.
    - border_mask: OD minus ID — the raised picture-frame border, present
                   at z = card_layer..H. When card_layer == 0 this equals
                   the original frame_mask.
    - top_full_mask: subset of border_mask where the top face is at z = H
                     (un-debossed flat top of the border).
    - pocket_mask:   subset of border_mask whose top face is at
                     z = H - deboss_depth (the bottom of the recess that
                     receives the debossed glyphs).

    Returns also a dict of useful counts for the build banner.
    """
    nx = int(round(args.od[0] / args.px_mm))
    ny = int(round(args.od[1] / args.px_mm))

    border_x = (args.od[0] - args.id_[0]) / 2.0
    border_y = (args.od[1] - args.id_[1]) / 2.0

    # Outer rect (everything inside the OD).
    outer = Image.new("L", (nx, ny), 0)
    ImageDraw.Draw(outer).rectangle([0, 0, nx - 1, ny - 1], fill=255)
    floor_mask = np.flipud(np.array(outer) > 128)

    # Inner rect (the card opening footprint).
    inner = Image.new("L", (nx, ny), 0)
    ix0 = int(round(border_x / args.px_mm))
    ix1 = int(round((args.od[0] - border_x) / args.px_mm))
    iy0 = int(round(border_y / args.px_mm))
    iy1 = int(round((args.od[1] - border_y) / args.px_mm))
    ImageDraw.Draw(inner).rectangle([ix0, iy0, ix1 - 1, iy1 - 1], fill=255)
    inner_mask = np.flipud(np.array(inner) > 128)

    # Border = OD minus ID; the part that rises from the floor up to z=H.
    border_mask = floor_mask & ~inner_mask

    # Bottom-band text mask, constrained to live inside the border only —
    # never on the floor (would lift the card surface) and never outside.
    bottom_mask, bottom_fs = rasterize_text_mask(args, nx, ny, border_y,
                                                 args.text, mirror=False)
    pocket_mask = bottom_mask & border_mask

    top_fs = None
    if args.top_text:
        top_mask, top_fs = rasterize_text_mask(args, nx, ny, border_y,
                                               args.top_text, mirror=True)
        pocket_mask = pocket_mask | (top_mask & border_mask)

    top_full_mask = border_mask & ~pocket_mask

    info = {
        "nx": nx, "ny": ny,
        "floor_px": int(floor_mask.sum()),
        "inner_px": int(inner_mask.sum()),
        "border_px": int(border_mask.sum()),
        "pocket_px": int(pocket_mask.sum()),
        "bottom_font": bottom_fs,
        "top_font": top_fs,
    }
    return floor_mask, inner_mask, border_mask, top_full_mask, pocket_mask, info


# ---------------------------------------------------------------------------
# Mesh emission. Each material region emits run-length-merged quads for
# horizontal faces and column/row-merged vertical wall quads. We keep the
# emitter helpers shared between the two top-face heights and four wall
# heights so the topology stays clean (every edge appears in exactly two
# triangles).
# ---------------------------------------------------------------------------
def add_quad(tris: list, v0, v1, v2, v3, normal):
    """Append a CCW-from-`normal` quad as two triangles."""
    tris.append((v0, v1, v2, normal))
    tris.append((v0, v2, v3, normal))


def emit_horizontal_face(tris: list, mask: np.ndarray, z: float,
                         normal_up: bool, px_mm: float) -> None:
    """Emit a flat face at height `z` over every True cell of `mask`,
    with row-wise run-length merging."""
    ny, nx = mask.shape
    n = (0.0, 0.0, 1.0 if normal_up else -1.0)
    for iy in range(ny):
        ix = 0
        row = mask[iy]
        while ix < nx:
            if row[ix]:
                start = ix
                while ix < nx and row[ix]:
                    ix += 1
                x0, x1 = start * px_mm, ix * px_mm
                y0, y1 = iy * px_mm, (iy + 1) * px_mm
                if normal_up:
                    add_quad(tris,
                             (x0, y0, z), (x1, y0, z),
                             (x1, y1, z), (x0, y1, z), n)
                else:
                    add_quad(tris,
                             (x0, y0, z), (x0, y1, z),
                             (x1, y1, z), (x1, y0, z), n)
            else:
                ix += 1


def emit_vertical_walls(tris: list, mask: np.ndarray,
                        z_lo: float, z_hi: float, px_mm: float,
                        flip_normals: bool = False) -> None:
    """Emit vertical walls along the boundary of `mask`, between z_lo and
    z_hi, with outward normals (CCW viewed from outside).

    When `flip_normals=True`, every wall has its winding reversed and its
    normal vector inverted. Use this when the supplied `mask` represents
    the *cavity* (e.g. a deboss pocket) rather than the surrounding solid
    — the resulting surface visually faces the cavity interior, which is
    the correct orientation for a recess seen from above.
    """
    ny, nx = mask.shape
    pad_h = np.zeros((ny, 1), dtype=bool)
    pad_v = np.zeros((1, nx), dtype=bool)

    right = np.concatenate([mask[:, 1:], pad_h], axis=1)
    left = np.concatenate([pad_h, mask[:, :-1]], axis=1)
    up = np.concatenate([mask[1:, :], pad_v], axis=0)
    down = np.concatenate([pad_v, mask[:-1, :]], axis=0)

    pos_x = mask & ~right    # boundary on +X face
    neg_x = mask & ~left     # boundary on -X face
    pos_y = mask & ~up       # boundary on +Y face
    neg_y = mask & ~down     # boundary on -Y face

    # +X / -X walls: column-wise Y run merge
    for ix in range(nx):
        for sign, wall in ((+1, pos_x), (-1, neg_x)):
            col = wall[:, ix]
            iy = 0
            while iy < ny:
                if col[iy]:
                    start = iy
                    while iy < ny and col[iy]:
                        iy += 1
                    y0, y1 = start * px_mm, iy * px_mm
                    if sign > 0:
                        x = (ix + 1) * px_mm
                        nx_vec = (-1.0, 0.0, 0.0) if flip_normals else (1.0, 0.0, 0.0)
                        if not flip_normals:
                            add_quad(tris, (x, y0, z_lo), (x, y1, z_lo),
                                     (x, y1, z_hi), (x, y0, z_hi), nx_vec)
                        else:
                            add_quad(tris, (x, y0, z_lo), (x, y0, z_hi),
                                     (x, y1, z_hi), (x, y1, z_lo), nx_vec)
                    else:
                        x = ix * px_mm
                        nx_vec = (1.0, 0.0, 0.0) if flip_normals else (-1.0, 0.0, 0.0)
                        if not flip_normals:
                            add_quad(tris, (x, y0, z_lo), (x, y0, z_hi),
                                     (x, y1, z_hi), (x, y1, z_lo), nx_vec)
                        else:
                            add_quad(tris, (x, y0, z_lo), (x, y1, z_lo),
                                     (x, y1, z_hi), (x, y0, z_hi), nx_vec)
                else:
                    iy += 1

    # +Y / -Y walls: row-wise X run merge
    for iy in range(ny):
        for sign, wall in ((+1, pos_y), (-1, neg_y)):
            row = wall[iy, :]
            ix = 0
            while ix < nx:
                if row[ix]:
                    start = ix
                    while ix < nx and row[ix]:
                        ix += 1
                    x0, x1 = start * px_mm, ix * px_mm
                    if sign > 0:
                        y = (iy + 1) * px_mm
                        ny_vec = (0.0, -1.0, 0.0) if flip_normals else (0.0, 1.0, 0.0)
                        if not flip_normals:
                            add_quad(tris, (x0, y, z_lo), (x0, y, z_hi),
                                     (x1, y, z_hi), (x1, y, z_lo), ny_vec)
                        else:
                            add_quad(tris, (x0, y, z_lo), (x1, y, z_lo),
                                     (x1, y, z_hi), (x0, y, z_hi), ny_vec)
                    else:
                        y = iy * px_mm
                        ny_vec = (0.0, 1.0, 0.0) if flip_normals else (0.0, -1.0, 0.0)
                        if not flip_normals:
                            add_quad(tris, (x0, y, z_lo), (x1, y, z_lo),
                                     (x1, y, z_hi), (x0, y, z_hi), ny_vec)
                        else:
                            add_quad(tris, (x0, y, z_lo), (x0, y, z_hi),
                                     (x1, y, z_hi), (x1, y, z_lo), ny_vec)
                else:
                    ix += 1


def main() -> int:
    args = parse_args()
    if args.deboss_depth < 0 or args.deboss_depth > args.height:
        print(f"--deboss-depth must be in [0, --height={args.height}]",
              file=sys.stderr)
        return 1
    if args.card_layer < 0 or args.card_layer >= args.height:
        print(f"--card-layer must be in [0, --height={args.height})",
              file=sys.stderr)
        return 1
    if not Path(args.font).exists():
        print(f"font not found: {args.font}", file=sys.stderr)
        return 1

    floor_mask, inner_mask, border_mask, top_full_mask, pocket_mask, info = build_masks(args)
    H = args.height
    cz = args.card_layer  # interface height between card-floor and rising border
    z_pocket = H - args.deboss_depth  # top face inside the deboss recess

    tris: list = []

    # Bottom face at z=0, full OD footprint (or border footprint when no
    # card layer), normal -Z.
    bottom_mask = floor_mask if cz > 0 else border_mask
    emit_horizontal_face(tris, bottom_mask, 0.0, normal_up=False,
                         px_mm=args.px_mm)

    # Card-floor top: only inside the inner opening, exposed at z=cz.
    if cz > 0:
        emit_horizontal_face(tris, inner_mask, cz, normal_up=True,
                             px_mm=args.px_mm)

    # Border top at z=H over un-debossed cells, normal +Z.
    emit_horizontal_face(tris, top_full_mask, H, normal_up=True,
                         px_mm=args.px_mm)

    # Pocket bottom at z=H-d, normal +Z.
    if args.deboss_depth > 0:
        emit_horizontal_face(tris, pocket_mask, z_pocket, normal_up=True,
                             px_mm=args.px_mm)

    if cz > 0:
        # OD perimeter walls span the full floor height.
        emit_vertical_walls(tris, floor_mask, 0.0, cz, args.px_mm)
        # Border walls cover OD outer (z=cz..H) AND ID inner rim.
        emit_vertical_walls(tris, border_mask, cz, H, args.px_mm)
    else:
        # Original behaviour: full-height border walls, no floor.
        emit_vertical_walls(tris, border_mask, 0.0, H, args.px_mm)

    # Pocket walls — at the boundary between the un-debossed top and the
    # recessed pocket. pocket_mask is fully contained inside border_mask,
    # so it never touches OD/ID exterior, so we can emit walls of pocket_mask
    # with `flip_normals=True` to face the cavity (correct outward direction).
    if args.deboss_depth > 0:
        emit_vertical_walls(tris, pocket_mask, z_pocket, H, args.px_mm,
                            flip_normals=True)

    print(f"Grid: {info['nx']} × {info['ny']}   "
          f"floor px: {info['floor_px']}   "
          f"border px: {info['border_px']}   "
          f"pocket px: {info['pocket_px']}   "
          f"bottom font px: {info['bottom_font']}"
          + (f"   top font px: {info['top_font']}"
             if info['top_font'] else ""))

    n_tris = len(tris)
    data = np.zeros(n_tris, dtype=mesh.Mesh.dtype)
    for i, (v0, v1, v2, normal) in enumerate(tris):
        data["vectors"][i][0] = v0
        data["vectors"][i][1] = v1
        data["vectors"][i][2] = v2
        data["normals"][i] = normal

    args.out.parent.mkdir(parents=True, exist_ok=True)
    mesh.Mesh(data).save(str(args.out))
    deboss_pct = 100 * args.deboss_depth / H
    floor_pct = 100 * cz / H
    print(f"Wrote {args.out}   triangles: {n_tris}   "
          f"deboss: {args.deboss_depth} mm ({deboss_pct:.0f}% of {H} mm)   "
          f"card floor: {cz} mm ({floor_pct:.0f}% of {H} mm)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
