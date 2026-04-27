"""Closest-color filament-profile matcher (item #58).

Loads BambuStudio's `filaments_color_codes.json` (7000+ entries) once
at import and exposes `match(color_hex, material=None)` that returns
the closest Bambu profile by Euclidean distance in RGB space.

The catalog entries shape:

    {
        "fila_color_code": "10300",
        "fila_id":         "GFA00",
        "fila_color_type": "单色",                # solid / dual-color / etc.
        "fila_type":       "PLA Basic",
        "fila_color_name": {"en": "Orange", ...},
        "fila_color":      ["#FF6A13FF"],         # 1+ hex strings (8-char)
    }

Wire surface: `GET /colorsync/match?color=AF7933&material=PLA` and
`GET /colorsync/state` on the bridge daemon. Web UI displays the
matched filament name under each AMS swatch and updates within ~3 s
of a state push.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("x2d.colorsync")

_CATALOG_PATH = Path(__file__).parent / "data" / "filaments_color_codes.json"


@dataclass
class FilamentMatch:
    profile:        str            # "Bambu PLA Basic Orange @BBL X2D"
    fila_id:        str            # "GFA00"
    fila_type:      str            # "PLA Basic"
    fila_color:     str            # 8-char hex of the catalog entry
    fila_color_name: str           # "Orange" (en localisation)
    fila_color_code: str           # "10300"
    distance:       float          # 0 = exact, ~441 = max possible (255√3)


_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6,8})$")


def _hex_to_rgb(s: str) -> tuple[int, int, int] | None:
    m = _HEX_RE.match(s.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 8:
        h = h[:6]            # drop alpha
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _normalise_material(s: str | None) -> str:
    """User-facing material strings come in many shapes — 'PLA',
    'PLA Basic', 'PLA-Basic', 'pla'. Reduce to a casefold token plus
    a list of tags so 'PLA Basic' matches 'PLA Silk' both via 'PLA'."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


@dataclass(frozen=True)
class _CatalogEntry:
    fila_id:         str
    fila_type:       str
    fila_color_code: str
    fila_color_name: str
    fila_color:      str        # 8-char hex
    rgb:             tuple[int, int, int]


@lru_cache(maxsize=1)
def _load_catalog() -> list[_CatalogEntry]:
    if not _CATALOG_PATH.exists():
        LOG.warning("colorsync catalog missing at %s", _CATALOG_PATH)
        return []
    raw = json.loads(_CATALOG_PATH.read_text())
    out: list[_CatalogEntry] = []
    for ent in raw.get("data", []):
        for color_hex in ent.get("fila_color", []) or []:
            rgb = _hex_to_rgb(color_hex)
            if rgb is None:
                continue
            out.append(_CatalogEntry(
                fila_id=ent.get("fila_id", ""),
                fila_type=ent.get("fila_type", ""),
                fila_color_code=str(ent.get("fila_color_code", "")),
                fila_color_name=(ent.get("fila_color_name") or {})
                                  .get("en", ""),
                fila_color=color_hex.lstrip("#").upper(),
                rgb=rgb,
            ))
    LOG.info("loaded %d catalog entries from %s", len(out), _CATALOG_PATH)
    return out


def _profile_name(ent: _CatalogEntry, *,
                   model: str = "X2D") -> str:
    """Construct the Bambu-canonical profile slug — same shape the
    BambuStudio filament dropdown uses, e.g.
    'Bambu PLA Basic Orange @BBL X2D'."""
    bits = ["Bambu", ent.fila_type or "PLA",
             ent.fila_color_name or ""]
    name = " ".join(b for b in bits if b)
    return f"{name} @BBL {model}".strip()


def _matches_material(ent: _CatalogEntry, want: str) -> bool:
    """Loose substring match. 'pla' matches both PLA Basic and PLA
    Silk; 'plabasic' constrains to PLA Basic only."""
    if not want:
        return True
    want = _normalise_material(want)
    have = _normalise_material(ent.fila_type)
    return want in have


def match(color_hex: str,
          material: Optional[str] = None,
          *, model: str = "X2D") -> FilamentMatch | None:
    """Return the closest catalog entry by RGB Euclidean distance.
    Returns None on parse failure or empty catalog. Material filter
    is best-effort: if no entries match the material, falls back to
    the whole catalog."""
    rgb = _hex_to_rgb(color_hex)
    if rgb is None:
        return None
    catalog = _load_catalog()
    if not catalog:
        return None
    candidates = [e for e in catalog if _matches_material(e, material or "")]
    if not candidates:
        candidates = catalog
    best = min(candidates,
               key=lambda e: ((e.rgb[0] - rgb[0]) ** 2 +
                              (e.rgb[1] - rgb[1]) ** 2 +
                              (e.rgb[2] - rgb[2]) ** 2))
    dist = math.sqrt((best.rgb[0] - rgb[0]) ** 2 +
                     (best.rgb[1] - rgb[1]) ** 2 +
                     (best.rgb[2] - rgb[2]) ** 2)
    return FilamentMatch(
        profile=_profile_name(best, model=model),
        fila_id=best.fila_id,
        fila_type=best.fila_type,
        fila_color=best.fila_color,
        fila_color_name=best.fila_color_name,
        fila_color_code=best.fila_color_code,
        distance=round(dist, 2),
    )


def state_for(state: dict | None, *, model: str = "X2D") -> list[dict]:
    """Walk a printer state's AMS slots and return a list of
    `{slot, color, material, match}` dicts.  Empty bays are
    represented with `match=None`."""
    if not state:
        return []
    p = state.get("print", {})
    ams_list = (p.get("ams", {}) or {}).get("ams") or []
    out: list[dict] = []
    for ams in ams_list:
        ams_id = ams.get("id", "0")
        for idx, tray in enumerate(ams.get("tray") or []):
            slot_idx = (int(ams_id) * 4) + idx + 1   # 1-indexed
            color = (tray or {}).get("tray_color", "")
            material = (tray or {}).get("tray_type", "")
            if not color:
                out.append({
                    "slot":     slot_idx,
                    "ams_id":   ams_id,
                    "color":    "",
                    "material": "",
                    "match":    None,
                })
                continue
            m = match(color, material=material, model=model)
            out.append({
                "slot":     slot_idx,
                "ams_id":   ams_id,
                "color":    color,
                "material": material,
                "match":    asdict(m) if m else None,
            })
    return out
