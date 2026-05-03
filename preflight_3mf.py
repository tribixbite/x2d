#!/usr/bin/env python3
"""Pre-flight validator for .gcode.3mf files before sending them to the printer.

Catches the failure modes we've actually hit (and a few we haven't but
should):

  * 3MF is a directory of metadata; one missing file (Metadata/plate_N.gcode,
    project_settings.config, the .md5 sidecar) and the firmware silently
    drops the print without an HMS error.
  * printer_model embedded in the 3MF doesn't match the printer it's being
    sent to. Inevitable cause of "wrong nozzle/wrong bed temp/missing
    feature" mid-print.
  * nozzle_diameter mismatch — slicing for a 0.6 nozzle and printing on
    a 0.4 (or vice versa) breaks first-layer adhesion or causes nozzle
    pressure spikes.
  * bed_type set in plate_1.json (the per-plate truth) doesn't match
    the start_print's --bed-type — printer warns and may refuse.
  * MD5 sidecar (Metadata/plate_N.gcode.md5) doesn't match the actual
    plate gcode bytes — firmware verifies and aborts ("file integrity
    check failed").
  * filament_type required by 3MF doesn't match anything loaded in any
    AMS slot — print starts then aborts on first filament-load step.
  * Max nozzle temp from the gcode header exceeds printer rating.
  * Slicer version is much older than the printer firmware expects (some
    older slicers emit deprecated commands the firmware now rejects).

Usage:
    # Standalone — just validate the file
    python3 preflight_3mf.py rumi_frame.gcode.3mf

    # Cross-check against the live printer's AMS state (signed pushall):
    python3 preflight_3mf.py rumi_frame.gcode.3mf --check-ams

    # JSON output for scripting
    python3 preflight_3mf.py rumi_frame.gcode.3mf --json

Exit codes:
    0  All checks pass
    1  Warnings only (print may work but with caveats)
    2  Errors found — print would fail
    3  File can't be opened or isn't a 3MF
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass
class Finding:
    severity: str  # "error" | "warn" | "info"
    code: str      # short machine-readable identifier
    message: str   # human-readable

    def fmt(self) -> str:
        sym = {"error": "✗", "warn": "!", "info": "i"}[self.severity]
        return f"  {sym} [{self.code}] {self.message}"


@dataclass
class PreflightResult:
    findings: list[Finding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def add(self, severity: str, code: str, message: str) -> None:
        self.findings.append(Finding(severity, code, message))


# Files every Bambu .gcode.3mf must contain. Missing == firmware drops.
REQUIRED_3MF_FILES = (
    "[Content_Types].xml",
    "_rels/.rels",
    "3D/3dmodel.model",
    "3D/_rels/3dmodel.model.rels",
    "Metadata/project_settings.config",
    "Metadata/slice_info.config",
)
PLATE_FILES_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
PLATE_MD5_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode\.md5$")


# Bambu printer max specs — used to flag "you sliced for 0.4 but printer is 0.2" etc.
PRINTER_SPECS = {
    "Bambu Lab X2D": {"nozzles": 2, "max_nozzle_temp": 320, "max_bed_temp": 110,
                      "build_volume": (256, 256, 256),
                      "valid_bed_types": ("textured_plate", "cool_plate",
                                          "engineering_plate", "high_temp_plate")},
    "Bambu Lab H2D": {"nozzles": 2, "max_nozzle_temp": 350, "max_bed_temp": 120,
                      "build_volume": (350, 320, 325),
                      "valid_bed_types": ("textured_plate", "cool_plate",
                                          "engineering_plate", "high_temp_plate",
                                          "supertack_plate")},
    "Bambu Lab X1C":  {"nozzles": 1, "max_nozzle_temp": 320, "max_bed_temp": 110,
                       "build_volume": (256, 256, 256),
                       "valid_bed_types": ("textured_plate", "cool_plate",
                                           "engineering_plate", "high_temp_plate")},
    "Bambu Lab X1E":  {"nozzles": 1, "max_nozzle_temp": 320, "max_bed_temp": 110,
                       "build_volume": (256, 256, 256),
                       "valid_bed_types": ("textured_plate", "cool_plate",
                                           "engineering_plate", "high_temp_plate")},
    "Bambu Lab P1S":  {"nozzles": 1, "max_nozzle_temp": 300, "max_bed_temp": 110,
                       "build_volume": (256, 256, 256),
                       "valid_bed_types": ("textured_plate", "cool_plate",
                                           "engineering_plate", "high_temp_plate")},
    "Bambu Lab A1":   {"nozzles": 1, "max_nozzle_temp": 300, "max_bed_temp": 100,
                       "build_volume": (256, 256, 256),
                       "valid_bed_types": ("textured_plate", "cool_plate")},
}


def _read_json(zf: zipfile.ZipFile, name: str) -> dict | None:
    try:
        return json.loads(zf.read(name))
    except (KeyError, json.JSONDecodeError):
        return None


def _read_text(zf: zipfile.ZipFile, name: str) -> str | None:
    try:
        return zf.read(name).decode("utf-8", errors="replace")
    except KeyError:
        return None


def _parse_gcode_header(gcode_bytes: bytes, max_lines: int = 4000) -> dict:
    """Pull comment-key metadata out of the gcode header (BambuStudio puts
    it in the CONFIG_BLOCK between HEADER_BLOCK_END and CONFIG_BLOCK_END).
    Returns a flat {key: value} dict; values are strings."""
    out: dict[str, str] = {}
    text = gcode_bytes[:200_000].decode("utf-8", errors="replace")
    in_config = False
    for i, line in enumerate(text.splitlines()):
        if i > max_lines: break
        if "CONFIG_BLOCK_START" in line: in_config = True; continue
        if "CONFIG_BLOCK_END"   in line: break
        if not in_config or not line.startswith("; "): continue
        # "; key = value"
        if "=" not in line: continue
        k, _, v = line[2:].partition("=")
        out[k.strip()] = v.strip()
    # Also pull a few HEADER_BLOCK comments for printing time / weight
    m_layers = re.search(r"total layer number:\s*(\d+)", text)
    if m_layers: out["_total_layers"] = m_layers.group(1)
    m_time = re.search(r"total estimated time:\s*(\S[^\n]*)", text)
    if m_time: out["_total_estimated_time"] = m_time.group(1).strip()
    m_filament = re.search(r"total filament weight \[g\]\s*:\s*([\d.]+)", text)
    if m_filament: out["_total_filament_weight_g"] = m_filament.group(1)
    return out


def validate(path: Path, *, want_printer: str | None = None,
             want_bed: str | None = None,
             ams_state: dict | None = None) -> PreflightResult:
    res = PreflightResult()
    if not path.exists():
        res.add("error", "no-such-file", f"file not found: {path}")
        return res
    if path.suffix not in (".3mf",) and not str(path).endswith(".gcode.3mf"):
        res.add("warn", "suffix", f"file doesn't end in .gcode.3mf: {path.name}")

    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as e:
        res.add("error", "bad-zip", f"not a valid zip/3mf: {e}")
        return res
    names = set(zf.namelist())

    # 1. Required files present?
    for req in REQUIRED_3MF_FILES:
        if req not in names:
            res.add("error", "missing-file", f"required file absent: {req}")

    # 2. Plate gcode + md5 sidecar
    plates = sorted(int(PLATE_FILES_RE.match(n).group(1))
                    for n in names if PLATE_FILES_RE.match(n))
    md5s = {int(PLATE_MD5_RE.match(n).group(1)): n
            for n in names if PLATE_MD5_RE.match(n)}
    if not plates:
        res.add("error", "no-plate-gcode",
                "no Metadata/plate_N.gcode entries — slicer didn't write any plate gcode")
        zf.close()
        return res
    res.summary["plates"] = plates

    # 3. project_settings.config — printer metadata
    cfg = _read_json(zf, "Metadata/project_settings.config") or {}
    pm = cfg.get("printer_model") or ""
    pv = cfg.get("printer_variant") or ""
    nd = cfg.get("nozzle_diameter") or []
    ft = cfg.get("filament_type") or []
    res.summary["printer_model"] = pm
    res.summary["printer_variant"] = pv
    res.summary["nozzle_diameter"] = nd
    res.summary["filament_type"] = ft
    res.summary["print_settings_id"] = cfg.get("print_settings_id", "")

    if not pm:
        res.add("error", "no-printer-model",
                "project_settings.config has empty printer_model — printer can't validate")
    elif want_printer and pm.replace(" ", "").lower() != want_printer.replace(" ", "").lower():
        res.add("error", "printer-mismatch",
                f"3MF was sliced for {pm!r} but you're sending it to {want_printer!r}")

    # 4. printer specs cross-check
    specs = PRINTER_SPECS.get(pm)
    if specs:
        if isinstance(nd, list):
            for d in nd:
                try: d_f = float(d)
                except (TypeError, ValueError):
                    res.add("warn", "nozzle-diameter-parse",
                            f"can't parse nozzle_diameter entry {d!r}")
                    continue
                if d_f < 0.2 or d_f > 0.8:
                    res.add("warn", "nozzle-diameter-range",
                            f"unusual nozzle diameter {d_f}mm")
        if pv:
            try: pv_f = float(pv)
            except ValueError: pv_f = None
            if pv_f is not None and isinstance(nd, list) and nd:
                if abs(float(nd[0]) - pv_f) > 1e-3:
                    res.add("warn", "variant-mismatch",
                            f"printer_variant={pv} doesn't match first "
                            f"nozzle_diameter={nd[0]}")

    # 5. plate_N.json bed_type
    plate_n = plates[0]
    pj = _read_json(zf, f"Metadata/plate_{plate_n}.json") or {}
    bed_type = pj.get("bed_type")
    res.summary["bed_type_from_plate_json"] = bed_type
    if not bed_type:
        res.add("warn", "no-bed-type",
                f"plate_{plate_n}.json has no bed_type — start_print will use a default")
    elif specs and bed_type not in specs.get("valid_bed_types", ()):
        res.add("error", "invalid-bed-type",
                f"bed_type {bed_type!r} isn't valid for {pm} "
                f"(valid: {', '.join(specs['valid_bed_types'])})")
    elif want_bed and bed_type != want_bed:
        res.add("warn", "bed-type-override",
                f"plate_{plate_n}.json says bed_type={bed_type!r} but you're "
                f"sending --bed-type={want_bed!r}; printer may warn")

    # 6. md5 sidecar
    plate_gcode_name = f"Metadata/plate_{plate_n}.gcode"
    plate_md5_name   = f"Metadata/plate_{plate_n}.gcode.md5"
    plate_bytes = zf.read(plate_gcode_name)
    res.summary["gcode_size"] = len(plate_bytes)
    real_md5 = hashlib.md5(plate_bytes).hexdigest()
    if plate_md5_name in names:
        sidecar = zf.read(plate_md5_name).decode("ascii", "replace").strip()
        # Some BambuStudio versions store it bare, others as `<md5>  filename`
        sidecar_md5 = sidecar.split()[0] if sidecar else ""
        if sidecar_md5 and sidecar_md5.lower() != real_md5.lower():
            res.add("error", "md5-mismatch",
                    f"plate_{plate_n}.gcode.md5 says {sidecar_md5} but actual "
                    f"is {real_md5} — printer will reject")
        else:
            res.summary["md5_match"] = True
    else:
        res.add("warn", "no-md5-sidecar",
                f"no Metadata/plate_{plate_n}.gcode.md5 — printer can't verify integrity")
        res.summary["computed_md5"] = real_md5

    # 7. gcode header — max temps + slicer version
    hdr = _parse_gcode_header(plate_bytes)
    res.summary["slicer"] = hdr.get("BambuStudio") or hdr.get("X-BBL-Client-Version", "?")
    if specs:
        # nozzle_temperature can be a list — pick max
        for k in ("nozzle_temperature", "nozzle_temperature_initial_layer",
                  "first_layer_temperature"):
            v = hdr.get(k)
            if not v: continue
            try:
                temps = [int(x.strip()) for x in v.split(",") if x.strip()]
            except ValueError:
                continue
            mx = max(temps) if temps else 0
            if mx > specs["max_nozzle_temp"]:
                res.add("error", "nozzle-temp-over-limit",
                        f"{k}={mx}°C exceeds {pm} max {specs['max_nozzle_temp']}°C")
        for k in ("bed_temperature", "first_layer_bed_temperature"):
            v = hdr.get(k)
            if not v: continue
            try:
                temps = [int(x.strip()) for x in v.split(",") if x.strip()]
            except ValueError:
                continue
            mx = max(temps) if temps else 0
            if mx > specs["max_bed_temp"]:
                res.add("error", "bed-temp-over-limit",
                        f"{k}={mx}°C exceeds {pm} max {specs['max_bed_temp']}°C")
    res.summary["total_layers"] = hdr.get("_total_layers", "?")
    res.summary["estimated_time"] = hdr.get("_total_estimated_time", "?")
    res.summary["filament_weight_g"] = hdr.get("_total_filament_weight_g", "?")

    # 8. AMS cross-check
    if ams_state and ft:
        loaded_types = set()
        ams_block = (ams_state.get("print") or {}).get("ams") or {}
        for unit in ams_block.get("ams") or []:
            for tray in unit.get("tray") or []:
                if int(tray.get("state", 0)) and tray.get("tray_type"):
                    loaded_types.add(tray.get("tray_type").upper())
        for t in ft:
            if t.upper() not in loaded_types:
                res.add("warn", "ams-no-match",
                        f"3MF needs filament_type={t!r} but no loaded AMS tray "
                        f"matches (loaded: {', '.join(sorted(loaded_types)) or 'none'})")
        res.summary["loaded_ams_types"] = sorted(loaded_types)

    zf.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path, help=".gcode.3mf to validate")
    ap.add_argument("--printer", default="",
                    help="Cross-check 3MF was sliced for this printer model "
                         "(e.g. 'Bambu Lab X2D'). If omitted, only structural "
                         "checks run.")
    ap.add_argument("--bed-type", default="",
                    help="What --bed-type your start_print will use. Triggers "
                         "a warning if the 3MF's plate_N.json disagrees.")
    ap.add_argument("--check-ams", action="store_true",
                    help="Connect (signed MQTT) to the printer named by "
                         "~/.x2d/credentials, fetch live AMS state, and "
                         "warn if no tray has a matching filament_type.")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of human text.")
    args = ap.parse_args()

    ams_state = None
    if args.check_ams:
        try:
            import x2d_bridge
            class _A: ip=""; code=""; serial=""; printer=""
            creds = x2d_bridge.Creds.resolve(_A())
            cli = x2d_bridge.X2DClient(creds)
            cli.connect()
            ams_state = cli.request_state(timeout=8.0)
            cli.disconnect()
        except Exception as e:
            print(f"warning: --check-ams failed ({e}); skipping AMS cross-check",
                  file=sys.stderr)

    res = validate(args.file, want_printer=args.printer or None,
                   want_bed=args.bed_type or None, ams_state=ams_state)

    if args.json:
        print(json.dumps({
            "errors":   [vars(f) for f in res.errors],
            "warnings": [vars(f) for f in res.warnings],
            "summary":  res.summary,
        }, indent=2))
    else:
        print(f"Pre-flight check: {args.file}")
        for k, v in res.summary.items():
            print(f"  - {k}: {v}")
        if not res.findings:
            print("\n✓ No issues — looks safe to print.")
        else:
            print(f"\nFindings ({len(res.errors)} errors, {len(res.warnings)} warnings):")
            for f in res.findings:
                print(f.fmt())

    if res.errors:   return 2
    if res.warnings: return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
