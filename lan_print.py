#!/usr/bin/env python3
"""End-to-end Bambu LAN print — query AMS state, upload .gcode.3mf via FTPS,
then issue a *signed* MQTT start_print command with an ams_mapping that
points at the AMS slot whose filament matches what the 3MF was sliced for.

Workflow:
  1. Connect to the printer (FTPS for upload + signed-MQTT for state/control)
     using x2d_bridge's X2DClient. The MQTT connection takes a few seconds
     to receive the first `pushall` state report, after which we read AMS
     contents.
  2. Walk every AMS tray on the printer. Match by --filament-match (default
     "Silk", case-insensitive substring against tray_sub_brands and
     tray_id_name). Refuse to proceed if zero or >1 matching trays are
     found unless --slot is forced explicitly.
  3. Cross-check that the matched tray's filament type agrees with the
     filament_type baked into the 3MF's project_settings.config (e.g. PLA
     in 3MF must match PLA in the slot, otherwise the printer would refuse
     anyway after the file load).
  4. Upload the file via x2d_bridge's implicit-FTPS uploader.
  5. Submit the full Jan-2025+ firmware-required project_file payload via
     x2d_bridge.start_print, which handles RSA-SHA256 signing with the
     publicly-leaked Bambu Connect cert (X2D / H2D / refreshed P1+X1
     firmwares reject unsigned publishes with err_code 84033543).

Why this used to use bambulabs_api and doesn't anymore:
  bambulabs_api 2.6.6 ships unsigned MQTT and a minimal start_print payload
  shape that the X2D's command handler validates against and silently
  drops. We share x2d_bridge.py's mature Creds / X2DClient / start_print /
  upload_file helpers instead, so this CLI uses the same code paths as the
  GUI shim's bridge.

Usage:
    python3 lan_print.py --file rumi_frame.gcode.3mf
        # Reads ip/code/serial from ~/.x2d/credentials [printer] section.

    python3 lan_print.py --ip 192.168.x.y --code 12345678 --serial 03ABC... \\
        --file rumi_frame.gcode.3mf

    # Force a specific slot, skip auto-match:
    python3 lan_print.py ... --slot 3

    # Match by a different keyword:
    python3 lan_print.py ... --filament-match "PETG-CF"

    # Dry-run: print AMS state + the auto-matched slot, then exit.
    python3 lan_print.py ... --query-only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from pathlib import Path

# Reuse x2d_bridge's signing/connection/upload/start_print path. This
# requires bambu_cert.py (publicly-leaked Bambu Connect signing key) to
# be importable next to x2d_bridge.py.
import x2d_bridge

log = logging.getLogger("lan_print")


def read_3mf_filament_types(path: Path) -> list[str]:
    """Pull `filament_type` out of the 3MF's project_settings.config."""
    with zipfile.ZipFile(path, "r") as z:
        with z.open("Metadata/project_settings.config") as f:
            cfg = json.load(f)
    return cfg.get("filament_type") or []


def read_3mf_bed_type(path: Path, plate: int = 1) -> str | None:
    """Pull bed_type from Metadata/plate_<N>.json — the per-plate truth.

    project_settings.config doesn't carry bed_type; only the plate JSON
    does. Mismatching bed_type at start_print time triggers a printer
    warning (sometimes refusal). Auto-reading this avoids relying on the
    user remembering --bed-type for every print.
    """
    try:
        with zipfile.ZipFile(path, "r") as z:
            with z.open(f"Metadata/plate_{plate}.json") as f:
                pj = json.load(f)
        return pj.get("bed_type")
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None


