#!/usr/bin/env python3
"""Remix a .gcode.3mf in-place: assign per-object extruder, override
shells / infill / layer-height, optionally re-scale.

This is the CLI companion to BambuStudio's per-object override panel
(items #82/#83/#84). Modifies Metadata/model_settings.config in the
3MF to add `<metadata key="..." value="..."/>` entries on the matching
`<object id="...">` block. The slicer reads these as per-object
overrides on top of the global preset at re-slice time.

Why this exists:
  * BambuStudio's UI route for assigning the X2D's right (auxiliary)
    extruder to an object as a primary (not just support) is buried
    in the Object List right-click ("Change Extruder"). This CLI
    surfaces it for batch + headless workflows.
  * On-the-fly remix without re-importing: rewrite shells/infill/
    layer-height per object then re-open in BambuStudio (or send
    straight to the printer if you already have plate_<N>.gcode).
  * Per-object overrides round-trip cleanly through the 3MF — open
    the remixed file in BambuStudio and the overrides appear in the
    Object List + the slicer respects them on next slice.

Usage:
    # Assign every object to the AUX (right) extruder
    python3 remix_3mf.py rumi_frame.gcode.3mf --extruder 2

    # Per-object: object id 2 -> extruder 2, all others -> extruder 1
    python3 remix_3mf.py model.3mf --extruder 1 --object 2:2

    # Override shells + infill density across the print
    python3 remix_3mf.py model.3mf \\
        --wall-loops 4 --sparse-infill 25 --layer-height 0.16

    # Rescale every object 110% on Z (taller print)
    python3 remix_3mf.py model.3mf --scale 1.0,1.0,1.10

    # Just print what's currently set per object
    python3 remix_3mf.py model.3mf --inspect

    # Strip all per-object overrides, returning to global presets
    python3 remix_3mf.py model.3mf --reset

The .gcode.3mf is rewritten in-place (atomic via temp + rename) unless
you pass --out NEW_PATH. The Metadata/plate_<N>.gcode files are NOT
re-generated — re-slice in BambuStudio (or `bambu-studio --slice
<file>`) after remixing if the print needs a fresh gcode. For
extruder-only changes that the printer applies at runtime via
filament_map (X2D firmware re-routes filament source per ams_mapping2)
no re-slice is needed.

Exit codes:
    0  rewrote (or inspected) successfully
    1  bad input / no objects matched
    2  3MF couldn't be opened / parsed
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


CONFIG_PATH = "Metadata/model_settings.config"

# Allowed override keys — slicer reads these per-object. The list is
# the union of what BambuStudio's PrintObjectConfig and PartConfig
# accept as keyable overrides; passing a bogus key would silently get
# ignored at slice time.
KNOWN_OBJECT_OVERRIDES = frozenset({
    "extruder",
    "wall_loops",
    "sparse_infill_density",
    "sparse_infill_pattern",
    "layer_height",
    "wall_filament",
    "sparse_infill_filament",
    "solid_infill_filament",
    "support_filament",
    "support_interface_filament",
    "top_shell_layers",
    "bottom_shell_layers",
    "first_layer_print_sequence",
    "outer_wall_speed",
    "infill_speed",
    "support",
    "support_type",
    "enable_support",
    "brim_type",
    "brim_width",
    "raft_layers",
    "ironing_type",
})


def _open_3mf(path: Path) -> tuple[zipfile.ZipFile, dict[str, bytes]]:
    """Read every member of the 3MF into memory so we can rewrite it."""
    with zipfile.ZipFile(path, "r") as zf:
        members = {info.filename: zf.read(info.filename) for info in zf.infolist()}
    return None, members


def _parse_object_id_kv(spec: str) -> tuple[int, int]:
    """Parse `OBJ_ID:EXTRUDER` like '2:2'. Raises SystemExit on bad input."""
    if ":" not in spec:
        sys.exit(f"--object expects OBJ_ID:EXTRUDER, got {spec!r}")
    a, b = spec.split(":", 1)
    try:
        return int(a), int(b)
    except ValueError:
        sys.exit(f"--object {spec!r}: both halves must be integers")


def _parse_scale(spec: str) -> tuple[float, float, float]:
    """Parse `Sx,Sy,Sz` (e.g. '1.0,1.0,1.10') or `S` (uniform)."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) == 1:
        s = float(parts[0])
        return (s, s, s)
    if len(parts) == 3:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    sys.exit(f"--scale expects S or Sx,Sy,Sz, got {spec!r}")


