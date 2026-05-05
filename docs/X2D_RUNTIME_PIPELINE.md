# X2D Runtime Pipeline — Termux + termux-x11 + Adreno 830

End-to-end stack for running BambuStudio (and the X2D bridge) on
Galaxy S25 Ultra under Termux + termux-x11 with hardware-accelerated
GL via the Adreno 830 GPU.

Resolved 2026-05-05 across IMPROVEMENTS.md items #80, #85, #86, #88,
#90, #92, #95, #96, #97, #98, #99, #100, #101, #102, #103, #104.
This doc is the canonical reference; run_gui.sh is the canonical
implementation.

---

## TL;DR — start everything from a clean shell

```bash
# 0. Android-side: open Termux:X11 app, accept the connection prompt.

# 1. X server + WM (one-shot, runs in background)
termux-x11 :1 -ac >/dev/null 2>&1 &
sleep 1
DISPLAY=:1 dbus-launch --exit-with-session xfce4-session >/dev/null 2>&1 &

# 2. BambuStudio + bridge supervisor + camera daemon (one-shot)
cd ~/git/x2d && DISPLAY=:1 nohup ./run_gui.sh </dev/null >>~/.x2d/run_gui.log 2>&1 & disown
```

That's it. The launcher handles the GL stack, the bridge watchdog,
the camera proxy, and BambuStudio itself.

For an orchestrator service definition (see `tmx` example below),
collapse to one line:

```bash
sh -c 'pgrep -f "Xtermux-x11 :1" >/dev/null || (termux-x11 :1 -ac >/dev/null 2>&1 & sleep 1; DISPLAY=:1 dbus-launch --exit-with-session xfce4-session >/dev/null 2>&1 &); cd ~/git/x2d && DISPLAY=:1 ./run_gui.sh'
```

---

## Render chain (active by default — `X2D_USE_ADRENO=1`)

```
BS wxGLCanvas
  └─ libGL.so.1          ← libglvnd ($PREFIX/lib)
       └─ Mesa virpipe   ← GALLIUM_DRIVER=virpipe
            └─ virgl_test_server_android --angle-vulkan
                 ├─ libEGL_angle.so       ← ANGLE on Android
                 └─ libGLESv2_angle.so
                      └─ libvulkan_wrapper.so   ← leegaos fork
                           └─ libvulkan.so      ← bionic loader
                                └─ /vendor/lib64/hw/vulkan.adreno.so
                                     └─ Adreno 830 hardware
```

Fallback if `X2D_USE_ADRENO=0`: BS → libGL.so.1 → Mesa llvmpipe
(software, ~8 fps for the Plater scene; ~25× slower than the
hw-accel path).

---

## Layer-by-layer — what each shim/env-var fixes