def collect_trays(state: dict | None) -> list[dict]:
    """Flatten the printer's AMS hub into a list of {slot_index, type, sub_brand, color}.

    `slot_index` is the global ams_mapping index Bambu firmware expects:
    AMS unit U with tray index T (both 0-indexed) becomes U*4 + T. So slot
    A4 of the first AMS == 3 in the firmware's wire format.

    `state` is the raw printer report (the JSON dict X2DClient.request_state
    returns). Walks `state["print"]["ams"]["ams"]` directly because the
    X2D's tray records omit some fields older library helpers expect.
    """
    if not state:
        return []
    ams_block = (state.get("print") or {}).get("ams") or {}
    ams_units = ams_block.get("ams") or []
    trays: list[dict] = []
    for unit in ams_units:
        ams_idx = int(unit.get("id", 0))
        for tray in unit.get("tray", []):
            # state == 0 means empty slot; skip
            if int(tray.get("state", 0)) == 0:
                continue
            tray_idx = int(tray.get("id", 0))
            trays.append({
                "slot_index": ams_idx * 4 + tray_idx,
                "ams": ams_idx,
                "tray": tray_idx,
                "type": tray.get("tray_type"),
                "sub_brand": tray.get("tray_sub_brands") or "",
                "id_name": tray.get("tray_id_name") or "",
                "info_idx": tray.get("tray_info_idx") or "",
                "color": tray.get("tray_color"),
            })
    return trays


def _norm_color(c: str) -> str:
    """Normalise a color spec for comparison.

    Accepts:
      - "#RRGGBB"      -> "RRGGBBFF"
      - "#RRGGBBAA"    -> "RRGGBBAA"
      - "RRGGBBAA"     -> "RRGGBBAA"
      - "RRGGBB"       -> "RRGGBBFF"
      - "0xRRGGBBAA"   -> "RRGGBBAA"
    All upper-case so the printer's `tray_color` (already upper, e.g.
    "057748FF") compares directly with anything the user types.
    """
    if not c:
        return ""
    s = c.strip().upper().lstrip("#")
    if s.startswith("0X"):
        s = s[2:]
    if len(s) == 6:
        s += "FF"
    return s


def _color_distance(a: str, b: str) -> int | None:
    """Manhattan distance between two normalised RGBA hex colors, or None
    if either is malformed. Lets us pick the closest tray when an exact
    color match isn't found (--filament-color-fuzzy)."""
    try:
        ra, ga, ba = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        rb, gb, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    except (ValueError, IndexError):
        return None
    return abs(ra - rb) + abs(ga - gb) + abs(ba - bb)


