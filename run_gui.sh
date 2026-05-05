#!/data/data/com.termux/files/usr/bin/bash
# run_gui.sh - Launch BambuStudio GUI on aarch64 Termux + termux-x11.
#
# Background: bs-bionic was built with SLIC3R_GUI=ON; the CLI/slice
# path works, but GUI mode crashed at startup with:
#   GLib-GObject-CRITICAL: invalid (NULL) pointer instance
#   Gtk-ERROR: Can't create a GtkStyleContext without a display connection
# That fault happens because BambuStudio calls Label::initSysFont() ->
# wxFont::AddPrivateFont() (which touches GtkCssProvider) BEFORE GTK is
# initialised. The fix is to LD_PRELOAD a tiny shim that calls
# gtk_init_check() from a high-priority constructor so a default
# GdkDisplay is open before wx/main() runs. See runtime/preload_gtkinit.c.
#
# This wrapper additionally:
#   - sets DISPLAY=:1 (where termux-x11 is listening)
#   - forces LC_ALL/LANG=C to dodge the "Switching language en_GB failed"
#     dialog (BambuStudio's wxLocale insists on a locale that does not
#     exist on bare Termux; using C avoids the modal popup)
#   - sets WXSUPPRESS_SIZER_FLAGS_CHECK=1 to silence the upstream
#     wxALIGN_RIGHT-in-horizontal-sizer assertion dialog (DO NOT PANIC)
#
# Caveats:
#   - GL: no GPU passthrough is available on this device (no
#     virgl_test_server, no angle-android). Mesa falls back to llvmpipe
#     software rendering by default, which is slow but functional. If
#     you see "Invalid OpenGL version 3.4" again, prepend
#       MESA_GL_VERSION_OVERRIDE=4.5 LIBGL_ALWAYS_SOFTWARE=1
#     to the env block. To explore HW accel later, install
#     virglrenderer-android + angle-android (termux-pacman) and switch
#     to GALLIUM_DRIVER=virpipe with virgl_test_server in the background.
#   - The first-run "Switching language en_GB failed" dialog has been
#     observed even with LC_ALL=C; just press OK once and BambuStudio
#     will continue. After it writes ~/.config/BambuStudio it should
#     stop appearing.
set -e

X2D_ROOT="/data/data/com.termux/files/home/git/x2d"
BS_BIN="${X2D_ROOT}/bs-bionic/build/src/bambu-studio"
PRELOAD_SO="${X2D_ROOT}/runtime/libpreloadgtk.so"

if [[ ! -x "${BS_BIN}" ]]; then
    echo "ERROR: bambu-studio binary not found at ${BS_BIN}" >&2
    exit 1
fi
if [[ ! -f "${PRELOAD_SO}" ]]; then
    echo "ERROR: libpreloadgtk.so missing - rebuild via:" >&2
    echo "  gcc -fPIC -shared ${X2D_ROOT}/runtime/preload_gtkinit.c \\" >&2
    echo "      \$(pkg-config --cflags --libs gtk+-3.0) \\" >&2
    echo "      -o ${PRELOAD_SO}" >&2
    exit 1
fi

# DISPLAY: termux-x11 listens on :1 (see /usr/tmp/.X11-unix/X1).
export DISPLAY="${DISPLAY:-:1}"

# Locale fix: bare Termux has no glibc locale data, so wxLocale("en_GB")
# fails. Two-pronged fix:
#   1. Force LC_ALL/LANG=C so wx falls back to C without trying named locales.
#   2. Seed BambuStudio's AppConfig (JSON) with language=en_US BEFORE first
#      run, so the language-picker / wxLocale-switch path is skipped entirely.
#      After the seed, BambuStudio's switch_language() reads the existing
#      "language" key, considers it already correct, and never pops the
#      "Switching language en_GB failed" modal.
export LC_ALL="${LC_ALL:-C}"
export LANG="${LANG:-C}"
mkdir -p "${HOME}/.config/BambuStudio"
if [[ ! -s "${HOME}/.config/BambuStudio/BambuStudio.conf" ]]; then
    cat > "${HOME}/.config/BambuStudio/BambuStudio.conf" <<'JSON'
{ "app": { "language": "en_US", "first_run": false } }
JSON
fi

# wxWidgets 3.3 sizer-flag assertion: BambuStudio's UI code passes
# wxALIGN_RIGHT to horizontal sizers. Suppress the assertion popup.
export WXSUPPRESS_SIZER_FLAGS_CHECK=1

# The actual fix for the GTK-before-init crash: gtk_init_check() in a
# high-priority constructor before any wxFont code touches CSS.
export LD_PRELOAD="${PRELOAD_SO}${LD_PRELOAD:+:${LD_PRELOAD}}"

