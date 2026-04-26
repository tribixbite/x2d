#!/usr/bin/env bash
# install.sh — one-shot installer for x2d on aarch64 Termux.
#
# Run via:
#   bash <(curl -fsSL https://raw.githubusercontent.com/tribixbite/x2d/main/install.sh)
#
# What it does (idempotent — safe to re-run for upgrades):
#   1. Verify we're on Termux + aarch64 + termux-x11 reachable.
#   2. `pkg install` every runtime dependency (idempotent).
#   3. `pip install paho-mqtt` (idempotent).
#   4. Download the latest tarball from
#      github.com/tribixbite/x2d/releases/latest, verify SHA-256 against
#      the released .sha256 sibling, and unpack into INSTALL_ROOT
#      (default ~/x2d).
#   5. Drop the libbambu_networking.so + libBambuSource.so plug-ins at
#      ~/.config/BambuStudioInternal/plugins/ where bambu-studio expects
#      them, and apply the wizard-skip binary patch to the new binary.
#   6. Pre-seed ~/.config/BambuStudioInternal/BambuStudio.conf with the
#      X2D printer model so the dropdown isn't empty.
#   7. Drop a ~/.x2d/credentials skeleton (mode 600) for the user to
#      fill in.
#   8. Optionally drop a ~/.termux/boot/x2d-bridge launcher so the
#      bridge daemon comes back after a phone reboot (skipped if
#      ~/.termux/boot/ doesn't exist — that requires the Termux:Boot
#      Android app).
#
# Exit codes:
#   0  success
#   1  unsupported platform / arch / missing termux-x11
#   2  network failure (release fetch, sha mismatch, pkg install)
#   3  filesystem failure (can't write target dirs)

set -eu

REPO=tribixbite/x2d
INSTALL_ROOT=${INSTALL_ROOT:-$HOME/x2d}
CONFIG_DIR=$HOME/.config/BambuStudioInternal
PLUGINS_DIR=$CONFIG_DIR/plugins
CREDS_DIR=$HOME/.x2d
BOOT_DIR=$HOME/.termux/boot
TARBALL=bambustudio-x2d-termux-aarch64.tar.xz

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_blue()  { printf '\033[34m%s\033[0m\n' "$*"; }
section() { printf '\n\033[1;36m==== %s ====\033[0m\n' "$*"; }
fatal()   { c_red "FATAL: $*"; exit "${2:-1}"; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fatal "missing required command: $1"
}

# ---------------------------------------------------------------------------
# Platform check
# ---------------------------------------------------------------------------

section "platform check"

case "${PREFIX:-}" in
    /data/data/com.termux/files/usr) ;;
    *) fatal "PREFIX is '${PREFIX:-unset}' — this script only runs on Termux. \
On other systems install from source per README.md." 1 ;;
esac

uname_m=$(uname -m)
case "$uname_m" in
    aarch64) ;;
    *) fatal "uname -m says '$uname_m' — only aarch64 is supported." 1 ;;
esac

c_green "Termux aarch64 detected"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

section "installing runtime dependencies"

# x11-repo carries wxwidgets / gtk3 / etc.
if ! pkg list-installed 2>/dev/null | grep -q '^x11-repo/'; then
    c_blue "enabling x11-repo…"
    pkg install -y x11-repo || fatal "pkg install x11-repo failed" 2
fi

PKG_LIST=(
    wxwidgets gtk3 webkit2gtk-4.1
    glew glfw mesa libllvm llvm
    glib pango cairo gdk-pixbuf atk
    fontconfig freetype libpng libjpeg libtiff
    openssl curl libcurl
    opencv libdbus libwebp
    libavcodec libswscale libavutil ffmpeg
    python python-cryptography xdotool
    openbox
)
# pkg install is idempotent; running it always is fine but slow if all are
# present, so check the cheap way first.
to_install=()
for pkg in "${PKG_LIST[@]}"; do
    pkg list-installed "$pkg" 2>/dev/null | grep -q . || to_install+=("$pkg")
done
if [ "${#to_install[@]}" -gt 0 ]; then
    c_blue "installing: ${to_install[*]}"
    pkg install -y "${to_install[@]}" || fatal "pkg install failed: ${to_install[*]}" 2
else
    c_green "all $((${#PKG_LIST[@]})) deps already installed"
fi

if ! python3 -c 'import paho.mqtt.client' 2>/dev/null; then
    c_blue "pip install paho-mqtt…"
    pip install --upgrade paho-mqtt || fatal "pip install paho-mqtt failed" 2
else
    c_green "paho-mqtt already importable"
fi

# ---------------------------------------------------------------------------
# Tarball fetch + verify + unpack
# ---------------------------------------------------------------------------

section "fetching release"

require_cmd curl
require_cmd sha256sum
require_cmd tar

mkdir -p "$INSTALL_ROOT" || fatal "can't mkdir $INSTALL_ROOT" 3

REL_BASE="https://github.com/${REPO}/releases/latest/download"
TMP=$(mktemp -d) || fatal "mktemp failed" 3
trap 'rm -rf "$TMP"' EXIT

c_blue "downloading $REL_BASE/$TARBALL …"
curl -fsSL -o "$TMP/$TARBALL"        "$REL_BASE/$TARBALL"        || fatal "tarball download failed" 2
curl -fsSL -o "$TMP/$TARBALL.sha256" "$REL_BASE/$TARBALL.sha256" || fatal "sha256 download failed" 2

