# Bambu → Craftmatic TS port — comprehensive plan

End-to-end TypeScript port of the BambuStudio Prepare → Preview pipeline,
targeted at the X2D dual-nozzle printer with full AMS support, hosted in
the existing `craftmatic` web app (`../craftmatic`). Replaces the
termux-x11 BambuStudio GUI for everything except slicing-engine work
(server-side initially, optional WASM later).

  * Import STL / 3MF
  * Interactive build plate (3D viewport, gizmos, multi-object, plates)
  * Per-object color + extruder assignment (#82 — aux as primary)
  * Per-object remix overrides (#83 — resize, shells, infill, layer
    height, extruder, infill-extruder split)
  * Slice → 3MF + gcode (server-side `bambu-studio --slice` initially)
  * Pre-flight validate (`preflight_3mf.py` reused)
  * Sign + send via MQTT (`x2d_bridge.py` reused via REST)
  * Live monitor: state-stream + RTSPS camera + AMS state + temps
  * Cross-cutting: **2nd extruder ALSO usable for infill**, not just
    walls / supports / per-part

## Tech stack already in `craftmatic`

  - `three` — 3D rendering
  - `three-mesh-bvh` — fast raycasts for object selection / picking
  - `manifold-3d` — CSG (boolean ops) for cut planes, model repair
  - `pako` — gzip for 3MF compressed streams
  - `prismarine-nbt` — irrelevant here (Minecraft NBT) but shows the
    project's appetite for binary parsers
  - `geotiff` / `pmtiles` — irrelevant
  - `express`, `commander` — server + CLI scaffolding ready
  - TypeScript + Vitest — typed + tested

We DON'T need to add: Three.js, BVH, manifold, gzip — already there.

We WILL need: `xmldom` (3MF XML), `paho-mqtt` (cloud + LAN MQTT,
JS port), `node-forge` or WebCrypto (RSA-SHA256 for signed publishes),
`yaml` (Bambu profile parsing), `vite` or similar for the SPA.

## Architecture

```
┌──────────────────── craftmatic web app (browser) ────────────────────┐
│                                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐  ┌────────────┐  │
│  │ STL/3MF     │  │ 3D viewport  │  │ Per-object  │  │ AMS palette│  │
│  │ importer    │→ │ (Three.js +  │← │ remix panel │← │ + extruder │  │
│  │             │  │  BVH + CSG)  │  │             │  │  selector  │  │
│  └─────────────┘  └──────────────┘  └─────────────┘  └────────────┘  │
│         │                  │                │                │       │
│         ▼                  ▼                ▼                ▼       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  In-memory project state (one-source-of-truth)                 │  │
│  │  { plates: [{ objects: [{ mesh, transform, overrides }] }] }   │  │
│  └────────────────────────────────────────────────────────────────┘  │
│         │                  │                │                │       │
│         ▼ (slice)          ▼ (preview)      ▼ (send)         ▼ (live)│
│  ┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌─────────────────┐  │
│  │ POST /   │  │ GCode parser │  │ POST /   │  │ WS subscribe    │  │
│  │ slice    │  │ (Three.js    │  │ print    │  │ printer state + │  │
│  │          │  │  LineSeg)    │  │          │  │ /cam.jpg poll    │  │
│  └─────┬────┘  └──────────────┘  └────┬─────┘  └────────┬────────┘  │
│        │                              │                  │           │
└────────┼──────────────────────────────┼──────────────────┼───────────┘
         │                              │                  │
         ▼                              ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  craftmatic-server  (Node/Express on the same Termux box)           │
│                                                                      │
│  POST /slice  ──spawn──▶ bambu-studio --slice <project.3mf>          │
│                          ──▶ returns sliced .gcode.3mf + plate.png   │
│                                                                      │
│  POST /print  ──REST──▶ x2d_bridge.py serve  (Unix socket)           │
│                          ──▶ signed MQTT publish to printer          │
│                                                                      │
│  WS  /state   ──tail──▶ x2d_bridge.py serve  state_push subscriber   │
│                          ──▶ pushes deltas to browser                │
│                                                                      │
│  GET /cam.jpg ──proxy─▶ x2d_bridge.py camera --bind 127.0.0.1:8767   │
│                          ──▶ decoded JPEG of latest RTSPS frame      │
└─────────────────────────────────────────────────────────────────────┘
```

The server is a thin shim — every primitive (`slice`, `print`, `state`,
`cam.jpg`) wraps an existing Python tool we've already built and tested.
So Phase 0 of the port is **near-zero risk**: we know each piece works.

## Phase 0 — Server scaffold (3-5 days)

`craftmatic-server/` gets a small Express app:

```ts
// craftmatic-server/src/routes/slice.ts
import express from "express";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import path from "node:path";

export const sliceRouter = express.Router();
sliceRouter.post("/slice", async (req, res) => {
  const dir = await mkdtemp("/tmp/cm-slice-");
  const inp = path.join(dir, "project.3mf");
  await writeFile(inp, req.body); // upload arrives as raw bytes
  const child = spawn("bambu-studio", ["--slice", "--load", inp,
                                        "--export-3mf", path.join(dir, "out.3mf")]);
  child.on("exit", async (code) => {
    if (code !== 0) { res.status(500).end("slice failed"); return; }
    const out = await readFile(path.join(dir, "out.3mf"));
    res.type("model/3mf").send(out);
  });
});
```

Same shape for `/print` (POSTs the .gcode.3mf bytes — server uploads via
`x2d_bridge.py print`), `/state` (Server-Sent Events tailing
`x2d_bridge.py serve`'s state push), `/cam.jpg` (HTTP proxy to the
existing camera daemon at `127.0.0.1:8767/cam.jpg`).

Deliverable: `curl http://localhost:3000/slice -X POST --data-binary
@model.3mf -o sliced.3mf` works end-to-end against the live X2D.

## Phase 1 — STL / 3MF importer (3-5 days)

3MF is just a zip of XML + binary parts. Three.js has `STLLoader` and
the community has `3MFLoader` (already part of three/examples). The
twist: BambuStudio's 3MF includes per-object metadata in
`Metadata/model_settings.config` (the file `remix_3mf.py` rewrites) —
loader needs to expose that as a TS interface.

```ts
// craftmatic/src/io/3mf.ts
import * as JSZip from "jszip";
import { Mesh, BufferGeometry } from "three";
import { ThreeMFLoader } from "three/examples/jsm/loaders/3MFLoader";

export interface ObjectOverrides {
  extruder?: number;            // 1=left, 2=right
  wall_loops?: number;
  sparse_infill_density?: number;
  sparse_infill_pattern?: string;
  sparse_infill_filament?: number; // ← #82 ask: lets infill go to extruder 2
  layer_height?: number;
  top_shell_layers?: number;
  bottom_shell_layers?: number;
}

export interface LoadedObject {
  id: number;
  name: string;
  mesh: Mesh;
  partExtruders: Record<number, number>;  // partId → extruder
  overrides: ObjectOverrides;
}

export interface LoadedProject {
  objects: LoadedObject[];
  plates: Plate[];
  globalProfile: PrintConfig;
}

export async function loadProject(buf: ArrayBuffer): Promise<LoadedProject> {
  const zip = await JSZip.loadAsync(buf);
  const meshes = await new ThreeMFLoader().parseAsync(buf);  // builds the Group
  const config = await zip.file("Metadata/model_settings.config")!.async("string");
  // Parse the model_settings.config XML — same format remix_3mf.py writes.
  // Return everything as a typed project the UI binds to.
  ...
}
```

STL loader is even simpler — `STLLoader` from three.js. Wrap each STL
into a single-object project with default overrides.

Tests: load every `*.gcode.3mf` and `*.stl` in the repo's root
(rumi_frame, mira_frame, zoey_frame, etc) — assert object count, name,
default extruder. ~10 cases.

## Phase 2 — 3D viewport + build plate (1-2 weeks)

Build plate = textured `PlaneGeometry` 256×256 (X2D build volume) with
grid lines via `GridHelper`. Background = dark mode default per
project preferences.

```ts
// craftmatic/src/viewport/scene.ts
import { Scene, PerspectiveCamera, WebGLRenderer, GridHelper, Mesh,
         PlaneGeometry, MeshStandardMaterial, AmbientLight, DirectionalLight } from "three";

export class BuildPlateScene {
  scene = new Scene();
  camera: PerspectiveCamera;
  renderer: WebGLRenderer;
  // Bambu X2D build volume: 256 x 256 x 256 (mm)
  static BUILD_VOLUME = { x: 256, y: 256, z: 256 };

  constructor(canvas: HTMLCanvasElement) {
    this.renderer = new WebGLRenderer({ canvas, antialias: true });
    this.camera = new PerspectiveCamera(50, canvas.width / canvas.height, 1, 5000);
    this.camera.position.set(400, 400, 400);
    this.camera.lookAt(0, 0, 0);
    const plate = new Mesh(
      new PlaneGeometry(BuildPlateScene.BUILD_VOLUME.x, BuildPlateScene.BUILD_VOLUME.y),
      new MeshStandardMaterial({ color: 0x202020, roughness: 0.85 }),
    );
    plate.rotation.x = -Math.PI / 2;
    this.scene.add(plate);
    this.scene.add(new GridHelper(BuildPlateScene.BUILD_VOLUME.x, 16, 0x444444, 0x222222));
    this.scene.add(new AmbientLight(0xffffff, 0.4));
    const sun = new DirectionalLight(0xffffff, 0.8);
    sun.position.set(200, 300, 200);
    this.scene.add(sun);
  }
  ...
}
```

Camera = OrbitControls (pan/zoom/rotate). Picking = `three-mesh-bvh`'s
accelerated raycast on every loaded mesh's BVH. Object outlines on
hover/select via `OutlinePass` from `EffectComposer`.

**Bed type visualisation**: cool_plate / textured_plate / etc map to
different plate textures (load from `craftmatic/static/plates/*.png`).
Default from the 3MF's `plate_<N>.json` `bed_type` field.

## Phase 3 — Object manipulation + multi-plate (1 week)

Each `LoadedObject` becomes a `THREE.Group` parented to the active
plate. `TransformControls` for translate/rotate/scale gizmos. Per-axis
scale stays connected to the per-object override (`#83` — applied via
`remix_3mf.py`-style metadata when we serialise back to 3MF).

Multi-plate: BambuStudio supports up to 16 plates per project. Each
plate is its own `THREE.Group` swapped via plate selector tabs at the
top of the viewport.

Snap-to-bed (Z=0 origin), align-XY-center, auto-arrange (bin-packing
in 2D footprint — use `binpackingjs` or implement simple shelf-pack —
~200 LOC).

## Phase 4 — AMS palette + extruder assignment UI (1 week)

The X2D has 2 nozzles + (typically) 2 AMS units = 8 slot positions.
Live state from the `/state` SSE stream populates the palette:

```ts
interface AMSSlot {
  amsId: number;        // 0, 1, ...
  trayId: number;       // 0..3
  globalSlot: number;   // amsId * 4 + trayId
  filamentType: string; // "PLA", "PETG", ...
  color: string;        // "#057748FF"
  infoIdx: string;      // "GFL95" — Bambu catalog ID
  extruderAffinity: 1 | 2 | "auto";   // user can pin a slot to L or R
}
```

The palette is a row of 8 swatches at the bottom of the viewport. Click
an object → click a swatch → assigns that filament. Click a swatch +
hold Shift → scope to one extruder for ALL roles routed through that
slot.

Per-object panel (right side of viewport) shows:

```
Object: rumi_frame.stl  [hide] [delete]
  Extruder (walls):     [Left | Right | Auto]   ← writes wall_filament
  Extruder (infill):    [Left | Right | Auto]   ← writes sparse_infill_filament  ❤
  Extruder (support):   [Left | Right | Auto]   ← writes support_filament
  Color:                [swatch picker]
  Per-part overrides:   [+] add part-level rule
```

The infill row is the **#82-extended** ask: the second extruder is now
explicitly available for infill, not just support. The slicer engine
(BambuStudio / Slic3r) honours `sparse_infill_filament = 2` per-object
or per-region. Concretely — when we serialise back to 3MF we write:

```xml
<object id="2">
  <metadata key="extruder"               value="1"/>  <!-- wall extruder = left -->
  <metadata key="sparse_infill_filament" value="2"/>  <!-- INFILL = right -->
  <metadata key="support_filament"       value="1"/>  <!-- support = left -->
</object>
```

This requires extending `remix_3mf.py`'s `KNOWN_OBJECT_OVERRIDES`
whitelist (already includes `sparse_infill_filament`,
`support_filament` — see line ~75 of that file).

## Phase 5 — Per-object remix panel (3-5 days)

Wraps the existing `remix_3mf.py` schema in a TS form:

```tsx
function RemixPanel({ obj, onApply }: Props) {
  const [overrides, set] = useState<ObjectOverrides>(obj.overrides);
  return (
    <Panel title={`Remix: ${obj.name}`}>
      <Field label="Wall loops">
        <NumberInput value={overrides.wall_loops ?? "auto"}
                     onChange={v => set({...overrides, wall_loops: v})}/>
      </Field>
      <Field label="Sparse infill density">
        <NumberInput unit="%" min={0} max={100}
                     value={overrides.sparse_infill_density ?? "auto"}
                     onChange={v => set({...overrides, sparse_infill_density: v})}/>
      </Field>
      <Field label="Sparse infill pattern">
        <Select value={overrides.sparse_infill_pattern ?? "auto"}
                options={["grid","gyroid","cubic","honeycomb","monotonic","adaptive_cubic"]}
                onChange={v => set({...overrides, sparse_infill_pattern: v})}/>
      </Field>
      <Field label="Layer height">
        <NumberInput unit="mm" step={0.04} value={overrides.layer_height ?? "auto"}
                     onChange={v => set({...overrides, layer_height: v})}/>
      </Field>
      <Field label="Walls extruder">
        <ExtruderToggle value={overrides.extruder ?? 1}
                        onChange={v => set({...overrides, extruder: v})}/>
      </Field>
      <Field label="Infill extruder">    {/* ← #82 ask */}
        <ExtruderToggle value={overrides.sparse_infill_filament ?? overrides.extruder ?? 1}
                        onChange={v => set({...overrides, sparse_infill_filament: v})}/>
      </Field>
      <Button onClick={() => onApply(overrides)}>Apply &amp; re-slice</Button>
    </Panel>
  );
}
```

Apply triggers `POST /slice` with the project blob + the new overrides
merged into `model_settings.config`. The server runs
`bambu-studio --slice` with the modified 3MF and returns the new
sliced 3MF (with fresh `plate_<N>.gcode`) for the Preview tab.

## Phase 6 — Slicing (server-side initially, WASM later) (1-2 weeks server / 3-6 months WASM)

**Server-side path** (recommended Phase 6 deliverable):

```ts
// In the server route /slice handler
const child = spawn("bambu-studio", [
  "--slice", "0",                       // slice plate index (0 = first/all)
  "--load", input3MF,
  "--export-3mf", output3MF,
  "--load-settings", "preset.json",     // global profile (X2D 0.4)
  "--load-filaments", "filament.json",  // AMS slot mapping
]);
```

The repo already has `slice.sh` / `BambuStudio.AppImage` / the in-tree
`bambu-studio` binary. We're already running this for development. The
server route just spawns the CLI and streams the result.

**WASM path** (Phase 6 stretch — defer to v2):

Emscripten port of the slicer engine. Steep but tractable: CuraEngine
has a working WASM build (~30 MB binary, slices small models in
browser); the BambuStudio engine is structurally similar but with more
deps (TBB, OCCT, OpenVDB, CGAL). Realistic effort: 3-6 months for one
dev who knows Emscripten well.

For X2D-specific features (AMS multi-color, dual-nozzle, calibration
knobs) the WASM port needs the proprietary `libbambu_networking.so`
stubbed out (we already have a stub in `runtime/network_shim/`) and
the BBL profiles + system filaments compiled in (~10 MB JSON).

## Phase 7 — GCode preview (1-2 weeks)

Parse `Metadata/plate_<N>.gcode` line-by-line into per-layer
`THREE.LineSegments`. Color by extruder index, feature type
(perimeter / infill / support), or by speed.

```ts
// craftmatic/src/preview/gcodeParser.ts
export interface GCodeMove {
  x: number; y: number; z: number; e: number;
  layer: number;
  extruder: number;     // 0/1
  feature: GCodeFeature;
  speed: number;        // mm/s
}

export interface ParsedGCode {
  layers: GCodeMove[][];   // moves grouped by Z layer
  totalTime: number;       // seconds
  totalFilament: Record<number, number>;  // mm per extruder
}

export function parseGCode(text: string): ParsedGCode { ... }
```

Layer scrubber slider on top of the viewport. Toggle visibility of
walls/infill/support/travel via checkboxes. "Time travel" — show
all moves up to time T, simulating the print head's path.

Bambu's gcode comments (`;FEATURE:Outer wall`, `;LAYER_HEIGHT:0.2`,
etc.) make this trivial — we just walk the gcode and bucket.

