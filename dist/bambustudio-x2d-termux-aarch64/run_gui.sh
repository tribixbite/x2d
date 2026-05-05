#!/usr/bin/env bash
# Resolve our own location so the script works whether the repo lives at
# ~/git/x2d, /opt/x2d, etc. — never hardcode the developer's path.
set -e
HERE="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BS_BIN="${HERE}/bin/bambu-studio"
PRELOAD_SO="${HERE}/runtime/libpreloadgtk.so"

# Repo-checkout fallback: when the script lives at the repo root rather than
# at the unpacked-tarball root, the binary and shim are one level deeper.
[[ -x "${BS_BIN}" ]] || BS_BIN="${HERE}/bs-bionic/build/src/bambu-studio"
[[ -f "${PRELOAD_SO}" ]] || PRELOAD_SO="${HERE}/runtime/libpreloadgtk.so"

if [[ ! -x "${BS_BIN}" ]]; then
    echo "ERROR: bambu-studio not found (looked at \$HERE/bin and \$HERE/bs-bionic/build/src)" >&2
    exit 1
fi
if [[ ! -f "${PRELOAD_SO}" ]]; then
    echo "ERROR: libpreloadgtk.so missing — rebuild via:" >&2
    echo "  gcc -fPIC -shared ${HERE}/runtime/preload_gtkinit.c \\" >&2
    echo "      \$(pkg-config --cflags --libs gtk+-3.0) -ldl -o ${PRELOAD_SO}" >&2
    exit 1
fi

export DISPLAY="${DISPLAY:-:1}"
# Forces wxLocale through the C/UTF-8 path; the LD_PRELOAD shim handles
# the bionic-specific gaps (en_US suffix retry + wxUILocale ICU bypass).
export LC_ALL="${LC_ALL:-C}"
export LANG="${LANG:-C}"

# Spawn a tiny window manager if one is installed and not already running.
# termux-x11 has no built-in WM, which means:
#   - GtkFileChooserDialog and other transient dialogs open with no title
#     bar, can't be dragged, and stack at (0,0) under the main frame so
#     clicks land on the main frame instead of the dialog (Cancel buttons
#     "don't work").
#   - wxFrame::Maximize() / Iconize() are no-ops because EWMH state hints
#     have no listener.
# Any EWMH-aware WM fixes both classes of problem. Install one of:
#     pkg install openbox     # ~600 KB, recommended
#     pkg install matchbox-window-manager
#     pkg install fluxbox
# If none are present we still launch (via the in-app patches) but dialogs
# will be janky.
if ! pgrep -f -u "$(id -u)" '(openbox|matchbox-window-manager|fluxbox|jwm)' >/dev/null 2>&1; then
    for wm in openbox matchbox-window-manager fluxbox jwm; do
        if command -v "$wm" >/dev/null 2>&1; then
            "$wm" >/dev/null 2>&1 &
            disown $! 2>/dev/null || true
            echo "[run_gui] spawned $wm" >&2
            sleep 0.4
            break
        fi
    done
fi
export WXSUPPRESS_SIZER_FLAGS_CHECK=1
export WXSUPPRESS_DBL_CLICK_ASSERT=1
export WXASSERT_DISABLE=1

mkdir -p "${HOME}/.config/BambuStudio"
if [[ ! -s "${HOME}/.config/BambuStudio/BambuStudio.conf" ]]; then
    echo '{ "app": { "language": "en_US", "first_run": false } }' \
        > "${HOME}/.config/BambuStudio/BambuStudio.conf"
fi

export LD_PRELOAD="${PRELOAD_SO}${LD_PRELOAD:+:${LD_PRELOAD}}"

