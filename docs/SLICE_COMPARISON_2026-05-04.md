# Slice Output Comparison vs GUI/MakerWorld References

Captured 2026-05-04 while testing CLI slicing path under #97.

## Test inputs

- Model: `rumi_frame.stl` (in repo root)
- Profile: `Bambu Lab X2D 0.4 nozzle` machine + `0.20mm Standard @BBL X2D` process
- Filament: `Bambu PLA Basic @BBL X2D 0.4 nozzle`
- BS version: 02.06.00.51

## CLI invocation

```bash
DISPLAY=:1 ./bambu-studio --slice 0 --debug 2 \
    --load-settings "$PROCESS;$MACHINE" \
    --load-filaments "$FILAMENT" \
    --outputdir "$OUT" \
    --export-3mf "out.gcode.3mf" \
    rumi_frame.stl
```

## Comparison table

| Field             | Our CLI      | Apr-25 GUI ref | MakerWorld (mira_official) |
|-------------------|-------------:|---------------:|---------------------------:|
| `prediction` (s)  | **1503**     | 986            | 912                        |
| `weight` (g)      | **""** (!)   | 6.20           | 3.19                       |
| `used_m` (m)      | 1.83         | 1.95           | 1.05                       |
| `first_layer_time`| 0            | 0              | 212.89                     |
| `tray_info_idx`   | **""** (!)   | GFA05          | GFA00                      |
| `printer_model_id`| **""** (!)   | (empty)        | N6                         |

## Issues identified

1. **52% slower prediction** (1503 vs 986s) — speed/acceleration values
   not propagating from process profile.
2. **Missing weight** — density not bound to filament because
   `tray_info_idx` is empty. The GUI populates this when the user
   picks a tray in the AMS dialog.
3. **Missing printer_model_id** — the model linkage isn't saved
   to the project, so the printer-side software can't validate
   the match.

## Repeated CLI errors

```
update_values_to_printer_extruders, Line 8308: could not found
extruder_type Bowden, nozzle_volume_type Standard,
extruder_index 2, nvt_index 0, nvt_count 1
```

The X2D machine config has `nozzle_diameter: ["0.4", "0.4"]` (dual
extruder) and inherits from `fdm_process_dual_0.20_nozzle_0.4`,
which suggests the slicer expects 4 filament slots (2 per nozzle?).
Passing `--load-filaments "$F;$F"` (two slots) didn't satisfy it.

## Note on `mira_official.gcode.3mf`

Despite the name, this file's `printer_model_id="N6"` matches the
H2D, **not** X2D. Don't use it as an X2D-specific MakerWorld
reference. The Apr-25 `rumi_frame.gcode.3mf` (sliced via our GUI
during a working session) is a better reference for X2D output.

## Recommended next step

Block on #97 — synthesize a proper assemble-list JSON or load a
pre-existing 3mf project so the dual-extruder filament mapping +
tray_info_idx are populated before slicing. Until then, CLI-mode
slicing produces dimensionally correct g-code (777 KB plate_1.gcode
generated successfully) but with broken estimates.