## Phase 8 — Send to printer (1 week)

`POST /print` server route wraps the existing `x2d_bridge.py print`
flow. Server already speaks the bridge socket (per
`runtime/network_shim/PROTOCOL.md`):

```ts
// craftmatic-server/src/routes/print.ts
sendRouter.post("/print", async (req, res) => {
  const body = req.body as PrintRequest;
  // body.gcode3mf is base64 of the sliced 3MF
  // body.amsSlots is the per-filament-index slot list
  const sock = net.createConnection(process.env.HOME + "/.x2d/bridge.sock");
  const reqLine = JSON.stringify({
    op: "start_local_print",
    args: {
      gcode_filename: body.filename,
      ams_slot: body.amsSlots,
      bed_type: body.bedType,
      flow_cali: body.flowCali,
    },
  });
  sock.write(reqLine + "\n");
  sock.on("data", buf => res.send(buf));  // forward bridge response
});
```

Browser-side: confirm dialog with the sliced gcode preview, AMS
mapping diagram, estimated time/filament, then "Start Print" sends the
POST.

## Phase 9 — Live monitor (1-2 weeks)

Two streams from the printer (both already exist via `x2d_bridge.py
serve`):

  1. **State push** — printer publishes its full state JSON every ~1 s
     to `device/<SN>/report`. The server tails this via
     `x2d_bridge.py`'s in-memory cache and forwards to the browser via
     SSE or WebSocket. Browser updates progress bar, layer counter,
     temps, AMS state.

  2. **Camera feed** — `x2d_bridge.py camera --port 322 --bind
     127.0.0.1:8767` decodes the X2D's RTSPS stream and serves
     `/cam.jpg` (single frame) + `/cam.mjpeg` (continuous multipart
     stream). The server proxies `/cam.jpg` polling at ~10 fps; the
     browser displays it as `<img src="/cam.jpg?t=...">` or as
     `<video>` if we MJPEG-stream it.

