"""End-to-end test for the AMS color → filament profile matcher
(item #58).

Covers:

* exact-color match returns distance 0 + the catalog's profile name
* near-color match (off by a few RGB points) returns the right
  catalog entry with a small distance
* material filter narrows to PLA Basic vs PLA Silk
* alpha byte stripped (8-char hex tolerated)
* invalid color → None
* empty material falls back to whole catalog
* state_for() walks all 4 AMS slots and returns one entry per slot,
  including empty bays
* HTTP `GET /colorsync/match` and `GET /colorsync/state` round-trip
  the matcher through the bridge daemon
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import x2d_bridge
from runtime.colorsync.mapper import match, state_for, _load_catalog


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    failed: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    cat = _load_catalog()
    check("catalog loaded ≥ 100 entries",
          len(cat) >= 100, detail=f"got {len(cat)}")

    # ----- 1. exact-color match ----------------------------------
    # FF6A13FF is "Bambu PLA Basic Orange" code 10300.
    m = match("FF6A13FF", material="PLA")
    check("exact match FF6A13FF returns a result", m is not None)
    if m:
        check("exact match distance == 0", m.distance == 0,
              detail=str(m.distance))
        check("exact match name == Orange",
              m.fila_color_name == "Orange", detail=m.fila_color_name)
        check("exact match fila_type == PLA Basic",
              m.fila_type == "PLA Basic", detail=m.fila_type)
        check("exact match fila_id == GFA00",
              m.fila_id == "GFA00", detail=m.fila_id)
        check("exact match fila_color_code == 10300",
              m.fila_color_code == "10300", detail=m.fila_color_code)
        check("profile matches Bambu PLA Basic … @BBL X2D",
              m.profile.startswith("Bambu PLA Basic")
              and m.profile.endswith("@BBL X2D"),
              detail=m.profile)

    # ----- 2. near-color match: AF7933 is close to a brown PLA --
    m = match("AF7933", material="PLA")
    check("near-color AF7933 returns a match", m is not None)
    if m:
        check("near-color distance reasonable (<60 RGB units)",
              m.distance < 60, detail=str(m.distance))
        check("near-color material is PLA family",
              m.fila_type.startswith("PLA"), detail=m.fila_type)
        print(f"    (AF7933 → {m.profile} @ d={m.distance})")

    # ----- 3. alpha byte stripped --------------------------------
    m6 = match("FF6A13",   material="PLA")
    m8 = match("FF6A13FF", material="PLA")
    check("6-char and 8-char hex match equivalently",
          m6 and m8 and m6.fila_color_code == m8.fila_color_code,
          detail=f"6={m6 and m6.fila_color_code} "
                 f"8={m8 and m8.fila_color_code}")

    # ----- 4. invalid input --------------------------------------
    check("invalid hex returns None",
          match("ZZZZZZ", material="PLA") is None)
    check("empty input returns None",
          match("", material="PLA") is None)

    # ----- 5. material filter narrows correctly ------------------
    pla_silk_match  = match("FFFFFF", material="PLA Silk")
    pla_basic_match = match("FFFFFF", material="PLA Basic")
    check("PLA Silk match returned",
          pla_silk_match is not None
          and "Silk" in pla_silk_match.fila_type,
          detail=str(pla_silk_match and pla_silk_match.fila_type))
    check("PLA Basic match returned",
          pla_basic_match is not None
          and pla_basic_match.fila_type == "PLA Basic",
          detail=str(pla_basic_match and pla_basic_match.fila_type))

    # ----- 6. empty material falls back to whole catalog --------
    m = match("FF6A13", material="")
    check("empty material still matches",
          m is not None and m.distance == 0)

    # ----- 7. state_for() walks every slot ----------------------
    fake_state = {"print": {"ams": {"ams": [{"id": 0, "tray": [
        {"tray_color": "FF6A13FF", "tray_type": "PLA"},
        {"tray_color": "AF7933FF", "tray_type": "PLA"},
        {"tray_color": "F95D73FF", "tray_type": "PLA"},
        {},  # empty bay
    ]}], "tray_now": "0"}}}
    out = state_for(fake_state)
    check("state_for returns 4 slot entries", len(out) == 4)
    if len(out) == 4:
        check("slot 1 has match",       out[0]["match"] is not None)
        check("slot 1 color preserved", out[0]["color"] == "FF6A13FF")
        check("slot 1 material PLA",    out[0]["material"] == "PLA")
        check("slot 4 (empty) has match=None",
              out[3]["match"] is None)
        check("slot 4 color is empty",  out[3]["color"] == "")
        # Slot order is sequential (1,2,3,4)
        check("slot indices are 1..4",
              [s["slot"] for s in out] == [1, 2, 3, 4])

    # ----- 8. HTTP /colorsync/match + /colorsync/state ----------
    port = _free_port()
    threading.Thread(
        target=x2d_bridge._serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     lambda _p: fake_state,
            "get_last_ts":   lambda _p: time.time() - 1,
            "max_staleness": 30.0,
            "auth_token":    None,
            "printer_names": [""],
            "clients":       {"": object()},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True, name="cs-http-bridge",
    ).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"

    with urllib.request.urlopen(
            base + "/colorsync/match?color=FF6A13&material=PLA",
            timeout=5) as r:
        body = json.loads(r.read())
    check("GET /colorsync/match status 200", r.status == 200)
    check("/colorsync/match returns Orange",
          body.get("fila_color_name") == "Orange",
          detail=str(body))
    check("/colorsync/match returns distance 0",
          body.get("distance") == 0, detail=str(body.get("distance")))

    with urllib.request.urlopen(
            base + "/colorsync/state", timeout=5) as r:
        body = json.loads(r.read())
    check("GET /colorsync/state status 200", r.status == 200)
    slots = body.get("printers", {}).get("", [])
    check("/colorsync/state returns 4 slot entries",
          len(slots) == 4, detail=str(len(slots)))
    if len(slots) == 4:
        check("/colorsync/state slot 1 matches Orange",
              slots[0].get("match", {}).get("fila_color_name") == "Orange",
              detail=str(slots[0]))

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — colorsync mapper (#58)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
