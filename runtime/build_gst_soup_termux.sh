#!/data/data/com.termux/files/usr/bin/bash
# build_gst_soup_termux.sh — rebuild gst-plugins-good's soup plugin on
# Termux to fix the "0 features registered" bug that blocks every HTTP
# source in gstreamer (souphttpsrc, HLS, DASH, …).
#
# Background. The Termux-packaged libgstsoup.so 1.28.2 was built before
# libsoup-3.0 was a runtime dep on this device, so meson's auto-detect
# took the "no libsoup found, skip" branch in ext/soup/meson.build. The
# resulting plugin shell loads but exposes 0 elements. Symptom:
#   $ gst-inspect-1.0 souphttpsrc
#   No such element or plugin 'souphttpsrc'
# despite libgstsoup.so being present in the gstreamer plugin dir.
#
# Two fixes needed:
#   1. Rebuild the soup plugin from the same upstream source version
#      with -Dsoup-version=3 -Dsoup=enabled, so libsoup-3.0 is wired
#      in at compile time.
#   2. Symlink libsoup-3.0.so → libsoup-3.0.so.0. Termux ships the
#      unversioned name but gstsouploader.c hardcodes
#         #define LIBSOUP_3_SONAME "libsoup-3.0.so.0"
#      and dlopen()s that exact filename at module-init time. Without
#      the symlink the dlopen fails silently and the plugin still
#      registers 0 features even with the correct build.
#
# Companion to item #70 (libcurl JPEG poller). #70 bypasses gstreamer
# for the in-GUI camera; #71 fixes gstreamer itself so that anything
# else on this Termux that relies on souphttpsrc / HLS works too.
#
# Idempotent — safe to re-run after Termux package updates that may
# overwrite the symlink or revert the plugin.
set -euo pipefail

GST_VERSION="${GST_VERSION:-1.28.2}"
WORK="${TMPDIR:-$PREFIX/tmp}/gst-rebuild"
PLUGIN_DIR="$PREFIX/lib/gstreamer-1.0"
LIBSOUP_LINK="$PREFIX/lib/libsoup-3.0.so.0"
LIBSOUP_REAL="$PREFIX/lib/libsoup-3.0.so"
TARBALL_URL="https://gstreamer.freedesktop.org/src/gst-plugins-good/gst-plugins-good-${GST_VERSION}.tar.xz"

mkdir -p "$WORK"
cd "$WORK"

echo "[+] Termux gst-plugins-good = $(pacman -Qi gst-plugins-good 2>/dev/null | awk '/^Version/{print $3}')"
echo "[+] requested rebuild for     = ${GST_VERSION}"

# Step 0 — sanity. We need libsoup-3.0 to be installed.
if [[ ! -f "$LIBSOUP_REAL" ]]; then
    echo "ERROR: $LIBSOUP_REAL missing. Install with: yes | pkg install libsoup3"
    exit 1
fi

# Step 1 — symlink so the dlopen finds it under the SONAME the plugin
# loader hardcodes. This MUST happen first; otherwise even our rebuilt
# plugin will register 0 features.
if [[ ! -L "$LIBSOUP_LINK" ]]; then
    echo "[+] symlinking $LIBSOUP_LINK -> libsoup-3.0.so"
    ln -sf libsoup-3.0.so "$LIBSOUP_LINK"
fi

# Step 2 — fetch source. Skip if extracted tree already exists.
SRC="gst-plugins-good-${GST_VERSION}"
if [[ ! -d "$SRC" ]]; then
    if [[ ! -f "${SRC}.tar.xz" ]]; then
        echo "[+] downloading ${TARBALL_URL}"
        curl -sLO "$TARBALL_URL"
    fi
    tar -xf "${SRC}.tar.xz"
fi

# Step 3 — meson configure with soup-only enabled + auto-features off.
# Disabling auto-features means meson won't try to build every other
# plugin in the tree (most need deps Termux doesn't have, e.g. libvpx
# at .so.12 vs Termux .so.13).
cd "$SRC"
if [[ ! -f _build/build.ninja ]]; then
    echo "[+] meson setup (this takes ~30s)"
    rm -rf _build
    meson setup _build \
        -Dsoup=enabled -Dsoup-version=3 -Dsoup-lookup-dep=true \
        -Dauto_features=disabled \
        -Dprefix="$PREFIX" -Dlibdir=lib
fi

# Step 4 — build only the soup plugin.
echo "[+] ninja build ext/soup/libgstsoup.so"
ninja -C _build ext/soup/libgstsoup.so

# Step 5 — install with backup.
SO_NEW="_build/ext/soup/libgstsoup.so"
SO_DST="$PLUGIN_DIR/libgstsoup.so"
if [[ -f "$SO_DST" && ! -f "${SO_DST}.bak" ]]; then
    cp "$SO_DST" "${SO_DST}.bak"
    echo "[+] backed up existing $SO_DST -> ${SO_DST}.bak"
fi
cp -f "$SO_NEW" "$SO_DST"
echo "[+] installed: $(stat -c %s $SO_DST) bytes ($(stat -c %s ${SO_DST}.bak) before)"

# Step 6 — invalidate gstreamer's plugin registry cache so the new
# plugin gets re-scanned on next gst-launch / playbin invocation.
rm -f "${HOME}/.cache/gstreamer-1.0/registry.aarch64.bin"

# Step 7 — verify. souphttpsrc must be discoverable AND the plugin must
# advertise > 0 features.
echo "[+] verifying souphttpsrc is registered"
if gst-inspect-1.0 souphttpsrc 2>&1 | grep -q '^Factory Details'; then
    echo "    OK — souphttpsrc loaded"
else
    echo "ERROR — souphttpsrc still missing after rebuild + symlink"
    gst-inspect-1.0 souphttpsrc 2>&1 | head -5
    exit 1
fi

# Step 8 — pipeline smoke test against the local camera daemon if up.
TEST_URL="${X2D_TEST_URL:-http://127.0.0.1:8767/cam.jpg}"
if curl -sf -o /dev/null --max-time 2 "$TEST_URL" 2>/dev/null; then
    echo "[+] smoke test: souphttpsrc -> jpegdec -> fakesink against $TEST_URL"
    timeout 5 gst-launch-1.0 -q \
        souphttpsrc location="$TEST_URL" ! \
        jpegdec ! fakesink num-buffers=1 -v 2>&1 | \
        grep -E 'caps|state-changed.*paused' | head -3
fi

echo
echo "[+] done. souphttpsrc, HLS, DASH and any other libsoup-using"
echo "    gstreamer plugin should now work on this Termux install."
echo "    To roll back: cp ${SO_DST}.bak ${SO_DST} && rm -f $LIBSOUP_LINK"