UI:

```
┌───────────────────────────────────────────────────────────┐
│  X2D — printing rumi_frame.gcode  [pause] [stop]          │
├───────────────────────────────────────────────────────────┤
│  ┌──────────────────────────┐  ┌────────────────────────┐ │
│  │                          │  │ Progress: 43% (38/194) │ │
│  │   [live camera frame]    │  │ Time left: 3h 03m      │ │
│  │                          │  │ Layer: 38              │ │
│  │                          │  │ Nozzle L: 230°C/230    │ │
│  │                          │  │ Nozzle R: 245°C/245    │ │
│  │                          │  │ Bed:      65°C/65      │ │
│  └──────────────────────────┘  │ Chamber:  35°C         │ │
│                                │                        │ │
│  ┌──────────────────────────┐  │ AMS state:             │ │
│  │  GCode preview overlay   │  │  S0 ●  PLA    Black    │ │
│  │  (current line + next 5) │  │  S1 ●  PLA    Green    │ │
│  └──────────────────────────┘  │  S2 ●  PLA    Purple   │ │
│                                │  ...                   │ │
│                                └────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
```

Pause / Stop / Light / etc buttons hit the existing
`x2d_bridge.py cloud-pause / cloud-resume / cloud-stop /
cloud-chamber-light` REST equivalents the server exposes.

