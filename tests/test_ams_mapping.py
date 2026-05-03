#!/usr/bin/env python3
"""Unit tests for the ams_mapping / ams_mapping2 wire shape that
x2d_bridge.start_print emits.

Single-color (one-filament) prints and multi-color (multi-filament,
N-extruder X2D) prints exercise different shapes:

  Single  ams_mapping  = [slot]
          ams_mapping2 = [{"ams_id": slot//4, "slot_id": slot%4}]

  Multi   ams_mapping  = [slot_0, slot_1, ...]
          ams_mapping2 = [{"ams_id":..., "slot_id":...}, ...]

The X2D firmware reads ams_mapping2 (the newer form) but rejects
prints whose mapping length doesn't equal the filament count, so this
test pins both shapes against expected values.

Runs on GHA — no printer needed (mocks publish out)."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _MockClient:
    """Stand-in for x2d_bridge.X2DClient that captures the published payload."""
    def __init__(self):
        self.creds = types.SimpleNamespace(serial="20P9AJ612700155")
        self.published: dict | None = None

    def publish(self, payload: dict, qos: int = 1) -> None:
        self.published = payload


def _expect(label: str, got, want) -> bool:
    if got != want:
        print(f"FAIL [{label}]: got={got!r} want={want!r}")
        return False
    print(f"  ✓ {label}")
    return True


def test_single_filament_int_slot() -> bool:
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=3)
    p = cli.published["print"]
    ok = True
    ok &= _expect("ams_mapping (single, int)",  p["ams_mapping"],  [3])
    ok &= _expect("ams_mapping2 (single, int)", p["ams_mapping2"], [{"ams_id": 0, "slot_id": 3}])
    ok &= _expect("use_ams (single, int)",      p["use_ams"], True)
    return ok


def test_single_filament_global_slot_into_ams1() -> bool:
    """slot 5 is AMS 1, tray 1 — verifies the //4 / %4 split for AMS#≥1."""
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=5)
    p = cli.published["print"]
    return _expect("ams_mapping2 ams_id=1 slot_id=1", p["ams_mapping2"],
                   [{"ams_id": 1, "slot_id": 1}])


def test_multi_filament_list() -> bool:
    """Two-color print: filament 0 -> AMS0 tray 1; filament 1 -> AMS1 tray 1."""
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "two_color.gcode.3mf", use_ams=True, ams_slot=[1, 5])
    p = cli.published["print"]
    ok = True
    ok &= _expect("ams_mapping (multi)", p["ams_mapping"], [1, 5])
    ok &= _expect("ams_mapping2 (multi)", p["ams_mapping2"],
                  [{"ams_id": 0, "slot_id": 1},
                   {"ams_id": 1, "slot_id": 1}])
    return ok


def test_no_ams_keeps_mappings_empty() -> bool:
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "ext_spool.gcode.3mf", use_ams=False, ams_slot=0)
    p = cli.published["print"]
    ok = True
    ok &= _expect("use_ams=False", p["use_ams"], False)
    ok &= _expect("ams_mapping empty",  p["ams_mapping"], [])
    ok &= _expect("ams_mapping2 empty", p["ams_mapping2"], [])
    return ok


def test_use_ams_true_empty_list_rejects() -> bool:
    """Passing use_ams=True with ams_slot=[] is a programmer error — must
    raise rather than silently produce a malformed payload that the
    firmware would drop without an HMS code."""
    import x2d_bridge
    cli = _MockClient()
    try:
        x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=[])
    except ValueError:
        print("  ✓ use_ams=True + ams_slot=[] rejected with ValueError")
        return True
    print("FAIL [use_ams=True + ams_slot=[]]: should raise ValueError, didn't")
    return False


def test_signed_publish_envelope_shape() -> bool:
    """Quick sanity check that the published payload is the inner dict
    BEFORE signing — sign_payload wraps it; we want to verify nothing
    accidentally moves the print block to a different key."""
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=0)
    return _expect("payload top-level keys", set(cli.published.keys()), {"print"})


def main() -> int:
    tests = [
        test_single_filament_int_slot,
        test_single_filament_global_slot_into_ams1,
        test_multi_filament_list,
        test_no_ams_keeps_mappings_empty,
        test_use_ams_true_empty_list_rejects,
        test_signed_publish_envelope_shape,
    ]
    passed = sum(1 for t in tests if t())
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
