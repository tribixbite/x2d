#!/usr/bin/env bash
# build_egl_vendor.sh — build libEGL_x2dadreno.so (item #95).
#
# This produces the GLVND EGL vendor that sits between libglvnd's
# dispatch layer and ANGLE-Vulkan, routing wxGLCanvas's EGL calls
# through ANGLE → libvulkan_wrapper.so → Adreno 830 hardware on the
# Galaxy S25 Ultra.
#
# Run after editing libEGL_x2dadreno.c — this script is idempotent.
# Output: $RUNTIME_DIR/libEGL_x2dadreno.so
#
# Install (separately, see dist/.../install.sh): the .so goes to
# $PREFIX/lib/ and the vendor JSON to $PREFIX/share/glvnd/egl_vendor.d/.

set -eu
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${RUNTIME_DIR}/libEGL_x2dadreno.c"
OUT="${RUNTIME_DIR}/libEGL_x2dadreno.so"

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: missing source $SRC" >&2
    exit 1
fi

CC="${CC:-clang}"
echo "[build_egl_vendor] $CC -> $OUT"
$CC -shared -fPIC -O2 -fvisibility=hidden \
    -o "$OUT" "$SRC" \
    -ldl -lpthread -lX11 -Wl,--export-dynamic
echo "[build_egl_vendor] OK ($(stat -c %s "$OUT") bytes)"
nm -D "$OUT" | grep -q '__egl_Main' || {
    echo "ERROR: __egl_Main not exported — vendor will not be picked up by libglvnd" >&2
    exit 2
}
echo "[build_egl_vendor] __egl_Main exported"