## Phase 10 — 2nd extruder for infill specifically (covered in Phase 4 + 5)

Already addressed in Phase 4 (palette swatches scope per-role) and
Phase 5 (RemixPanel has separate Walls / Infill / Support extruder
toggles). The serialiser writes the right keys into the 3MF:

```xml
<object id="2">
  <metadata key="extruder"               value="1"/>  <!-- default body extruder -->
  <metadata key="wall_filament"          value="1"/>  <!-- walls = left -->
  <metadata key="sparse_infill_filament" value="2"/>  <!-- INFILL = right -->
  <metadata key="solid_infill_filament"  value="2"/>  <!-- top/bottom solids = right -->
  <metadata key="support_filament"       value="1"/>  <!-- support = left -->
</object>
```

The X2D firmware + Bambu slicer engine respects these per-region
overrides. The wipe-tower / purge-block placement handles the L↔R
swaps automatically — at the cost of extra purge filament per layer.
For prints where infill colour doesn't matter (common case), this is
a great way to put cheap PLA infill behind expensive AMS-Pro silk
shells.

## Cross-cutting concerns

  * **Auth** — craftmatic's existing session/cookie auth wraps every
    new route. `/print` requires an authenticated user that owns the
    printer. Per-printer ACL via the user's bound-devices list (we
    fetch via `x2d_bridge.py cloud-printers`).
  * **Rate-limit** — `/slice` is expensive (CPU-bound). One job per
    user at a time; queue the rest. Server uses `p-queue` or BullMQ.
  * **Multi-printer** — server speaks to multiple `x2d_bridge.py
    serve` instances (one per printer), keyed by `~/.x2d/credentials`
    section name (`printer:NAME`). UI has a printer selector at the
    top.
  * **Offline mode** — when the LAN printer is offline, fall back to
    cloud MQTT (`POST /cloud/print` already exists in
    `x2d_bridge.py`). Browser doesn't need to know which path was
    taken.
  * **Mobile-friendly** — touchscreen-first viewport, large hit
    targets, swipe to rotate camera. Use `OrbitControls` with touch
    enabled.