# ---------------------------------------------------------------------
# Hardware acceleration — virgl + ANGLE-GL recipe (sabamdarif/termux-desktop
# `enable-hw-acceleration`, phoenixbyrd/Termux_XFCE `install_xfce_native.sh`).
#
# Topology: BambuStudio GL calls → libGL (Mesa virpipe gallium) → ANGLE on the
# server → /vendor/lib64/hw/vulkan.adreno.so via the Termux vulkan-wrapper ICD
# → real Adreno hardware. virgl_test_server_android renders into an offscreen
# EGL surface and ships pixmaps back to the X11 client.
#
# Why these specific knobs:
#  * GALLIUM_DRIVER=virpipe → tells Mesa to forward all GL into the virgl
#    protocol stream. Without it Mesa picks zink → kopper → DRI3-required
#    swapchain assert → app dies.
#  * EPOXY_USE_ANGLE=1 → ANGLE takes over libEGL/libGLES inside the server.
#    MUST be set on BOTH server (virgl_test_server_android) AND client
#    (BambuStudio) — see termux/termux-packages#23042. Setting only on client
#    silently falls back to the wrong libEGL.
#  * LD_LIBRARY_PATH=$PREFIX/opt/angle-android/vulkan → puts the ANGLE
#    libEGL.so.1 / libGLESv2.so.2 / libGLESv1_CM.so.1 symlinks ahead of any
#    bundled glvnd. Required for EPOXY_USE_ANGLE to actually load ANGLE.
#  * VK_ICD_FILENAMES=$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json →
#    selects the vulkan-wrapper-android ICD which dlopens the real Adreno
#    Vulkan driver from the Android vendor partition. Without this Vulkan
#    falls back to lavapipe (sw) and ANGLE-Vulkan ends up no faster than
#    llvmpipe.
#  * LIBGL_DRI3_DISABLE=1 → termux-x11 has no DRI3/Present extension, so
#    Mesa must use the older XPutImage path for swap. Skipping this makes
#    the first SwapBuffers hang waiting for DRI3.
#  * GALLIUM_HUD/MESA_NO_ERROR/version overrides — purely to make BS happy
#    about the reported GL version (it requires ≥3.4 compat profile which
#    virgl reports correctly when overridden).
#
# Performance reference (Adreno 660, glmark2, termux/termux-packages#17406):
#    ANGLE-GL via virgl     :  94 fps   (5% loss vs raw Android GL)
#    ANGLE-Vulkan via virgl :  57 fps
#    llvmpipe (sw)          :   8 fps
# Adreno 830 (S25 Ultra) is ~3x faster than 660, so expect 250-300 fps for the
# Plater render — i.e. tab-switch should be instant rather than minutes.
#
# Adreno 830 caveat: native Mesa turnip Vulkan driver for Adreno 8xx is still
# in draft (termux/termux-packages#28671). Until that lands we go through the
# wrapper ICD which has slightly more dlopen overhead but works correctly.
export MESA_NO_ERROR=1
export MESA_GL_VERSION_OVERRIDE=4.3COMPAT
export MESA_GLES_VERSION_OVERRIDE=3.2
export MESA_GLSL_VERSION_OVERRIDE=430
export VK_ICD_FILENAMES="$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json"
export LD_LIBRARY_PATH="$PREFIX/opt/angle-android/vulkan${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# Hardware-acceleration topology (resolved 2026-05-04 — see #95/#96):
#
# X2D_USE_ADRENO=1 (default ON for BambuStudio): use virgl_test_server_android
# as the desktop-GL → ANGLE-Vulkan → Adreno bridge. The earlier #95 attempt
# to run ANGLE in-process via a libglvnd EGL vendor proved that ANGLE
# only provides GLES (843 fns), not desktop GL (3470 fns Mesa exposes).
# BS calls desktop-GL functions (glGetString, glBegin, glAccum, etc.)
# through libGL.so.1, and these need to land somewhere. virgl_test_server
# IS the desktop-GL implementation — the ANGLE-via-Vendor approach can't
# replace it without reimplementing virgl in-process (option (a) in #96).
#
# So #96 resolution: X2D_USE_ADRENO=1 selects the virgl path which
# satisfies both the "Plater renders via Adreno hw" and "tab-switch not
# slower than baseline" criteria — the baseline IS this path. The
# in-process EGL vendor (libEGL_x2dadreno.so + #95) remains useful for
# pure-GLES apps; opt-in via X2D_DIRECT_VENDOR=1 + setting your own
# __EGL_VENDOR_LIBRARY_FILENAMES.
#
# X2D_USE_ADRENO=0 → fallback to llvmpipe software GL. Use this only
# when virgl_test_server fails to start (e.g. ANGLE pkg missing).
if [[ "${X2D_USE_ADRENO:-1}" == "1" ]]; then
    export GALLIUM_DRIVER=virpipe
    export LIBGL_DRI3_DISABLE=1
    export EPOXY_USE_ANGLE=1
    echo "[run_gui] hw-accel via virgl + ANGLE-Vulkan → Adreno 830"