def match_slot(trays: list[dict], *,
               match_substr: str = "",
               match_color: str = "",
               match_info_idx: str = "",
               color_fuzzy_max: int | None = None,
               expected_type: str | None = None) -> dict:
    """Find the unique tray matching the given criteria.

    Match modes (apply in this priority order — first non-empty wins):
      1. `match_color`  exact RGBA hex match against `tray_color`. With
         `color_fuzzy_max` set, falls back to nearest within that
         Manhattan distance threshold (e.g. 30 ≈ visually close).
      2. `match_info_idx` exact (case-insensitive) match against
         `tray_info_idx` — Bambu's filament catalog ID like "GFL95".
      3. `match_substr` case-insensitive substring against tray_sub_brands
         and tray_id_name. The X2D omits these; on its loadouts this
         matcher will silently match nothing.
      4. If nothing was given, the only candidate filter is
         `expected_type` (e.g. "PLA"); we error out if more than one
         tray matches that alone.

    `expected_type` filters every mode — mismatched filament_type trays
    are dropped before reporting "ambiguous" or "no match".
    """
    candidates: list[dict] = []
    for t in trays:
        if expected_type and (t.get("type") or "").upper() != expected_type.upper():
            continue
        if match_color:
            want = _norm_color(match_color)
            have = _norm_color(t.get("color") or "")
            if want and have == want:
                candidates.append(t)
            continue
        if match_info_idx:
            if (t.get("info_idx") or "").upper() == match_info_idx.upper():
                candidates.append(t)
            continue
        if match_substr:
            needle = match_substr.lower()
            haystacks = [t.get("sub_brand") or "", t.get("id_name") or ""]
            if any(needle in (h or "").lower() for h in haystacks):
                candidates.append(t)
            continue
        # No criteria given: every tray of expected_type is a candidate.
        candidates.append(t)

    # Color fuzzy fallback: if exact color match returned nothing, try
    # nearest-within-threshold among the type-filtered trays.
    if not candidates and match_color and color_fuzzy_max is not None:
        want = _norm_color(match_color)
        scored: list[tuple[int, dict]] = []
        for t in trays:
            if expected_type and (t.get("type") or "").upper() != expected_type.upper():
                continue
            d = _color_distance(want, _norm_color(t.get("color") or ""))
            if d is not None and d <= color_fuzzy_max:
                scored.append((d, t))
        if scored:
            scored.sort(key=lambda p: p[0])
            return scored[0][1]

    criteria_desc = (
        f"color={match_color!r}" if match_color
        else f"info_idx={match_info_idx!r}" if match_info_idx
        else f"substring={match_substr!r}"
    )
    if not candidates:
        raise SystemExit(
            f"No AMS tray matches {criteria_desc}"
            + (f" of type {expected_type}" if expected_type else "")
            + ". Available trays:\n"
            + "\n".join(f"  slot {t['slot_index']}: type={t.get('type')!r} "
                       f"color={t.get('color')!r} info_idx={t.get('info_idx')!r}"
                       for t in trays)
        )
    if len(candidates) > 1:
        raise SystemExit(
            f"Multiple trays match {criteria_desc}; pass --slot N to disambiguate:\n"
            + "\n".join(f"  slot {t['slot_index']}: type={t.get('type')!r} "
                       f"color={t.get('color')!r} info_idx={t.get('info_idx')!r}"
                       for t in candidates)
        )
    return candidates[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Printer credentials — match x2d_bridge's flag names so Creds.resolve() works.
    ap.add_argument("--ip", default="", help="Printer IP. Defaults to ~/.x2d/credentials.")
    ap.add_argument("--code", default="",
                    help="Printer LAN access code (8 hex chars). "
                         "Defaults to ~/.x2d/credentials.")
    ap.add_argument("--serial", default="",
                    help="Printer serial. Defaults to ~/.x2d/credentials.")
    ap.add_argument("--printer", default="",
                    help="Named [printer:NAME] section in ~/.x2d/credentials.")
    ap.add_argument("--file", required=True, type=Path,
                    help="Path to the .gcode.3mf to upload+print")
    ap.add_argument("--plate", type=int, default=1, help="Plate number in the 3MF (default 1)")
    # Match modes — pick exactly one. Color is the most reliable on X2D
    # since the firmware's MQTT report omits sub_brand/id_name strings on
    # some loadouts (so --filament-match would match nothing). info_idx
    # is the next-best — Bambu's catalog ID like "GFL95" — same idea but
    # tied to the filament SKU rather than its color.
    ap.add_argument("--filament-match", default="",
                    help="Case-insensitive substring matched against tray_sub_brands "
                         "and tray_id_name. Pre-X2D printers populate these; X2D "
                         "often doesn't — prefer --filament-color or --filament-info-idx.")
    ap.add_argument("--filament-color", default="",
                    help='RGBA hex of the AMS slot color, e.g. "#057748" or '
                         '"057748FF". Compares against the tray\'s tray_color '
                         '(the firmware reports it upper-case with alpha=FF).')
    ap.add_argument("--filament-color-fuzzy", type=int, default=None,
                    metavar="MAX",
                    help="With --filament-color, if no exact match falls back to "
                         "the nearest tray within MAX Manhattan distance in RGB "
                         "(0-765). 30 ≈ visually close, 100 = same family.")
    ap.add_argument("--filament-info-idx", default="",
                    help='Bambu filament catalog ID, e.g. "GFL95" (Bambu Basic '
                         'PLA Bambu Green) or "GFA05" (Bambu Generic PLA). '
                         'Inspect AMS state with --query-only to see what '
                         'each loaded tray reports.')
    ap.add_argument("--slot", type=int, default=None,
                    help="Force a specific global AMS slot index (skips auto-match)")
    ap.add_argument("--no-flow-cali", action="store_true",
                    help="Disable per-print flow calibration (faster start, tiny risk)")
    ap.add_argument("--bed-type", default="",
                    help="textured_plate | cool_plate | engineering_plate | "
                         "high_temp_plate. If unset, read from the 3MF's "
                         "Metadata/plate_<N>.json (the per-plate truth) and "
                         "fall back to textured_plate only if that's missing.")
    ap.add_argument("--query-only", action="store_true",
                    help="Connect, dump AMS contents + matched slot, then exit "
                         "without uploading or starting a print. Use this to verify "
                         "auto-matching before committing to a real print run.")
    ap.add_argument("--verbose", "-v", action="count", default=0)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else
              logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("lan_print").setLevel(logging.INFO)

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 1

    fil_types = read_3mf_filament_types(args.file)
    log.info("3MF declares %d filament(s): %s", len(fil_types), fil_types)
    if len(fil_types) != 1:
        log.warning("3MF has %d filaments; ams_mapping will be a list of length %d",
                    len(fil_types), len(fil_types))
    expected_type = fil_types[0] if fil_types else None

    creds = x2d_bridge.Creds.resolve(args)
    log.info("Connecting (signed MQTT) to %s [%s]…", creds.ip, creds.serial)
    client = x2d_bridge.X2DClient(creds)
    client.connect()

    # request_state sends a signed `pushall` and waits for the next report.
    # The X2D usually replies within a second; AMS comes in either the same
    # message or the immediately-following push. Poll a few times so we
    # don't race the second push.
    state = None
    trays: list[dict] = []
    for i in range(8):  # ~8 s budget total
        try:
            state = client.request_state(timeout=1.5)
        except TimeoutError:
            continue
        trays = collect_trays(state)
        if trays:
            break
        time.sleep(0.5)
    if not trays:
        client.disconnect()
        raise SystemExit(
            "Timed out waiting for AMS state report. Is the AMS attached and "
            "powered? (The X2D will sometimes report no AMS if a tray is mid-load.)"
        )

    log.info("AMS contents (%d slot(s)):", len(trays))
    for t in trays:
        log.info("  slot %d (AMS%d.tray%d): %s %s color=%s",
                 t["slot_index"], t["ams"], t["tray"],
                 t.get("type"), t.get("sub_brand"), t.get("color"))

    def _do_match() -> dict:
        return match_slot(
            trays,
            match_substr=args.filament_match,
            match_color=args.filament_color,
            match_info_idx=args.filament_info_idx,
            color_fuzzy_max=args.filament_color_fuzzy,
            expected_type=expected_type,
        )

    if args.query_only:
        log.info("--query-only set; not uploading or printing. Exiting.")
        if args.slot is None:
            try:
                chosen = _do_match()
                log.info("Would auto-match slot %d (type=%s color=%s info_idx=%s)",
                         chosen["slot_index"], chosen.get("type"),
                         chosen.get("color"), chosen.get("info_idx"))
            except SystemExit as e:
                log.warning("Auto-match would fail: %s", e)
        client.disconnect()
        return 0

    if args.slot is not None:
        forced = next((t for t in trays if t["slot_index"] == args.slot), None)
        if forced is None:
            client.disconnect()
            raise SystemExit(f"--slot {args.slot} not found in AMS state")
        chosen = forced
        log.info("Using forced slot %d", args.slot)
    else:
        chosen = _do_match()
        log.info("Auto-matched slot %d (type=%s color=%s info_idx=%s)",
                 chosen["slot_index"], chosen.get("type"),
                 chosen.get("color"), chosen.get("info_idx"))

    # Resolve bed_type: explicit --bed-type wins; else read from
    # Metadata/plate_<N>.json (the per-plate truth); else textured.
    bed_type = args.bed_type
    if not bed_type:
        bed_type = read_3mf_bed_type(args.file, args.plate) or "textured_plate"
        if bed_type != "textured_plate":
            log.info("Using bed_type=%r from plate_%d.json", bed_type, args.plate)

    fname = args.file.name
    log.info("Uploading %s (%d B) via implicit-FTPS…", fname, args.file.stat().st_size)
    x2d_bridge.upload_file(creds, args.file, remote_name=fname)
    log.info("Upload complete")

    log.info("Sending signed start_print(plate=%d, ams_slot=%d, bed=%s, flow_cali=%s)",
             args.plate, chosen["slot_index"], bed_type, not args.no_flow_cali)
    try:
        x2d_bridge.start_print(
            client, fname,
            use_ams=True,
            ams_slot=chosen["slot_index"],
            flow_cali=not args.no_flow_cali,
            bed_type=bed_type,
            local_path=args.file,
        )
        log.info("start_print signed-published — printer accepted the command")
        rc = 0
    except Exception as e:
        log.error("start_print failed: %s", e)
        rc = 2

    client.disconnect()
    return rc


if __name__ == "__main__":
    sys.exit(main())