## Effort estimate (single dev, full-time)

| Phase                            | Effort      | Notes                                        |
|----------------------------------|-------------|----------------------------------------------|
| 0 — Server scaffold              | 3-5 days    | Wraps existing Python tools, low risk        |
| 1 — STL/3MF importer             | 3-5 days    | Three.js loaders + 3MF metadata parser       |
| 2 — 3D viewport + build plate    | 1-2 weeks   | OrbitControls, picking, plate textures       |
| 3 — Object manipulation, plates  | 1 week      | TransformControls, multi-plate swap          |
| 4 — AMS palette + extruder UI    | 1 week      | Live state binding, role-scoped assignments  |
| 5 — Per-object remix panel       | 3-5 days    | Wraps `remix_3mf.py` schema in form UI       |
| 6 — Slicing (server-side)        | 1-2 weeks   | `/slice` endpoint, queue, error handling     |
| 6b — Slicing (WASM, optional)    | 3-6 months  | Emscripten port — defer to v2                |
| 7 — GCode preview                | 1-2 weeks   | Parser + Three.js LineSegments + scrubber    |
| 8 — Send to printer              | 1 week      | `/print` route, browser confirm dialog       |
| 9 — Live monitor                 | 1-2 weeks   | SSE/WS state stream + camera proxy + UI      |
| 10 — Infill-extruder split (incl)| (in 4+5)    | 3MF metadata write — engine already supports |
| **TOTAL (browser+server, no WASM)** | **8-12 weeks** | Full feature parity with Prepare/Preview |
| **TOTAL with WASM slicing**         | **+3-6 months** | Fully offline web slicer                |

