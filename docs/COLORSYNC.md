# Real-time AMS color sync

Maps a tray's RGB hex color to the closest curated Bambu filament
profile by Euclidean distance in RGB space. Source data:
BambuStudio's official `filaments_color_codes.json` (~7000 entries
covering Bambu's full BBL filament line).

## API

```bash
# Ad-hoc lookup
curl 'http://127.0.0.1:8765/colorsync/match?color=AF7933&material=PLA'
# → {
#     "profile":         "Bambu PLA Metal Copper Brown Metallic @BBL X2D",
#     "fila_id":         "GFA10",
#     "fila_type":       "PLA Metal",
#     "fila_color":      "B07832FF",
#     "fila_color_name": "Copper Brown Metallic",
#     "fila_color_code": "11200",
#     "distance":        26.87
#   }

# Per-printer state — every AMS slot resolved
curl http://127.0.0.1:8765/colorsync/state
# → {
#     "printers": {
#       "studio": [
#         {"slot": 1, "ams_id": "0", "color": "FF6A13FF", "material": "PLA",
#          "match": {"profile": "Bambu PLA Basic Orange @BBL X2D", "distance": 0, ...}},
#         {"slot": 2, ...},
#         ...
#       ]
#     }
#   }
```

## Material filtering

The `material` query param substring-matches the catalog entry's
`fila_type`. Examples:

| Query              | Filters to |
|--------------------|------------|
| `material=PLA`     | every PLA family entry (Basic, Silk, Metal, Translucent, …) |
| `material=PLA Basic` | only PLA Basic entries (~50) |
| `material=PETG-HF` | PETG-HF only |
| empty              | whole catalog |

If no entries match, the matcher falls back to the whole catalog so
the call always returns *some* profile (with a higher distance) —
better than returning None for an exotic material the X2D pushall
might emit.

## Web UI

The AMS card's swatches now render the resolved colour name as a
caption inside the swatch + the full profile name + RGB distance
as a tooltip. Updates land within ~3 s of an MQTT state push (the
SSE pipeline is at that cadence).

## Profile-name shape

`{profile}` is built from the catalog entry as:

```
"Bambu" + fila_type + fila_color_name (English) + " @BBL " + model
```

So for X2D:

* `FF6A13FF` PLA Basic → `Bambu PLA Basic Orange @BBL X2D`
* `FFFFFFFF` PLA Silk  → `Bambu PLA Silk White @BBL X2D`
* `B07832FF` PLA Metal → `Bambu PLA Metal Copper Brown Metallic @BBL X2D`

Override the model via `mapper.match(color, material, model="P1S")`
or the `device_model` arg on the HA publisher.

## Test harness

```bash
PYTHONPATH=. python3.12 runtime/colorsync/test_mapper.py  # 30/30 PASS
```

Covers exact match (distance 0), near-color match, alpha-byte stripping,
material filter narrowing PLA Silk vs PLA Basic, empty-material
fallback to whole catalog, invalid input, `state_for()` walking all
4 AMS slots including empty bays, HTTP route round-trip.