c_blue "verifying SHA-256…"
expected=$(awk '{print $1}' "$TMP/$TARBALL.sha256")
actual=$(sha256sum "$TMP/$TARBALL" | awk '{print $1}')
if [ "$expected" != "$actual" ]; then
    c_red "expected: $expected"
    c_red "actual:   $actual"
    fatal "SHA-256 mismatch — DO NOT use this tarball" 2
fi
c_green "SHA-256 OK"

c_blue "unpacking into $INSTALL_ROOT …"
tar -xJf "$TMP/$TARBALL" -C "$INSTALL_ROOT" --strip-components=1 \
    || fatal "tar extract failed" 3
chmod +x "$INSTALL_ROOT/bin/bambu-studio" \
         "$INSTALL_ROOT/run_gui.sh" \
         "$INSTALL_ROOT/patch_bambu_skip_wizard.py" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Plug-ins + binary patch
# ---------------------------------------------------------------------------

section "installing plug-ins"

mkdir -p "$PLUGINS_DIR" || fatal "can't mkdir $PLUGINS_DIR" 3
cp -f "$INSTALL_ROOT/plugins/libbambu_networking.so" "$PLUGINS_DIR/" 2>/dev/null \
    && c_green "libbambu_networking.so → $PLUGINS_DIR" \
    || c_yellow "no libbambu_networking.so in tarball (older release?)"
cp -f "$INSTALL_ROOT/plugins/libBambuSource.so"      "$PLUGINS_DIR/" 2>/dev/null \
    && c_green "libBambuSource.so → $PLUGINS_DIR" \
    || true

if [ -x "$INSTALL_ROOT/patch_bambu_skip_wizard.py" ]; then
    c_blue "applying wizard-skip binary patch…"
    python3 "$INSTALL_ROOT/patch_bambu_skip_wizard.py" \
        "$INSTALL_ROOT/bin/bambu-studio" \
        || c_yellow "wizard-skip patch reported a mismatch — binary may be a different build; see runtime/network_shim/PROTOCOL.md for the manual offset-finding recipe"
fi

# ---------------------------------------------------------------------------
# AppConfig pre-seed (idempotent — won't clobber an existing one)
# ---------------------------------------------------------------------------

section "pre-seeding AppConfig"

mkdir -p "$CONFIG_DIR" || fatal "can't mkdir $CONFIG_DIR" 3
APPCONF="$CONFIG_DIR/BambuStudio.conf"
if [ -s "$APPCONF" ]; then
    c_yellow "$APPCONF already present — leaving it alone"
else
    cat > "$APPCONF" <<'JSON'
{
    "version": "02.06.00.51",
    "app": {
        "language": "en_US",
        "region": "Others",
        "first_run": "false",
        "user_mode": "advanced",
        "show_splash": "false"
    },
    "firstguide": {
        "finish": "1",
        "privacyuse": "true"
    },
    "models": [
        {
            "vendor": "BBL",
            "model": "Bambu Lab X2D",
            "nozzle_diameter": "\"0.4\""
        }
    ],
    "presets": {
        "filaments": ["Bambu PLA Silk @BBL X2D 0.4 nozzle"],
        "filament": "Bambu PLA Silk @BBL X2D 0.4 nozzle",
        "print": "0.20mm Standard @BBL X2D",
        "printer": "Bambu Lab X2D 0.4 nozzle"
    }
}
JSON
    c_green "wrote $APPCONF"
fi

# ---------------------------------------------------------------------------
# ~/.x2d/credentials skeleton
# ---------------------------------------------------------------------------

section "credentials skeleton"

mkdir -p "$CREDS_DIR" && chmod 700 "$CREDS_DIR"
CREDS="$CREDS_DIR/credentials"
if [ -s "$CREDS" ]; then
    c_yellow "$CREDS already present — leaving it alone"
else
    cat > "$CREDS" <<'INI'
# Fill in your printer's LAN IP, 8-char access code (Settings → Network
# → Access Code on the printer screen), and serial number (printed on
# the device sticker / Settings → About).
[printer]
ip =
code =
serial =
INI
    chmod 600 "$CREDS"
    c_green "wrote $CREDS (chmod 600) — fill in ip/code/serial before using the bridge"
fi

# ---------------------------------------------------------------------------
# Optional: Termux:Boot launcher for the bridge daemon
# ---------------------------------------------------------------------------

section "Termux:Boot autostart"

if [ -d "$BOOT_DIR" ]; then
    cat > "$BOOT_DIR/x2d-bridge" <<EOF
#!/usr/bin/env bash
# Auto-spawned by Termux:Boot on phone power-on. Starts the x2d
# bridge daemon so the GUI shim can immediately reach the printer.
exec python3 "$INSTALL_ROOT/helpers/x2d_bridge.py" daemon \\
    --interval 5 --http 127.0.0.1:8765 --quiet
EOF
    chmod +x "$BOOT_DIR/x2d-bridge"
    c_green "$BOOT_DIR/x2d-bridge installed"
else
    c_yellow "$BOOT_DIR doesn't exist — install the Termux:Boot Android app"
    c_yellow "if you want the bridge to survive phone reboots."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

section "ready"

cat <<EOF
Installation complete.

Next steps:
  1. Fill in $CREDS with your printer's IP / access code / serial.
  2. Start the X server: in the termux-x11 Android app tap "Open in
     full-screen", then in this Termux session run:
          termux-x11 :1
  3. Launch the GUI:
          $INSTALL_ROOT/run_gui.sh
  4. Test the bridge from CLI without the GUI:
          python3 $INSTALL_ROOT/helpers/x2d_bridge.py status

Re-run this installer any time to upgrade to the newest release —
your AppConfig and credentials file are preserved.
EOF