## Risk register

  - **Bambu changes the 3MF schema** — we already track this via
    `preflight_3mf.py`'s known-keys list; bump on each BS release.
  - **Cert rotation** — already monitored via `bambu_cert.py validate`;
    cron alerts if signed publishes start failing.
  - **AMS doesn't report `tray_sub_brands` on X2D** — already worked
    around in Phase 4 (match by color hex / info_idx, see
    `lan_print.py --filament-color/--filament-info-idx`).
  - **Wipe-tower purge bloat with per-region extruders** — UI surfaces
    estimated purge in the slice result so users can tune.

## Reuse map

What we keep from the Termux/Python codebase:
  - `x2d_bridge.py serve` — bridge daemon (LAN + cloud MQTT, signed
    publishes, FTPS upload, bambu_cert rotation)
  - `lan_print.py` — same FTPS-upload + signed start_print path the
    server's `/print` route hits
  - `remix_3mf.py` — referenced by the importer/exporter
  - `preflight_3mf.py` — server runs it before forwarding to printer
  - `bambu_cert.py` — re-used for cert validation cron + signing
  - `cloud_client.py` — REST against Bambu cloud (login, printers,
    state, cloud-publish fallback)
  - `bin/bambu-studio` — invoked headlessly for slicing
  - `runtime/network_shim/x2d_bridge.py` Unix-socket protocol —
    server speaks it directly

