# Multi-extruder & on-the-fly model remix (X2D, items #82–#84)

The X2D ships with two nozzles ("left" / "right" or "main" / "auxiliary").
BambuStudio's stock UI exposes the right nozzle primarily through the
support-material preset slots (`support_filament`,
`support_interface_filament`), which is enough for soluble-support
workflows but obscures the fact that the second nozzle is a fully
independent primary extruder.

This repo adds two routes to use the auxiliary as a primary extruder
and to rewrite per-object settings without re-importing the model:

  1. **In the GUI** — BambuStudio's existing right-click ➜ "Change
     Extruder" works on the X2D too. Select the object in the Object
     List, right-click ➜ "Change Extruder" ➜ "Filament 2" (or whichever
     ordinal corresponds to the right nozzle in your loaded AMS layout).
     The slicer treats the right nozzle as a primary for all roles
     (walls, infill, top/bottom shells), not just support.

  2. **From the CLI** — `remix_3mf.py` rewrites a `.gcode.3mf` in place
     (or to a new file via `--out`) so you can do batch / scripted
     remixes without launching BambuStudio.

Both routes round-trip cleanly through the 3MF: per-object overrides
land in `Metadata/model_settings.config` as
`<metadata key="..." value="..."/>` entries on the matching
`<object id="...">` block, and BambuStudio reads them back as
per-object overrides on next open / re-slice.

## CLI quick reference

### Inspect — what's currently set?

```sh
$ python3 remix_3mf.py rumi_frame.gcode.3mf --inspect
object id=2 name='rumi_frame.stl'
  extruder = '1'
  part id=1 name='rumi_frame.stl'

1 object(s) total
```

### #82 — Send everything to the AUX (right) nozzle as a primary

```sh
$ python3 remix_3mf.py rumi_frame.gcode.3mf --extruder 2
applied 1 override(s)
wrote rumi_frame.gcode.3mf
```

Per-object split (object id 2 ➜ right nozzle, others ➜ left):

```sh
$ python3 remix_3mf.py multi.gcode.3mf --extruder 1 --object 2:2
```

The `extruder` key is 1-indexed (1 = left, 2 = right). The slicer
respects this for *all* roles — walls, infill, top/bottom shells,
not just support.

### #83 — Rewrite shells / infill / layer height without re-importing

```sh
$ python3 remix_3mf.py rumi_frame.gcode.3mf \
        --wall-loops 4 \
        --sparse-infill 25 \
        --sparse-infill-pattern gyroid \
        --layer-height 0.16 \
        --top-shells 5 \
        --bottom-shells 4
applied 6 override(s)
```

Re-scale objects (uniform or per-axis):

```sh
$ python3 remix_3mf.py model.3mf --scale 1.10            # 110% all axes
$ python3 remix_3mf.py model.3mf --scale 1.0,1.0,1.10    # 110% on Z only
```

After rewriting, re-open in BambuStudio and re-slice (Ctrl+R), or run
the headless slice:

```sh
$ bambu-studio --slice rumi_frame.gcode.3mf
```

The reslice picks up the per-object overrides; the global preset stays
unchanged (so you can A/B compare without touching profile state).

### #84 — Persistence guarantees

Per-object overrides survive every write/re-read cycle. Verified by the
4-case round-trip suite at `tests/test_remix_3mf.py`:

  * `#82`  single-object extruder assignment writes a single
    `<metadata key="extruder" value="2"/>` entry that --inspect can read
    back unchanged.
  * `#83`  4 simultaneous overrides (wall_loops / sparse_infill_density
    / layer_height / top_shell_layers) all serialise + round-trip.
  * `#84`  Two consecutive `remix_3mf.py` invocations on the same file
    preserve every prior override and append the new one.
  * `#84`  `--reset --extruder 1` runs the reset first then sets the
    fresh extruder — combinable in a single invocation.

If you re-open the remixed 3MF in BambuStudio you'll see the per-object
overrides appear in the Object List's per-object settings panel, and a
re-slice will produce gcode that matches the new parameters.

## Reset to the global preset

```sh
$ python3 remix_3mf.py model.3mf --reset                 # wipe all overrides
$ python3 remix_3mf.py model.3mf --reset --extruder 1    # wipe then set fresh
```

## Known limitations

  * `remix_3mf.py` does not regenerate `Metadata/plate_<N>.gcode`. After
    rewriting overrides you need a re-slice (in BambuStudio or
    `bambu-studio --slice`) to materialise the new gcode. For
    extruder-only changes, the X2D firmware re-routes the filament source
    via `ams_mapping2` at print-start, so a re-slice may not be strictly
    needed — but a fresh slice is the safer default for multi-color or
    multi-extruder primary changes.
  * `--scale` pre-multiplies each part's affine matrix from the world
    origin. For centred scaling, run `Object ➜ Center` in BambuStudio
    after re-importing.
  * The CLI deliberately limits itself to the `KNOWN_OBJECT_OVERRIDES`
    whitelist. To set a key the slicer accepts but the CLI doesn't
    expose, edit `Metadata/model_settings.config` by hand (it's an
    XML file inside the .gcode.3mf zip).

## See also

  * `preflight_3mf.py` — pre-print validator (printer-model match,
    bed_type sanity, MD5 sidecar, max-temp guards). Run after
    remixing to catch any combinations the printer would reject.
  * `lan_print.py --filament-color` / `--filament-info-idx` — match
    AMS slot to the chosen extruder/filament when sending the print.