# Hardware acceleration via ANGLE-GL + virgl, gated on virglrenderer-android
# and angle-android being installed. Falls back to llvmpipe (software) if not.
# Reference: sabamdarif/termux-desktop `enable-hw-acceleration` — the recipe
# is `EPOXY_USE_ANGLE=1 virgl_test_server_android --angle-gl &` on the server
# AND `GALLIUM_DRIVER=virpipe EPOXY_USE_ANGLE=1` on the client. ANGLE-GL is
# 60% faster than ANGLE-Vulkan-via-virgl on Adreno per the bench in
# termux/termux-packages#17406, and ~12x faster than llvmpipe.
ANGLE_DIR="$PREFIX/opt/angle-android/vulkan"
VIRGL_BIN="$PREFIX/bin/virgl_test_server_android"
WRAPPER_ICD="$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json"
unset EPOXY_USE_ANGLE MESA_GL_VERSION_OVERRIDE MESA_GLES_VERSION_OVERRIDE \
      MESA_GLSL_VERSION_OVERRIDE LIBGL_DRI3_DISABLE GALLIUM_DRIVER \
      LIBGL_ALWAYS_SOFTWARE MESA_LOADER_DRIVER_OVERRIDE EGL_PLATFORM \
      VK_ICD_FILENAMES

# Hardware-acceleration topology (resolved 2026-05-04 — see #95/#96 in
# IMPROVEMENTS.md). X2D_USE_ADRENO=1 (default ON) uses virgl_test_server_android
# as the desktop-GL → ANGLE-Vulkan → Adreno bridge. The libEGL_x2dadreno.so
# vendor (still installed for non-BS GLES apps) can't replace virgl because
# ANGLE provides only GLES, but BS calls desktop-GL functions through
# libGL.so.1.
if [[ "${X2D_USE_ADRENO:-1}" == "1" ]] && [[ -d "$ANGLE_DIR" ]] && [[ -x "$VIRGL_BIN" ]]; then
    export MESA_NO_ERROR=1
    export MESA_GL_VERSION_OVERRIDE=4.3COMPAT
    export MESA_GLES_VERSION_OVERRIDE=3.2
    export MESA_GLSL_VERSION_OVERRIDE=430
    export GALLIUM_DRIVER=virpipe
    export LIBGL_DRI3_DISABLE=1
    export EPOXY_USE_ANGLE=1
    [[ -f "$WRAPPER_ICD" ]] && export VK_ICD_FILENAMES="$WRAPPER_ICD"
    export LD_LIBRARY_PATH="$ANGLE_DIR${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    if ! pgrep -f virgl_test_server_android >/dev/null 2>&1; then
        # `--angle-vulkan` not `--angle-gl` — see #100 in IMPROVEMENTS.md.
        # The GL backend pulls libgtk-3 which needs `epoxy_glXQueryExtension`
        # absent from virgl's bundled libepoxy. Vulkan path skips X11/GLX
        # entirely and reaches Adreno via leegaos vulkan_wrapper just fine.
        EPOXY_USE_ANGLE=1 \
        LD_LIBRARY_PATH="$ANGLE_DIR${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
        ${WRAPPER_ICD:+VK_ICD_FILENAMES="$WRAPPER_ICD"} \
        "$VIRGL_BIN" --angle-vulkan \
            > "${TMPDIR:-/data/data/com.termux/files/usr/tmp}/virgl_server.log" 2>&1 &
        sleep 1
    fi
    echo "[run_gui] hw-accel via virgl + ANGLE-Vulkan → Adreno 830"
else
    # Software fallback — llvmpipe via XPutImage. Slow but always works.
    # zink_kopper.c:720 asserts on swapchain acquire because termux-x11 has no
    # DRI3/Present; force llvmpipe to bypass zink entirely.
    export GALLIUM_DRIVER=llvmpipe
    export LIBGL_ALWAYS_SOFTWARE=1
    export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
    export EGL_PLATFORM=surfaceless
    echo "[run_gui] software fallback (llvmpipe) — slow"
fi

# x2d/termux #88 — suppress the "Could not read the contents of /" GTK popup.
# gvfsd-trash enumerates / at startup looking for writable trash dirs and
# fails on Android (no read perm on /). The GTK file monitor pops a modal
# that the user has to dismiss every launch. Routing all GIO through the
# local backend bypasses gvfs entirely.
export GIO_USE_VFS=local
export GVFS_DISABLE_FUSE=1
# also kill any persistent gvfsd-trash from xfce4-session — see comment in
# the dev run_gui.sh; GIO_USE_VFS in BS doesn't stop the daemon's own
# enumerate of / which dbus-pops the popup at GTK file monitor connect.
pkill -TERM gvfsd-trash 2>/dev/null || true
pkill -TERM gvfsd-recent 2>/dev/null || true
# Thunar (xfce4-session auto-spawn) is the actual source of the
# "Could not read the contents of /" popup — it scans / for the desktop
# location panel. BS doesn't need Thunar; killing it is safe for our
# slicer-only use case. The xfce4-panel + xfwm4 stay running.
pkill -TERM Thunar 2>/dev/null