def _set_object_metadata(obj: ET.Element, key: str, value: str) -> None:
    """Replace or insert <metadata key="K" value="V"/> on the object."""
    for md in obj.findall("metadata"):
        if md.get("key") == key:
            md.set("value", value)
            return
    md = ET.SubElement(obj, "metadata")
    md.set("key", key)
    md.set("value", value)


def _drop_object_metadata(obj: ET.Element, key: str) -> bool:
    for md in obj.findall("metadata"):
        if md.get("key") == key:
            obj.remove(md)
            return True
    return False


def _scale_part_matrix(part: ET.Element, sx: float, sy: float, sz: float) -> bool:
    """Multiply the part's affine matrix in-place by diag(sx, sy, sz, 1).

    BambuStudio's matrix metadata is a 16-float row-major affine
    (right-multiplied — the 4th column is translation). We pre-multiply
    by S so the object scales around the origin; for centred scaling the
    user can re-arrange in-app afterwards.
    """
    for md in part.findall("metadata"):
        if md.get("key") != "matrix":
            continue
        try:
            mat = [float(x) for x in (md.get("value") or "").split()]
        except ValueError:
            return False
        if len(mat) != 16:
            return False
        # Row-major 4x4: [r0c0..r0c3 r1c0..r1c3 r2c0..r2c3 r3c0..r3c3].
        # Pre-multiply by diag(sx,sy,sz,1) -> scales rows 0,1,2.
        for i in range(4):
            mat[0 * 4 + i] *= sx
            mat[1 * 4 + i] *= sy
            mat[2 * 4 + i] *= sz
        md.set("value", " ".join(f"{v:g}" for v in mat))
        return True
    return False


def _walk_objects(root: ET.Element):
    for obj in root.findall("object"):
        try:
            oid = int(obj.get("id") or "-1")
        except ValueError:
            continue
        yield oid, obj


def cmd_inspect(root: ET.Element) -> int:
    n_objects = 0
    for oid, obj in _walk_objects(root):
        n_objects += 1
        # Filter out face-count-only <metadata face_count="N"/> blocks that
        # have no key attribute — they're mesh stats, not config overrides.
        kvs = {md.get("key"): md.get("value") for md in obj.findall("metadata")
               if md.get("key") is not None}
        name = kvs.pop("name", "?")
        print(f"object id={oid} name={name!r}")
        for k, v in sorted(kvs.items(), key=lambda kv: (kv[0] or "")):
            tag = "" if k in KNOWN_OBJECT_OVERRIDES else "  (unknown-key)"
            print(f"  {k} = {v!r}{tag}")
        for part in obj.findall("part"):
            pid = part.get("id") or "?"
            pkvs = {md.get("key"): md.get("value") for md in part.findall("metadata")}
            pname = pkvs.pop("name", "?")
            print(f"  part id={pid} name={pname!r}")
            for k, v in sorted(pkvs.items()):
                if k in KNOWN_OBJECT_OVERRIDES:
                    print(f"    {k} = {v!r}")
    print(f"\n{n_objects} object(s) total")
    return 0


