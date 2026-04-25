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
# wx 3.3 GLCanvas needs EGL; surfaceless lets Mesa allocate offscreen render
# targets without GLX (which termux-x11 doesn't expose either).
export EGL_PLATFORM=surfaceless

cd "$(dirname "${BS_BIN}")"
exec "${BS_BIN}" "$@"
