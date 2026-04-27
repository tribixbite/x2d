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

# Termux source patch (item #27): gvfs probes "/" by default and pops a
# "Could not read the contents of /" error every launch because Android
# blocks app-process root reads. Pinning XDG + gvfs to $HOME stops the
# probe before it starts.
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
export GIO_USE_VFS=local
export GVFS_DISABLE_FUSE=1
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
unset EPOXY_USE_ANGLE MESA_GL_VERSION_OVERRIDE MESA_GLES_VERSION_OVERRIDE \
      MESA_GLSL_VERSION_OVERRIDE LIBGL_DRI3_DISABLE

# Force Mesa llvmpipe (software GL) instead of zink (Vulkan→GL).
# zink_kopper.c:720 asserts on swapchain acquire because termux-x11 has no
# DRI3/Present, so kopper can't allocate presentable images. Triggers as soon
# as wxGLCanvas actually renders (Prepare tab, embedded WebView dialogs, etc.).
# llvmpipe renders to an offscreen surface and blits via XPutImage, which
# termux-x11 supports.
export GALLIUM_DRIVER=llvmpipe
export LIBGL_ALWAYS_SOFTWARE=1
export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
# wx 3.3 GLCanvas needs EGL. We previously set surfaceless which allocates
# offscreen-only render targets — those rendered fine but never got blitted
# to the X window, so the 3D viewport showed blank white. Switching to the
# native x11 EGL platform (item #25) lets Mesa swrast use XPutImage to
# present the rendered surface to the X window. termux-x11 supports
# XPutImage; that's the same path standard wxFrame painting uses.
export EGL_PLATFORM=x11

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
          pkill -TERM -f "x2d_bridge.py serve" 2>/dev/null' EXIT
    # Wait up to 3s for the bridge to bind the socket so the shim's
    # first ensure_socket() finds it ready and skips its own spawn.
    for _ in 1 2 3 4 5 6; do
        [[ -S "${BRIDGE_SOCK}" ]] && break
        sleep 0.5
    done
fi

# Termux source patch (item #26): change cwd to $HOME before exec so
# wxFileDialog (Ctrl+O / Save Project / Import STL) defaults to the
# user's home dir instead of "/" (which triggers the gvfs permission
# popup). resources_dir() uses install-prefix-relative paths derived
# from argv[0], not cwd, so this doesn't break resource discovery.
cd "${HOME}"
exec "${BS_BIN}" "$@"