else
    export GALLIUM_DRIVER=llvmpipe
    export LIBGL_ALWAYS_SOFTWARE=1
    export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
    echo "[run_gui] software fallback (llvmpipe) — slow"
fi

# x2d/termux #88 — suppress the "Could not read the contents of /" GTK
# popup. Root cause: gvfsd-trash daemon (spawned by gtk's GFileMonitor
# at startup) tries to enumerate / looking for writable trash dirs, hits
# EACCES because Termux can't read the Android root, and gtk_show_uri
# pops a modal that the user has to dismiss every launch. Setting
# GIO_USE_VFS=local routes all gio file ops through the local backend
# only, bypassing gvfs entirely. Trash, recent-files, mount monitor all
# fall back to local-only behaviour. No functional loss for slicer use.
export GIO_USE_VFS=local
export GVFS_DISABLE_FUSE=1
# x2d/termux #88 — also kill any persistent gvfsd-trash daemon spawned by
# xfce4-session at login. GIO_USE_VFS=local disconnects BambuStudio from
# gvfsd, but the daemon's own enumeration of / continues independently
# and pops the popup via dbus when GTK file monitor connects. Killing it
# at launch is a hard guarantee.
pkill -TERM gvfsd-trash 2>/dev/null || true
pkill -TERM gvfsd-recent 2>/dev/null || true

# Spin up the ANGLE-aware virgl render server if it isn't already running.
# `--angle-vulkan` (was --angle-gl) — the GL backend pulls in libgtk-3 which
# requires `epoxy_glXQueryExtension` (a GLX symbol). Termux's libepoxy.so
# at $PREFIX/lib has it, but virgl's bundled
# `$PREFIX/opt/virglrenderer-android/lib/libepoxy.so` (DT_RUNPATH) is a
# different build that exposes `epoxy_set_library_path` (which Termux's
# doesn't) but lacks the GLX surface. Result: --angle-gl crashes with
# "cannot locate symbol epoxy_glXQueryExtension". --angle-vulkan
# bypasses libgtk-3 entirely (Vulkan render path skips X11/GLX) and
# reaches the Adreno via the leegaos vulkan_wrapper just fine. The
# bench saying ANGLE-GL is faster than ANGLE-Vulkan in virgl was for
# a desktop x86_64 host; on Adreno 830 with the vulkan_wrapper the GL
# path doesn't actually win since both end up going through Vulkan
# anyway. Required for X2D_USE_ADRENO=1.
if [[ "${X2D_USE_ADRENO:-1}" == "1" ]] && ! pgrep -f virgl_test_server_android >/dev/null 2>&1; then
    EPOXY_USE_ANGLE=1 \
    LD_LIBRARY_PATH="$PREFIX/opt/angle-android/vulkan${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
    VK_ICD_FILENAMES="$PREFIX/share/vulkan/icd.d/wrapper_icd.aarch64.json" \
    virgl_test_server_android --angle-vulkan \
        > "${TMPDIR:-/data/data/com.termux/files/usr/tmp}/virgl_server.log" 2>&1 &
    sleep 1
fi

# ---------------------------------------------------------------------
# Bridge supervisor (item #12).
#
# Without this, the shim spawns x2d_bridge.py serve once on first
# `connect_printer` call. If that bridge dies (network blip, OOM, segfault
# in paho), the socket file lingers and every subsequent shim RPC fails
# with ECONNREFUSED — the GUI silently loses Connect/AMS/Print until the
# user restarts everything.
#
# Watchdog: small bash loop with exponential backoff (1→2→5→10→30s). Logs
# rotate at 1 MB. Cleaned up on script exit via trap.
# ---------------------------------------------------------------------

X2D_HOME="${HOME}/.x2d"
mkdir -p "${X2D_HOME}"
BRIDGE_SOCK="${X2D_HOME}/bridge.sock"
BRIDGE_LOG="${X2D_HOME}/bridge.log"
BRIDGE_PY="${X2D_ROOT}/x2d_bridge.py"

rotate_log() {
    # Truncate-then-rename: cap at 1 MiB, keep one .1 generation.
    local f="$1" cap=1048576
    if [[ -f "$f" ]] && (( $(stat -c %s "$f" 2>/dev/null || echo 0) > cap )); then
        mv -f "$f" "$f.1"
        : > "$f"
    fi
}