What we drop:
  - The wxWidgets/GTK GUI entirely (this whole document is about
    replacing it)
  - The termux-x11 dependency
  - All the wxLocale / wxGLCanvas / wxAuiManager workarounds
  - `runtime/preload_gtkinit.c` LD_PRELOAD shim

## File tree (target)

```
craftmatic/
├── src/
│   ├── io/
│   │   ├── 3mf.ts           # Phase 1
│   │   ├── stl.ts           # Phase 1
│   │   └── gcode.ts         # Phase 7
│   ├── viewport/
│   │   ├── scene.ts         # Phase 2
│   │   ├── plate.ts         # Phase 2
│   │   ├── object.ts        # Phase 3
│   │   ├── transformControls.ts # Phase 3
│   │   └── outlinePass.ts   # Phase 2
│   ├── ams/
│   │   ├── palette.ts       # Phase 4
│   │   └── stateStream.ts   # Phase 9
│   ├── remix/
│   │   ├── panel.tsx        # Phase 5
│   │   ├── overrides.ts     # Phase 5 (mirrors remix_3mf.py)
│   │   └── infillExtruder.ts # Phase 10 (Phase 4+5 wiring)
│   ├── slice/
│   │   └── client.ts        # Phase 6 — POSTs to /slice
│   ├── preview/
│   │   ├── parser.ts        # Phase 7
│   │   ├── lineSegments.ts  # Phase 7
│   │   └── scrubber.tsx     # Phase 7
│   ├── send/
│   │   └── client.ts        # Phase 8
│   └── monitor/
│       ├── stateView.tsx    # Phase 9
│       ├── cameraView.tsx   # Phase 9
│       └── controls.tsx     # Phase 9 (pause/stop/light)
└── tests/
    └── ...                   # Vitest, one suite per src/ module

craftmatic-server/
├── src/
│   ├── routes/
│   │   ├── slice.ts         # Phase 0/6
│   │   ├── print.ts         # Phase 0/8
│   │   ├── state.ts         # Phase 0/9 — SSE
│   │   └── cam.ts           # Phase 0/9 — JPEG proxy
│   ├── bridge.ts            # Unix socket client to x2d_bridge.py
│   └── queue.ts             # p-queue for slice jobs
└── tests/
    └── ...
```

## Acceptance criteria (per phase)

  * Phase 0: `curl POST /slice` returns a valid 3MF; `curl POST
    /print` queues a print on the live X2D.
  * Phase 1: open every `.gcode.3mf` in this repo's root; assert
    object names + counts match `unzip -p .. Metadata/model_settings.config`.
  * Phase 2: build plate + grid render at 60 fps in Chrome on the
    user's S25 Ultra; OrbitControls work touch + mouse.
  * Phase 3: drag-drop / scale / rotate gizmos persist to project
    state; multi-plate swap preserves per-plate state.
  * Phase 4: live AMS state appears in palette in <1 s after
    `cloud-state` push; clicking a swatch updates per-object
    `extruder` override.
  * Phase 5: applying a remix triggers a fresh slice; sliced .gcode.3mf
    has the new override values (verified via `remix_3mf.py
    --inspect`).
  * Phase 6 server: 10-MB STL slices in <60 s; queue length surfaces
    to the UI; failures return structured error.
  * Phase 7: scrubber moves smoothly through every layer of a
    100-layer print; layer count + estimated time match the gcode
    header.
  * Phase 8: clicking "Start Print" results in the X2D actually
    printing the job (live verified).
  * Phase 9: live state push refreshes <1 s after printer reports;
    camera frame updates at ≥5 fps.
  * Phase 10: 2-color print sliced for X2D with infill on extruder 2
    actually prints with right-nozzle infill (live verified).

## Hand-off

Each phase has a single concrete acceptance criterion above and lives
in its own `craftmatic/src/<module>/` folder so the work parallelises
across multiple devs if needed. Server-side phase 6/8/9 wraps existing
shell-stable Python; the only truly novel C++/WASM work is the
optional Phase 6b WASM slicer.

Net: full Prepare → Preview → Send → Monitor parity in **8-12 weeks
of single-dev effort**, server slicing path. Add another **3-6 months**
for the optional offline-WASM slicer.