def cmd_apply(args: argparse.Namespace, root: ET.Element) -> int:
    objects = list(_walk_objects(root))
    if not objects:
        sys.exit("no objects in 3MF (Metadata/model_settings.config has no <object> blocks)")

    # Build per-object override map.
    per_obj: dict[int, dict[str, str]] = {oid: {} for oid, _ in objects}
    if args.extruder is not None:
        for oid in per_obj:
            per_obj[oid]["extruder"] = str(args.extruder)
    for spec in args.object or []:
        oid, ex = _parse_object_id_kv(spec)
        if oid not in per_obj:
            sys.exit(f"--object {oid}: no object with that id (have: "
                     f"{sorted(per_obj)})")
        per_obj[oid]["extruder"] = str(ex)

    flag_map = {
        "wall_loops": args.wall_loops,
        "sparse_infill_density": args.sparse_infill,
        "sparse_infill_pattern": args.sparse_infill_pattern,
        "layer_height": args.layer_height,
        "top_shell_layers": args.top_shells,
        "bottom_shell_layers": args.bottom_shells,
    }
    for k, v in flag_map.items():
        if v is None:
            continue
        sval = str(v)
        # Density-style ints accept "%" or bare "25"; canonicalise to "%".
        if k == "sparse_infill_density" and not sval.endswith("%"):
            sval += "%"
        for oid in per_obj:
            per_obj[oid][k] = sval

    scale = _parse_scale(args.scale) if args.scale else None

    # Apply. --reset runs FIRST so combinable invocations like
    # `--reset --extruder 1` end up with just the freshly-set extruder
    # rather than racing the reset against the new value.
    rewrites = 0
    if args.reset:
        for oid, obj in objects:
            for key in list(KNOWN_OBJECT_OVERRIDES):
                if _drop_object_metadata(obj, key):
                    rewrites += 1

    for oid, obj in objects:
        for key, val in per_obj[oid].items():
            if key not in KNOWN_OBJECT_OVERRIDES:
                print(f"warning: skipping unknown key {key!r}", file=sys.stderr)
                continue
            _set_object_metadata(obj, key, val)
            rewrites += 1
        if scale is not None:
            for part in obj.findall("part"):
                if _scale_part_matrix(part, *scale):
                    rewrites += 1

    if rewrites == 0:
        sys.exit("nothing to change — pass --extruder / --object / --wall-loops / "
                 "--sparse-infill / --layer-height / --scale / --reset, "
                 "or --inspect to see current state.")
    return rewrites


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", type=Path, help=".gcode.3mf to remix in-place")
    ap.add_argument("--out", type=Path,
                    help="Write the remixed 3MF to a new path instead of "
                         "rewriting in-place.")
    ap.add_argument("--inspect", action="store_true",
                    help="Print every object's current metadata + per-part "
                         "overrides, then exit (no write).")
    ap.add_argument("--extruder", type=int,
                    help="Assign every object to this extruder (1=left, "
                         "2=right). For X2D this is the primary mechanism "
                         "to use the AUX nozzle as a body extruder, not "
                         "just for support material.")
    ap.add_argument("--object", action="append", default=[],
                    metavar="OBJ_ID:EXTRUDER",
                    help="Per-object override, e.g. '2:2' to send object "
                         "id 2 to extruder 2. Stack multiple --object "
                         "flags for multi-object prints.")
    ap.add_argument("--wall-loops", type=int)
    ap.add_argument("--sparse-infill", help="Density % (int 0..100)")
    ap.add_argument("--sparse-infill-pattern",
                    help="grid|honeycomb|gyroid|cubic|monotonic|adaptive_cubic|...")
    ap.add_argument("--layer-height", type=float)
    ap.add_argument("--top-shells", type=int)
    ap.add_argument("--bottom-shells", type=int)
    ap.add_argument("--scale",
                    help="Uniform 'S' or 'Sx,Sy,Sz' (e.g. '1.0,1.0,1.10' "
                         "to make Z 10% taller). Pre-multiplies each part's "
                         "matrix.")
    ap.add_argument("--reset", action="store_true",
                    help="Strip every per-object override, returning to the "
                         "global preset. Combinable with the override flags "
                         "to do a clean reset+set.")
    args = ap.parse_args()

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr); return 1
    try:
        _, members = _open_3mf(args.file)
    except (zipfile.BadZipFile, KeyError) as e:
        print(f"can't open 3MF: {e}", file=sys.stderr); return 2
    if CONFIG_PATH not in members:
        print(f"3MF missing {CONFIG_PATH} — not a BambuStudio export?",
              file=sys.stderr); return 2

    root = ET.fromstring(members[CONFIG_PATH])

    if args.inspect:
        return cmd_inspect(root)

    rewrites = cmd_apply(args, root)
    print(f"applied {rewrites} override(s)")

    # Serialise the updated config back into the member dict.
    new_xml = ET.tostring(root, xml_declaration=True, encoding="UTF-8")
    members[CONFIG_PATH] = new_xml

    out_path = args.out or args.file
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    tmp.replace(out_path)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