# x2d/termux #88 — wxFrame::Maximize() in BambuStudio source runs before
# xfwm4 finishes mapping the window, so the EWMH _NET_WM_STATE_MAXIMIZED
# request is dropped. Background a small watcher that polls for the
# bambu-studio main window and forces it to fill the screen via
# `xdotool windowsize 100% 100%`. Caps at 30s so it doesn't run forever
# if BS fails to start.
if command -v xdotool >/dev/null 2>&1; then
    (
        for _ in $(seq 1 60); do
            sleep 0.5
            BS_PID=$(pgrep -f "/bambu-studio$" | head -1)
            [ -z "$BS_PID" ] && continue
            for wid in $(xdotool search --pid "$BS_PID" 2>/dev/null); do
                geom=$(xwininfo -id "$wid" 2>/dev/null | grep -E "Width: (1000|1[0-9]{3})")
                if [ -n "$geom" ]; then
                    DISP_W=$(xdpyinfo 2>/dev/null | awk '/dimensions/{split($2,a,"x"); print a[1]}')
                    DISP_H=$(xdpyinfo 2>/dev/null | awk '/dimensions/{split($2,a,"x"); print a[2]}')
                    [ -z "$DISP_W" ] && DISP_W=1080
                    [ -z "$DISP_H" ] && DISP_H=2340
                    WORK_H=$((DISP_H - 140))
                    xdotool windowsize "$wid" "$DISP_W" "$WORK_H" 2>/dev/null
                    xdotool windowmove "$wid" 0 0 2>/dev/null
                    exit 0
                fi
            done
        done
    ) &
fi
# Ensure xfce4-panel-1 reserves screen space (struts) so maximized apps
# don't draw over it. New installs default to enable-struts=false on the
# main bottom dock — flip it once at launch. Idempotent.
if command -v xfconf-query >/dev/null 2>&1; then
    xfconf-query -c xfce4-panel -p /panels/panel-1/enable-struts -s true 2>/dev/null || true
fi

# ---------------------------------------------------------------------
# Bridge supervisor (item #12).
#
# The shim spawns x2d_bridge.py serve once on first connect_printer. If
# that bridge dies (paho segfault, OOM, network blip), the socket file
# lingers and every subsequent shim RPC fails with ECONNREFUSED — the
# GUI silently loses Connect / AMS / Print until the user restarts
# everything. Watchdog: bash loop with 1→2→5→10→30s backoff, log
# rotated at 1 MiB. Trapped on EXIT.
# ---------------------------------------------------------------------

X2D_HOME="${HOME}/.x2d"
mkdir -p "${X2D_HOME}"
BRIDGE_SOCK="${X2D_HOME}/bridge.sock"
BRIDGE_LOG="${X2D_HOME}/bridge.log"

# bridge_py: prefer the canonical install path, fall back to the repo
# checkout (developer mode), then PATH lookup. Mirrors the candidates
# baked into runtime/network_shim/src/bridge_client.cpp.
for cand in \
    "${HERE}/helpers/x2d_bridge.py" \
    "${HERE}/x2d_bridge.py" \
    "/data/data/com.termux/files/home/git/x2d/x2d_bridge.py"
do
    if [[ -f "$cand" ]]; then
        BRIDGE_PY="$cand"
        break
    fi
done

bridge_watchdog() {
    local backoff=1
    while true; do
        # Log rotation: cap at 1 MiB, keep one .1 generation.
        if [[ -f "${BRIDGE_LOG}" ]] && \
           (( $(stat -c %s "${BRIDGE_LOG}" 2>/dev/null || echo 0) > 1048576 )); then
            mv -f "${BRIDGE_LOG}" "${BRIDGE_LOG}.1"
            : > "${BRIDGE_LOG}"
        fi
        # Stale socket cleanup — Unix sockets aren't auto-removed when
        # the owner dies and bind() fails with EADDRINUSE on retry.
        [[ -S "${BRIDGE_SOCK}" ]] && rm -f "${BRIDGE_SOCK}"
        echo "[$(date +%H:%M:%S)] watchdog: spawning x2d_bridge serve" >> "${BRIDGE_LOG}"
        python3.12 "${BRIDGE_PY}" serve --sock "${BRIDGE_SOCK}" \
            >> "${BRIDGE_LOG}" 2>&1
        local rc=$?
        echo "[$(date +%H:%M:%S)] watchdog: bridge exited rc=$rc; sleeping ${backoff}s" \
            >> "${BRIDGE_LOG}"
        sleep "${backoff}"
        case $backoff in
            1)  backoff=2  ;;
            2)  backoff=5  ;;
            5)  backoff=10 ;;
            10) backoff=30 ;;
        esac
    done
}

