#!/usr/bin/env python3
"""Flatten a Bambu profile JSON by following `inherits` chains and merging `include` template JSONs.

The BambuStudio CLI (`--load-settings`) does NOT auto-resolve either:
  * `"inherits": "<parent_name>"` — sibling JSON in the same dir whose `name` matches
  * `"include": [...]` — array of sibling JSON `name`s whose contents merge in

Without this, the slice produces a 3MF with placeholder `machine_start_gcode`,
single-extruder collapse on dual printers (X2D), and missing `default_nozzle_volume_type`.
The desktop app's PresetBundle does this resolution automatically; the CLI doesn't.

Usage:
  resolve_profile.py <profile.json> -o <out.json>
  # writes a flat JSON with all inherited/included keys merged.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def index_dir(directory: Path) -> dict[str, Path]:
    """Map every JSON file in `directory` from its `name` field to its path."""
    out: dict[str, Path] = {}
    for p in directory.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        nm = data.get("name")
        if isinstance(nm, str):
            out[nm] = p
    return out


def load_chain(name: str, idx: dict[str, Path], visited: set[str]) -> dict:
    """Recursively load `name` plus its `inherits` chain, parents-first then child wins."""
    if name in visited:
        raise RuntimeError(f"cycle: {name} already in {visited}")
    visited.add(name)
    if name not in idx:
        raise FileNotFoundError(f"profile not found in dir: {name!r}")
    self_data = json.loads(idx[name].read_text())
    parent_name = self_data.get("inherits")
    if isinstance(parent_name, str) and parent_name:
        merged = load_chain(parent_name, idx, visited)
    else:
        merged = {}
    # `include` references are siblings (same dir); recurse without re-applying inherits there
    includes = self_data.get("include") or []
    for inc in includes:
        if inc not in idx:
            raise FileNotFoundError(f"include not found: {inc!r}")
        inc_data = json.loads(idx[inc].read_text())
        # Templates carry `instantiation: false`; strip metadata before merging
        for k in ("type", "name", "instantiation", "from", "inherits", "setting_id", "include"):
            inc_data.pop(k, None)
        merged.update(inc_data)
    # finally apply this node's own keys (child wins over parent)
    own = dict(self_data)
    own.pop("include", None)  # already processed
    own.pop("inherits", None)  # squashed
    merged.update(own)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", type=Path, help="Path to leaf profile JSON")
    ap.add_argument("-o", "--out", type=Path, required=True, help="Output flattened JSON")
    args = ap.parse_args()

    if not args.profile.exists():
        print(f"error: {args.profile} not found", file=sys.stderr)
        return 1
    idx = index_dir(args.profile.parent)
    leaf_data = json.loads(args.profile.read_text())
    leaf_name = leaf_data.get("name")
    if not leaf_name:
        print(f"error: {args.profile} has no `name` field", file=sys.stderr)
        return 2
    if leaf_name not in idx:
        idx[leaf_name] = args.profile
    flat = load_chain(leaf_name, idx, set())

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(flat, indent=2))
    n_keys = len(flat)
    has_start = bool(flat.get("machine_start_gcode"))
    has_change = bool(flat.get("change_filament_gcode"))
    n_nozzle = len(flat.get("nozzle_diameter") or [])
    n_offset = len(flat.get("extruder_offset") or [])
    print(
        f"wrote {args.out} ({n_keys} keys, "
        f"machine_start_gcode={'present' if has_start else 'MISSING'}, "
        f"change_filament_gcode={'present' if has_change else 'MISSING'}, "
        f"nozzle_diameter={n_nozzle}, extruder_offset={n_offset})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