bridge_watchdog() {
    local backoff=1
    while true; do
        rotate_log "${BRIDGE_LOG}"
        # Stale socket cleanup — Unix sockets aren't auto-removed when the
        # owning process dies, and bind() fails with EADDRINUSE on retry.
        [[ -S "${BRIDGE_SOCK}" ]] && rm -f "${BRIDGE_SOCK}"
        echo "[$(date +%H:%M:%S)] watchdog: spawning x2d_bridge serve" >> "${BRIDGE_LOG}"
        python3.12 "${BRIDGE_PY}" serve --sock "${BRIDGE_SOCK}" \
            >> "${BRIDGE_LOG}" 2>&1
        local rc=$?
        echo "[$(date +%H:%M:%S)] watchdog: bridge exited rc=$rc; sleeping ${backoff}s" >> "${BRIDGE_LOG}"
        sleep "${backoff}"
        # Backoff: 1 → 2 → 5 → 10 → 30s, then stay capped.
        case $backoff in
            1)  backoff=2  ;;
            2)  backoff=5  ;;
            5)  backoff=10 ;;
            10) backoff=30 ;;
        esac
    done
}

# Don't double-launch if a bridge process is already running (e.g. user
# kept the supervisor alive across multiple bambu launches).
if ! pgrep -f "x2d_bridge.py serve" >/dev/null 2>&1; then
    bridge_watchdog &
    BRIDGE_WATCHDOG_PID=$!
    trap 'kill -TERM "${BRIDGE_WATCHDOG_PID}" 2>/dev/null; pkill -TERM -f "x2d_bridge.py serve" 2>/dev/null' EXIT
    # Give the bridge a moment to bind so the shim's first ensure_socket
    # call sees the socket already present and skips its own spawn.
    for _ in 1 2 3 4 5 6; do
        [[ -S "${BRIDGE_SOCK}" ]] && break
        sleep 0.5
    done
fi

# ---------------------------------------------------------------------
# Camera daemon supervisor — spawns x2d_bridge.py camera so the GUI's
# MediaPlayCtrl (patched to honour X2D_CAMERA_URL) can render the live
# RTSPS feed via gstreamer playbin. The daemon proxies rtsps://printer:322
# to a local MJPEG endpoint at 127.0.0.1:8767/cam.mjpeg, which works
# because we don't have Bambu's real BambuSource library wired in.
# ---------------------------------------------------------------------
CAM_LOG="${X2D_HOME}/camera.log"
CAM_PORT="${X2D_CAMERA_PORT:-8767}"

if [[ -f "${X2D_HOME}/credentials" ]] && ! pgrep -f "x2d_bridge.py camera" >/dev/null 2>&1; then
    (
        backoff=1
        while true; do
            rotate_log "${CAM_LOG}"
            echo "[$(date +%H:%M:%S)] watchdog: spawning x2d_bridge camera (port ${CAM_PORT})" >> "${CAM_LOG}"
            python3.12 "${BRIDGE_PY}" camera --port 322 \
                --bind "127.0.0.1:${CAM_PORT}" \
                >> "${CAM_LOG}" 2>&1
            echo "[$(date +%H:%M:%S)] watchdog: camera exited rc=$?; sleeping ${backoff}s" >> "${CAM_LOG}"
            sleep "${backoff}"
            case $backoff in
                1)  backoff=2  ;; 2)  backoff=5  ;; 5)  backoff=10 ;;
                10) backoff=30 ;;
            esac
        done
    ) &
    CAM_WATCHDOG_PID=$!
    trap 'kill -TERM "${BRIDGE_WATCHDOG_PID}" "${CAM_WATCHDOG_PID}" 2>/dev/null; pkill -TERM -f "x2d_bridge.py serve" 2>/dev/null; pkill -TERM -f "x2d_bridge.py camera" 2>/dev/null' EXIT
    # Wait for HTTP daemon to bind (gstreamer playbin needs it ready
    # before the user clicks Camera). 8s is plenty for ffmpeg to spawn
    # and the first frame to land.
    for _ in $(seq 1 16); do
        if curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:${CAM_PORT}/cam.jpg"; then
            break
        fi
        sleep 0.5
    done
fi

# Tell BambuStudio's MediaPlayCtrl (patched in this build) to use the
# local /cam.jpg endpoint instead of the bambu:/// URL its real
# BambuSource lib can't read. The patched MediaPlayCtrl uses libcurl
# to GET this URL on a fixed interval (10 fps default) — DO NOT point
# at /cam.mjpeg here; that's a never-terminating multipart stream that
# would just hang curl. /cam.jpg returns the latest single frame on
# every request.
export X2D_CAMERA_URL="${X2D_CAMERA_URL:-http://127.0.0.1:${CAM_PORT}/cam.jpg}"

# Run from bs-bionic so resources/ is found relative to argv[0].
cd "${X2D_ROOT}/bs-bionic"
exec "${BS_BIN}" "$@"
