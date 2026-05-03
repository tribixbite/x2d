#!/usr/bin/env python3
"""Round-trip tests for remix_3mf.py — guards items #82/#83/#84.

  #82 — per-object extruder assignment ends up in model_settings.config
        as `<metadata key="extruder" value="N"/>`, recoverable via
        --inspect, and BambuStudio's slicer / X2D firmware will read
        it as the per-object override.

  #83 — wall_loops / sparse_infill_density / layer_height / shell
        counts also serialise to model_settings.config metadata keys.

  #84 — overrides survive a write+re-read cycle (no metadata loss
        between zip writes).

Runs against the repo's rumi_frame.gcode.3mf as the fixture so we
exercise a real BambuStudio export, not a hand-rolled stub.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remix_3mf  # noqa: WPS433


FIXTURE = Path(__file__).resolve().parent.parent / "rumi_frame.gcode.3mf"
TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp"))


def _read_overrides(path: Path) -> dict[int, dict[str, str]]:
    with zipfile.ZipFile(path, "r") as zf:
        xml = zf.read(remix_3mf.CONFIG_PATH)
    root = ET.fromstring(xml)
    out: dict[int, dict[str, str]] = {}
    for obj in root.findall("object"):
        try: oid = int(obj.get("id") or "-1")
        except ValueError: continue
        out[oid] = {md.get("key"): md.get("value")
                    for md in obj.findall("metadata")
                    if md.get("key") in remix_3mf.KNOWN_OBJECT_OVERRIDES}
    return out


def _expect(label: str, got, want) -> bool:
    if got != want:
        print(f"FAIL [{label}]: got={got!r} want={want!r}")
        return False
    print(f"  ✓ {label}")
    return True


def test_82_extruder_assignment() -> bool:
    with tempfile.TemporaryDirectory(dir=TMP_ROOT) as td:
        path = Path(td) / "remix82.gcode.3mf"
        shutil.copy(FIXTURE, path)
        # Simulate the CLI: assign extruder=2 to all objects.
        sys.argv = ["remix_3mf.py", str(path), "--extruder", "2"]
        rc = remix_3mf.main()
        if rc != 0:
            print(f"FAIL: remix CLI rc={rc}")
            return False
        ovr = _read_overrides(path)
        # rumi_frame has one object id=2; expect extruder 2.
        return _expect("#82 single-object extruder", ovr, {2: {"extruder": "2"}})


def test_83_remix_overrides() -> bool:
    with tempfile.TemporaryDirectory(dir=TMP_ROOT) as td:
        path = Path(td) / "remix83.gcode.3mf"
        shutil.copy(FIXTURE, path)
        sys.argv = ["remix_3mf.py", str(path),
                    "--wall-loops", "4",
                    "--sparse-infill", "25",
                    "--layer-height", "0.16",
                    "--top-shells", "5"]
        rc = remix_3mf.main()
        if rc != 0:
            print(f"FAIL: remix CLI rc={rc}")
            return False
        ovr = _read_overrides(path).get(2, {})
        ok = True
        ok &= _expect("#83 wall_loops",            ovr.get("wall_loops"),            "4")
        ok &= _expect("#83 sparse_infill_density", ovr.get("sparse_infill_density"), "25%")
        ok &= _expect("#83 layer_height",          ovr.get("layer_height"),          "0.16")
        ok &= _expect("#83 top_shell_layers",      ovr.get("top_shell_layers"),      "5")
        return ok


def test_84_persistence_round_trip() -> bool:
    """Apply overrides, re-open with the CLI, ensure the second --inspect
    pass sees exactly the values the first pass wrote (no zip-rewrite
    corruption / metadata loss / encoding mangling)."""
    with tempfile.TemporaryDirectory(dir=TMP_ROOT) as td:
        path = Path(td) / "remix84.gcode.3mf"
        shutil.copy(FIXTURE, path)
        sys.argv = ["remix_3mf.py", str(path), "--extruder", "2",
                    "--wall-loops", "3", "--sparse-infill", "33"]
        if remix_3mf.main() != 0:
            print("FAIL: first apply"); return False
        first = _read_overrides(path)
        # Re-open and apply a no-op (just adding another override) — should
        # still see the previous overrides untouched.
        sys.argv = ["remix_3mf.py", str(path), "--top-shells", "6"]
        if remix_3mf.main() != 0:
            print("FAIL: second apply"); return False
        second = _read_overrides(path)
        ok = True
        ok &= _expect("#84 first-apply extruder kept",
                      second.get(2, {}).get("extruder"), "2")
        ok &= _expect("#84 first-apply wall_loops kept",
                      second.get(2, {}).get("wall_loops"), "3")
        ok &= _expect("#84 first-apply sparse_infill_density kept",
                      second.get(2, {}).get("sparse_infill_density"), "33%")
        ok &= _expect("#84 second-apply top_shell_layers added",
                      second.get(2, {}).get("top_shell_layers"), "6")
        return ok


def test_84_reset_then_set() -> bool:
    """--reset wipes overrides, then a fresh --extruder writes a clean
    state. Combinable in one invocation."""
    with tempfile.TemporaryDirectory(dir=TMP_ROOT) as td:
        path = Path(td) / "remix84r.gcode.3mf"
        shutil.copy(FIXTURE, path)
        sys.argv = ["remix_3mf.py", str(path),
                    "--wall-loops", "8", "--sparse-infill", "50"]
        if remix_3mf.main() != 0:
            print("FAIL: pre-set"); return False
        sys.argv = ["remix_3mf.py", str(path), "--reset", "--extruder", "1"]
        if remix_3mf.main() != 0:
            print("FAIL: reset+set"); return False
        ovr = _read_overrides(path).get(2, {})
        # After --reset the only override should be the freshly-set extruder.
        # wall_loops + sparse_infill_density must be GONE.
        ok = True
        ok &= _expect("#84 reset cleared wall_loops",
                      "wall_loops" in ovr, False)
        ok &= _expect("#84 reset cleared sparse_infill_density",
                      "sparse_infill_density" in ovr, False)
        ok &= _expect("#84 reset+set extruder",
                      ovr.get("extruder"), "1")
        return ok


def main() -> int:
    if not FIXTURE.exists():
        print(f"FIXTURE not found: {FIXTURE}", file=sys.stderr)
        return 1
    tests = [
        test_82_extruder_assignment,
        test_83_remix_overrides,
        test_84_persistence_round_trip,
        test_84_reset_then_set,
    ]
    passed = sum(1 for t in tests if t())
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
