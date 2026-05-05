#!/usr/bin/env python3
"""x2d_slice.py — slice an STL with the X2D dual-extruder profile via BS CLI,
producing a .gcode.3mf whose metadata (weight, tray_info_idx, prediction)
matches what the GUI would produce.

Why this wrapper exists (resolves #97 in IMPROVEMENTS.md):

BambuStudio's `--slice` CLI mode supports two input forms:
  * a bare STL/STP/OBJ/etc. + `--load-settings <process>;<machine>` +
    `--load-filaments <filament>` — but the X2D dual-extruder profile
    expects 4 filament slots and the CLI doesn't synthesize the
    missing tray_info_idx / density linkage. Output ships with empty
    weight, GIF=Generic Input Filament, and prediction times off by
    ~50%.
  * an existing .gcode.3mf project file with all settings already
    embedded — re-slices correctly with weight, density, prediction
    matching the original.

This script bridges the gap: it takes an STL, opens a known-good
template .gcode.3mf, swaps in the STL's geometry, and feeds the
resulting hybrid 3MF to BS for re-slicing.

Usage:
    x2d_slice.py model.stl --out model.gcode.3mf
    x2d_slice.py model.stl --out model.gcode.3mf --template ref.gcode.3mf
    x2d_slice.py model.stl --out model.gcode.3mf --process 0.16mm

Default template lives at $X2D_ROOT/rumi_frame.gcode.3mf.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

X2D_ROOT = Path(os.environ.get("X2D_ROOT", "/data/data/com.termux/files/home/git/x2d"))
DEFAULT_TEMPLATE = X2D_ROOT / "rumi_frame.gcode.3mf"
BS_BIN = X2D_ROOT / "bs-bionic" / "build" / "src" / "bambu-studio"

# 3MF model XML namespace
NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
ET.register_namespace("", NS_3MF)


def parse_stl(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary or ASCII STL into (vertices, triangles) — vertices
    deduplicated to keep the 3MF compact."""
    data = path.read_bytes()
    is_ascii = data[:5].lower() == b"solid" and b"\nfacet" in data[:512]
    verts: dict[tuple[float, float, float], int] = {}
    tris: list[tuple[int, int, int]] = []

    def add_vert(v: tuple[float, float, float]) -> int:
        # Quantize to 6 decimal places to dedup numerically-identical verts
        k = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
        if k not in verts:
            verts[k] = len(verts)
        return verts[k]

    if is_ascii:
        # ASCII parser
        text = data.decode("utf-8", errors="replace")
        cur: list[tuple[float, float, float]] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("vertex"):
                xyz = tuple(float(x) for x in line.split()[1:4])
                cur.append(xyz)
                if len(cur) == 3:
                    tris.append((add_vert(cur[0]), add_vert(cur[1]), add_vert(cur[2])))
                    cur = []
    else:
        # Binary STL: 80-byte header, 4-byte tri count, then 50 bytes per tri
        if len(data) < 84:
            raise ValueError(f"{path} too small to be an STL")
        n_tris = struct.unpack_from("<I", data, 80)[0]
        offset = 84
        for _ in range(n_tris):
            # Skip the 12-byte normal vector
            v1 = struct.unpack_from("<fff", data, offset + 12)
            v2 = struct.unpack_from("<fff", data, offset + 24)
            v3 = struct.unpack_from("<fff", data, offset + 36)
            tris.append((add_vert(v1), add_vert(v2), add_vert(v3)))
            offset += 50
    # Stable vertex order: dict insertion order
    vlist = list(verts.keys())
    return vlist, tris


def build_3mf_object(vlist, tris) -> str:
    """Generate a single-object 3D/Objects/object_1.model XML in the 3MF
    schema. Returns the XML as a string ready to write into the zip."""
    sio = []
    sio.append('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n')
    sio.append(
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS_3MF}" '
        f'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n'
    )
    sio.append("  <resources>\n")
    sio.append('    <object id="1" type="model">\n')
    sio.append("      <mesh>\n        <vertices>\n")
    for x, y, z in vlist:
        sio.append(f'          <vertex x="{x}" y="{y}" z="{z}"/>\n')
    sio.append("        </vertices>\n        <triangles>\n")
    for a, b, c in tris:
        sio.append(f'          <triangle v1="{a}" v2="{b}" v3="{c}"/>\n')
    sio.append("        </triangles>\n      </mesh>\n    </object>\n")
    sio.append("  </resources>\n")
    sio.append('  <build>\n    <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n  </build>\n')
    sio.append("</model>\n")
    return "".join(sio)