if [[ -n "${BRIDGE_PY:-}" ]] && \
   ! pgrep -f "x2d_bridge.py serve" >/dev/null 2>&1; then
    bridge_watchdog &
    BRIDGE_WATCHDOG_PID=$!
    trap 'kill -TERM "${BRIDGE_WATCHDOG_PID}" 2>/dev/null; \
          pkill -TERM -f "x2d_bridge.py serve" 2>/dev/null; \
          pkill -TERM -f "x2d_bridge.py camera" 2>/dev/null' EXIT
    # Wait up to 3s for the bridge to bind the socket so the shim's
    # first ensure_socket() finds it ready and skips its own spawn.
    for _ in 1 2 3 4 5 6; do
        [[ -S "${BRIDGE_SOCK}" ]] && break
        sleep 0.5
    done
fi

# ---------------------------------------------------------------------
# Camera daemon supervisor (item #70). Spawns x2d_bridge.py camera
# (port 322 RTSPS → 127.0.0.1:8767/cam.jpg) so the patched
# MediaPlayCtrl can render the live X2D feed via libcurl JPEG poll
# inside the Camera widget. Skip if no creds file (LAN auth needed
# for RTSPS) or if a camera daemon is already running.
# ---------------------------------------------------------------------
CAM_LOG="${X2D_HOME}/camera.log"
CAM_PORT="${X2D_CAMERA_PORT:-8767}"
if [[ -n "${BRIDGE_PY:-}" ]] && [[ -f "${X2D_HOME}/credentials" ]] && \
   ! pgrep -f "x2d_bridge.py camera" >/dev/null 2>&1; then
    (
        backoff=1
        while true; do
            if [[ -f "${CAM_LOG}" ]] && \
               (( $(stat -c %s "${CAM_LOG}" 2>/dev/null || echo 0) > 1048576 )); then
                mv -f "${CAM_LOG}" "${CAM_LOG}.1"; : > "${CAM_LOG}"
            fi
            echo "[$(date +%H:%M:%S)] watchdog: spawning x2d_bridge camera" >> "${CAM_LOG}"
            python3.12 "${BRIDGE_PY}" camera --port 322 \
                --bind "127.0.0.1:${CAM_PORT}" >> "${CAM_LOG}" 2>&1
            echo "[$(date +%H:%M:%S)] watchdog: camera exited rc=$?; sleep ${backoff}s" >> "${CAM_LOG}"
            sleep "${backoff}"
            case $backoff in 1) backoff=2 ;; 2) backoff=5 ;; 5) backoff=10 ;; 10) backoff=30 ;; esac
        done
    ) &
    CAM_WATCHDOG_PID=$!
    # Wait up to 8s for the camera HTTP server to bind so the JPEG
    # poller in MediaPlayCtrl finds /cam.jpg ready on first Play().
    for _ in $(seq 1 16); do
        if curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:${CAM_PORT}/cam.jpg" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
fi

# Tell the patched MediaPlayCtrl to load /cam.jpg via libcurl
# (BeginJpegPoll path) instead of going through wxMediaCtrl3 →
# gstreamer playbin (which has known issues on Termux unless the
# soup plugin is rebuilt — see runtime/build_gst_soup_termux.sh).
# DO NOT point at /cam.mjpeg here — the libcurl path expects a
# single-frame endpoint; /cam.mjpeg is a never-terminating
# multipart stream that would just hang the worker.
export X2D_CAMERA_URL="${X2D_CAMERA_URL:-http://127.0.0.1:${CAM_PORT}/cam.jpg}"

cd "$(dirname "${BS_BIN}")"
exec "${BS_BIN}" "$@"