| Layer | Knob / file | Reason it's needed |
|-------|-------------|-------------------|
| **wx static-init** | `LD_PRELOAD=runtime/libpreloadgtk.so` | wx 3.3 calls `wxFont::AddPrivateFont` from a static initializer, which touches `GtkCssProvider` before `gtk_init`. Shim's high-priority ctor calls `gtk_init_check()` first. |
| **Locale** | `LC_ALL=C`, `LANG=en_US.UTF-8` + `~/.config/BambuStudio/BambuStudio.conf` seeded with `language=en_US` | wxLocale insists on a named locale that bionic's libicu doesn't know; the shim also overrides `wxLocale::IsAvailable` + `setlocale`/`newlocale` to retry with `.UTF-8` suffix. |
| **wxLocale failure dialog** | shim overrides `_ZN10wxUILocale11IsAvailableEi` + `_ZN8wxLocale11IsAvailableEi` | otherwise BS pops "Switching language failed" then `std::exit(EXIT_FAILURE)`. |
| **gvfs popup** | `GIO_USE_VFS=local`, `GVFS_DISABLE_FUSE=1`, `pkill gvfsd-trash gvfsd-recent` | gvfsd-trash enumerates `/` looking for writable trash dirs and pops a "Could not read the contents of /" modal on every launch. |
| **wxGLCanvasEGL `IsShownOnScreen` gate** | `_ZNK13wxGLCanvasEGL15IsShownOnScreenEv` shim returns `true`; BS source patch falls back to `IsShown()` | wx 3.3 `glegl.cpp:832` gates `eglSwapBuffers` on `IsShownOnScreen`, which queries EWMH/`_NET_WM_STATE` — termux-x11 implements neither, so SwapBuffers never swaps. |
| **Mesa version override** | `MESA_NO_ERROR=1`, `MESA_GL_VERSION_OVERRIDE=4.3COMPAT`, `MESA_GLES_VERSION_OVERRIDE=3.2`, `MESA_GLSL_VERSION_OVERRIDE=430` | BS rejects with "OpenGL version lower than 2.0" if `glGetString(GL_VERSION)` doesn't parse a version ≥3.4. |
| **DRI3 absence** | `LIBGL_DRI3_DISABLE=1` | termux-x11 doesn't implement DRI3/Present. Without this, Mesa picks the zink path and `kopper_swapchain_acquire` asserts. |
| **Mesa → ANGLE bridge** | `GALLIUM_DRIVER=virpipe`, `EPOXY_USE_ANGLE=1`, `LD_LIBRARY_PATH=$PREFIX/opt/angle-android/vulkan` | virpipe sends GL through the virgl protocol; libepoxy in BS auto-resolves to ANGLE's libGL/libEGL when `EPOXY_USE_ANGLE=1`. |
| **virgl spawn** | `env -i …` (full env scrub) + `--angle-vulkan` (NOT `--angle-gl`) | (a) `--angle-gl` pulls libgtk-3 transitively which needs `epoxy_glXQueryExtension` that virgl's bundled libepoxy doesn't expose; (b) Termux's `libtermux-exec.so` re-injects `LD_PRELOAD=libpreloadgtk.so` even after `LD_PRELOAD=` empty, only `env -i` actually scrubs. |
| **ANGLE → Adreno bridge** | `VK_ICD_FILENAMES=$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json` | The wrapper ICD points at `libvulkan_wrapper.so` (leegaos fork) which bridges Vulkan calls into `/vendor/lib64/hw/vulkan.adreno.so` (the real Adreno driver). Without it, Vulkan picks lavapipe (software) and ANGLE-Vulkan ends up no faster than llvmpipe. |
| **GLCanvas3D paint race** | BS source patch (`patches/GLCanvas3D.cpp.termux.patch`): `init()` calls `m_dirty=true; m_canvas->Refresh(); m_canvas->Update();` after `m_initialized=true` | Paint events fired BEFORE the GL context was ready hit `render()` → bails on `!_is_shown_on_screen()/!_set_current()/!init()` and never re-arms. The post-init Refresh re-queues a paint that succeeds. |
| **wxGLCanvas size race** | BS source patch: `_set_current` force-sizes the canvas from the parent's client area when its own size is ≤0 | termux-x11 returns stale negative sizes for the canvas until first parent Layout pass; without this, `wxGLCanvasEGL` refuses surface allocation. |
| **Touch hit-test** | BS source patches removing `wxRect::Contains` checks in `Button::mouseReleased` etc. | Touchscreen drift between mousedown and mouseup pushes the up-coords off the button rect, dropping the click. |
| **AMS panel sizing** | `AMSItem.cpp` AMS_CANS_WINDOW_SIZE 320→360, `BBLTopbar.cpp` TOPBAR_ICON_SIZE 18→32, `Label.cpp` font-shrink removed | All four AMS slots fit in landscape on 1080-wide phone; tab targets ≥48dp Material minimum. |

---

## Background processes (managed by `run_gui.sh`)

| Daemon | Role | Restart policy | Log |
|--------|------|----------------|-----|
| `virgl_test_server_android --angle-vulkan` | GL renderer (one per shell, shared by all BS instances) | none — re-spawns on next `run_gui.sh` if missing | `$TMPDIR/virgl_server.log` |
| `x2d_bridge.py serve --sock ~/.x2d/bridge.sock` | RPC server for `libbambu_networking.so` shim | watchdog with exp-backoff (1→2→5→10→30s), 1 MiB log rotation | `~/.x2d/bridge.log` |
| `x2d_bridge.py camera --port 322 --bind 127.0.0.1:8767` | RTSPS-to-MJPEG proxy for `MediaPlayCtrl` (only if `~/.x2d/credentials` exists) | same watchdog | `~/.x2d/camera.log` |
| `bambu-studio` | the GUI | foreground, killed by user closing the window | (encrypted in `~/.config/BambuStudioInternal/log/`) |

---

## Headless / CLI / scripted workflows

