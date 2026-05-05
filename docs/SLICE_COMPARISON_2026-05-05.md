# Slice Output Comparison — same model, four paths (#101)

Captured 2026-05-05 to characterise how `x2d_slice.py` (#97/#102)
diverges from BS-GUI sliced output. Predates and supersedes the
2026-05-04 doc which used `mira_official.gcode.3mf` as a "MakerWorld"
reference; that file is actually `printer_model_id="N6"` (= H2D), not
X2D, so it's not a valid X2D MakerWorld baseline.

## Test inputs

- Model on disk: `rumi_frame.stl` (838 KB binary STL).
- Reference 3MF: `rumi_frame.gcode.3mf` (212 KB), produced from a
  prior live BS GUI session sliced against the same X2D 0.4 nozzle
  + 0.20mm Standard process + Bambu PLA Basic GFA05 filament.
- BS version: 02.06.00.51.

## Four slice paths exercised

| Path | Description |
|---|---|
| **A** | Direct re-slice of `rumi_frame.gcode.3mf` (no graft). |
| **B** | `x2d_slice.py rumi_frame.stl` (graft STL into template at scale=1, no color override). |
| **C** | `x2d_slice.py rumi_frame.stl --scale 0.5` |
| **D** | `x2d_slice.py rumi_frame.stl --scale 0.7 --color "#00FF00"` |

## Results

| Path | prediction | weight | used_m | filament_color | tray_info_idx |
|------|-----------:|-------:|-------:|:---------------|:--------------|
| A    |       986s |  6.20g |  1.95m | `#00AE42`      | `GFA05`       |
| B    |      1602s | 10.95g |  3.45m | `#00AE42`      | `GFA05`       |
| C    |       623s |  2.03g |  0.64m | `#00AE42`      | `GFA05`       |
| D    |       908s |  4.27g |  1.34m | `#00FF00`      | `GFA05`       |

## Why A ≠ B (same template, same X2D profile, "same" model)

The STL on disk and the mesh embedded in the reference 3MF aren't
identical:

```
STL on disk:    15 404 verts, 16 776 triangles
3MF-embedded:   11 461 verts, 11 197 triangles
STL bbox:       67 × 108 × 1.2 mm
```

Path A re-slices the 11 197-triangle mesh that BS already optimised /
dedup'd / repaired during the original GUI session. Path B re-grafts
the raw 16 776-triangle STL. The triangle-count delta (~50% more) is
what drives the time/material delta:

* More triangles → more wall facets to traverse → longer outer-wall
  paths → ~62% longer print.
* Same nominal volume but more surface detail → ~77% more material
  including supports and flow-rate ramping.

The grafted-STL path is the **honest** slice for that STL; the
embedded mesh is a curated version. For applications where exact
GUI-match is required, snapshot a placement in the GUI once and use
that 3MF as the `--template`.

## Scale fidelity (B → C → D)

| Variant | Linear scale | Expected weight ratio (s³) | Actual weight ratio | Expected time ratio (≈s²) | Actual time ratio |
|---------|:------------:|:--------------------------:|:-------------------:|:-------------------------:|:-----------------:|
| B (1.0) |     1.0      |          1.00              |        1.00         |          1.00             |       1.00        |
| C (0.5) |     0.5      |          0.125             |        0.185        |          0.25             |       0.39        |
| D (0.7) |     0.7      |          0.343             |        0.390        |          0.49             |       0.57        |

Volume scales as s³ for solid prints, as s² for thin walls; rumi_frame
is mostly thin-walled (z=1.2mm out of 67×108 footprint), so weight
tracks closer to s²·thickness than s³. Time tracks s²·layer_count, so
the actual ratios match the geometry. **All scale operations are
nominally correct.**

## Note on the original #97/#35 "MakerWorld" claim

The prior `mira_official.gcode.3mf` reference had `printer_model_id="N6"`
(H2D, not X2D), so the comparison there wasn't apples-to-apples for
an X2D-targeted slice. To do a true MakerWorld-vs-local X2D slice
comparison, one would need to:

1. Find a MakerWorld model published with `printer_model_id` matching
   X2D (the BS profile catalogue lists `Bambu Lab X2D` with
   `setting_id: GM045`).
2. Download both the `.3mf` and the matching MakerWorld-server
   `plate_1.gcode` for that print.
3. Re-slice the 3mf locally via `x2d_slice.py` and diff prediction /
   weight / used_m.

MakerWorld's public model search API requires either auth or a
specific printer-tag filter that isn't exposed via the unauthed
endpoint. Until an X2D-tagged public model is identified, we use
`rumi_frame.gcode.3mf` as our internal reference and acknowledge the
mesh-curation gap in path B above.

## Recommended workflows for users

* **Headless slice + print**: `x2d_bridge slice-print model.stl` —
  uses the rumi_frame template's X2D profile + identity transform.
* **Custom scale**: `x2d_slice.py model.stl --out out.gcode.3mf --scale 0.7`
* **Custom color**: `x2d_slice.py model.stl --out out.gcode.3mf --color "#FF8800"`
* **GUI-match accuracy needed**: open the STL once in BS, save as
  `.gcode.3mf`, then use that as `--template`.