def graft_stl_into_template(template: Path, stl: Path, out: Path) -> None:
    """Copy template 3MF, replace its 3D geometry with the STL's, and write
    to `out`. Preserves project_settings, machine, filament, etc.
    """
    vlist, tris = parse_stl(stl)
    print(f"[x2d_slice] parsed STL: {len(vlist)} verts, {len(tris)} triangles", file=sys.stderr)

    new_xml = build_3mf_object(vlist, tris)

    with zipfile.ZipFile(template, "r") as zin:
        names = zin.namelist()
        # The geometry usually lives at 3D/Objects/object_1.model;
        # 3D/3dmodel.model has a small header that just refs it.
        target = None
        for cand in ("3D/Objects/object_1.model", "3D/3dmodel.model"):
            if cand in names:
                target = cand
                break
        if not target:
            # Pick any *.model under 3D/
            target = next((n for n in names if n.startswith("3D/") and n.endswith(".model")), None)
        if not target:
            raise FileNotFoundError(f"no .model file found in template {template}")
        print(f"[x2d_slice] grafting STL into 3MF entry {target!r}", file=sys.stderr)

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                if name == target:
                    zout.writestr(name, new_xml)
                else:
                    zout.writestr(name, zin.read(name))


def run_bs_slice(input_3mf: Path, out_3mf: Path, plate: int = 0, debug: int = 1) -> int:
    """Invoke BS CLI to re-slice the given 3MF and produce a fresh
    output. The output dir is the parent of `out_3mf`; BS writes
    `<basename>` plus `plate_*.gcode` files."""
    out_dir = out_3mf.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(BS_BIN),
        "--slice", str(plate),
        "--debug", str(debug),
        "--outputdir", str(out_dir),
        "--export-3mf", out_3mf.name,
        str(input_3mf),
    ]
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":1")
    print(f"[x2d_slice] running: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd, env=env)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("stl", type=Path, help="input STL/STP/etc.")
    p.add_argument("--out", "-o", type=Path, required=True, help="output .gcode.3mf path")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                   help=f"reference 3mf with embedded X2D profile (default: {DEFAULT_TEMPLATE})")
    p.add_argument("--plate", type=int, default=0, help="plate to slice (0 = all)")
    p.add_argument("--keep-graft", action="store_true",
                   help="keep the intermediate grafted 3mf for debugging")
    args = p.parse_args()

    if not args.stl.exists():
        print(f"input not found: {args.stl}", file=sys.stderr)
        return 2
    if not args.template.exists():
        print(f"template not found: {args.template}", file=sys.stderr)
        return 2
    if not BS_BIN.exists():
        print(f"bambu-studio not found at {BS_BIN}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="x2d_slice_") as td:
        graft = Path(td) / "graft.gcode.3mf"
        graft_stl_into_template(args.template, args.stl, graft)
        if args.keep_graft:
            kept = args.out.with_suffix(".graft.3mf")
            shutil.copy2(graft, kept)
            print(f"[x2d_slice] kept grafted 3mf: {kept}", file=sys.stderr)
        rc = run_bs_slice(graft, args.out, plate=args.plate)
        if rc != 0:
            print(f"[x2d_slice] BS CLI exited rc={rc}", file=sys.stderr)
            return rc

    # Print summary
    if args.out.exists():
        with zipfile.ZipFile(args.out) as z:
            try:
                info = z.read("Metadata/slice_info.config").decode("utf-8", errors="replace")
            except KeyError:
                info = ""
        for key in ("prediction", "weight", "used_m", "tray_info_idx", "printer_model_id"):
            for line in info.splitlines():
                if f'key="{key}"' in line or f"{key}=" in line:
                    print(f"  {line.strip()}", file=sys.stderr)
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main())