```bash
# Slice an STL with the X2D 0.4-nozzle profile
x2d_slice.py model.stl --out out.gcode.3mf
x2d_slice.py model.stl --out out.gcode.3mf --scale 0.7 --color "#FF8800"

# One-shot: slice + upload + start print on the X2D
x2d_bridge slice-print model.stl --scale 0.5 --color "#00FF00"
x2d_bridge slice-print model.stl --dry-run            # produce the 3mf only

# Fetch a model from MakerWorld / Printables / Thingiverse / direct URL
x2d_bridge fetch https://raw.githubusercontent.com/CreativeTools/3DBenchy/master/Single-part/3DBenchy.stl
x2d_bridge fetch https://makerworld.com/en/models/<id>
x2d_bridge fetch https://www.thingiverse.com/thing:<id>     # needs THINGIVERSE_TOKEN env
x2d_bridge fetch <url> --open                                # spawn BS with the file preloaded

# Diagnostics
x2d_bridge health    # all-in-one printer reachability + MQTT + camera + bridge socket
x2d_bridge watch     # live one-line status stream (every N s)
x2d_bridge files [/|timelapse|cache|video|model] [--json]   # list X2D SD-card files via FTPS

# Print control
x2d_bridge upload <local.gcode.3mf>
x2d_bridge print <remote.gcode.3mf> [--slot N] [--no-ams] [--timelapse]
x2d_bridge pause | resume | stop
x2d_bridge home | level
x2d_bridge ams-load <SLOT> | ams-unload <SLOT>
x2d_bridge gcode "<G-code line>"
x2d_bridge set-temp nozzle 220 | set-temp bed 60 | set-temp chamber 35
x2d_bridge chamber-light on|off
x2d_bridge jog x 10 | jog z 0.2
x2d_bridge record start|stop | timelapse start|stop | resolution low|medium|high|full
x2d_bridge notify "message"   # via Termux notification API
```

---

## Opt-in: in-process EGL vendor for non-BS GLES apps

For apps that use **only** GLES (not desktop GL) and want to skip
the virgl IPC hop:

```bash
__EGL_VENDOR_LIBRARY_FILENAMES=$PREFIX/share/glvnd/egl_vendor.d/40_x2dadreno.json \
  X2D_ANGLE_DIR=$PREFIX/opt/angle-android/vulkan \
  ./your-gles-app
```

This routes `eglCreatePlatformWindowSurface` + `eglSwapBuffers`
through `libEGL_x2dadreno.so` (in-process X11→pbuffer redirect +
`glReadPixels`+`XPutImage` blit). Bench: ~200 fps for a 400×300
GLES2 fragment shader on this device. **Not compatible with BS**
because BS uses Mesa's libGL desktop functions which ANGLE doesn't
provide — see #96.

---

## tmx orchestrator service definition

Add to `~/.config/tmx/tmx.toml`:

```toml
[sessions.x2d]
description = "BambuStudio + bridge supervisor + Adreno hw-accel"
command = """
sh -c '
  pgrep -f "Xtermux-x11 :1" >/dev/null \
    || ( termux-x11 :1 -ac >/dev/null 2>&1 &
         sleep 1
         DISPLAY=:1 dbus-launch --exit-with-session xfce4-session >/dev/null 2>&1 & )
  cd ~/git/x2d
  DISPLAY=:1 exec ./run_gui.sh
'
"""
working_dir = "/data/data/com.termux/files/home/git/x2d"
restart = "on-failure"
```

If `tmx` is preferred over a raw `&` background, this gives you
session lifecycle + restart-on-failure + log rotation via the
orchestrator. The `pgrep` guard means rerunning this command
won't double-spawn the X server.

---

## Diagnose if something breaks

```bash
# Is the chain alive?
pgrep -fa virgl_test_server_android
pgrep -fa "x2d_bridge.py serve"
pgrep -fa "bambu-studio$"

# What's BS actually using?
PID=$(pgrep -f "^/data.*/bambu-studio$")
grep -E "virgl|libGL|libEGL_angle" /proc/$PID/maps | awk '{print $NF}' | sort -u
cat /proc/$PID/environ | tr '\0' '\n' | grep -E "GALLIUM|EPOXY|VK_ICD|LD_LIBRARY"

# Did virgl crash?
cat $TMPDIR/virgl_server.log
# Empty + virgl pid alive = good.
# "cannot locate symbol epoxy_glXQueryExtension" = LD_PRELOAD wasn't scrubbed
#   (rebuild run_gui.sh from source — env -i fix landed in c4db4af).

# Did the wrapper Vulkan ICD bridge to real Adreno?
DISPLAY=:1 vulkaninfo --summary | head -20
# Should list GPU0 = Adreno (TM) 830 with driverID = DRIVER_ID_QUALCOMM_PROPRIETARY.

# Bridge end-to-end health
x2d_bridge health
```

---

## Reference impls

* `run_gui.sh` — top-level launcher
* `dist/bambustudio-x2d-termux-aarch64/run_gui.sh` — distributable copy
* `runtime/preload_gtkinit.c` — gtk-init + locale-shim source
* `runtime/libEGL_x2dadreno.c` + `runtime/build_egl_vendor.sh` — opt-in vendor
* `patches/*.patch` — durable BS source patches (re-applied on rebuild)
* `bin/install.sh` — one-shot installer that `pkg install`s deps + drops vendor JSON + seeds `~/.config/BambuStudioInternal`
