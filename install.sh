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
#      them.  (The wizard-skip is now a source patch in
#      patches/GUI_App.cpp.termux.patch, baked into the shipped binary —
#      no runtime patcher needed; see item #21/#34.)
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

# Optional: install the WebRTC stack so `x2d_bridge.py webrtc` works.
# Skip silently if deps fail to build — the rest of the toolkit still
# functions without WebRTC. On Termux this needs libsrtp built from
# source; see docs/WEBRTC.md.
if ! python3 -c 'import aiortc, av, aiohttp' 2>/dev/null; then
    c_blue "(optional) pip install aiortc/av/aiohttp for WebRTC streaming…"
    if pip install --no-build-isolation --no-deps \
            'aiortc==1.10.1' 'av==13.1.0' 'aiohttp' \
            'pyee' 'aioice' 'pylibsrtp<1.0' 'google-crc32c' \
            'pyOpenSSL' 'ifaddr' 2>&1 | tail -3; then
        if python3 -c 'import aiortc, av, aiohttp' 2>/dev/null; then
            c_green "WebRTC stack installed"
        else
            c_blue "WebRTC stack partially installed; non-fatal"
        fi
    else
        c_blue "WebRTC stack not installed (non-fatal); see docs/WEBRTC.md"
    fi
else
    c_green "aiortc / av / aiohttp already importable"
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
         "$INSTALL_ROOT/run_gui.sh" 2>/dev/null || true

# ---------------------------------------------------------------------------
# GLVND EGL vendor (item #95) — installs libEGL_x2dadreno.so + JSON so
# X2D_USE_ADRENO=1 routes wxGLCanvas through ANGLE-Vulkan → Adreno 830 hw.
# ---------------------------------------------------------------------------

section "installing GLVND EGL vendor (Adreno hw-accel)"

VENDOR_SO_SRC="$INSTALL_ROOT/runtime/libEGL_x2dadreno.so"
VENDOR_SO_DST="$PREFIX/lib/libEGL_x2dadreno.so"
VENDOR_JSON_DIR="$PREFIX/share/glvnd/egl_vendor.d"
VENDOR_JSON="$VENDOR_JSON_DIR/40_x2dadreno.json"

if [[ -f "$VENDOR_SO_SRC" ]]; then
    cp -f "$VENDOR_SO_SRC" "$VENDOR_SO_DST" \
        && c_green "libEGL_x2dadreno.so → $VENDOR_SO_DST"
    mkdir -p "$VENDOR_JSON_DIR"
    cat > "$VENDOR_JSON" <<JSON
{
    "file_format_version" : "1.0.0",
    "ICD" : {
        "library_path" : "$VENDOR_SO_DST"
    }
}
JSON
    c_green "vendor JSON → $VENDOR_JSON"
    c_blue  "  Use \`X2D_USE_ADRENO=1 ./run_gui.sh\` to switch wxGLCanvas onto"
    c_blue  "  the direct ANGLE-Vulkan path (default still virgl for compat)."
else
    c_yellow "no libEGL_x2dadreno.so in tarball — Adreno hw path unavailable, falling back to virgl"
fi

# ---------------------------------------------------------------------------
# Plug-ins (item #34: binary wizard-patch removed — config_wizard_startup
# is now source-patched in patches/GUI_App.cpp.termux.patch and baked
# into the shipped binary)
# ---------------------------------------------------------------------------

section "installing plug-ins"

mkdir -p "$PLUGINS_DIR" || fatal "can't mkdir $PLUGINS_DIR" 3
cp -f "$INSTALL_ROOT/plugins/libbambu_networking.so" "$PLUGINS_DIR/" 2>/dev/null \
    && c_green "libbambu_networking.so → $PLUGINS_DIR" \
    || c_yellow "no libbambu_networking.so in tarball (older release?)"
cp -f "$INSTALL_ROOT/plugins/libBambuSource.so"      "$PLUGINS_DIR/" 2>/dev/null \
    && c_green "libBambuSource.so → $PLUGINS_DIR" \
    || true

# ---------------------------------------------------------------------------
# Vendor profile seed (so the Device tab works on first run)
#
# Without a Bambu vendor preset selected, MainFrame.cpp falls back to
# `web/device/missing_connection.html` — the SSDP-discovered X2D never
# reaches the agent-driven MonitorPanel. The wizard normally copies these
# profiles from `resources/profiles/` into `<data_dir>/system/`, but we
# skip the wizard. Mirror the copy here so a fresh install lands ready.
#
# We use Bambu Lab X2D as the default — that's what this toolkit targets,
# and the upstream BBL profile catalogue ships full X2D variants
# (0.2/0.4/0.6/0.8 nozzles, X2D-specific filaments, 0.20mm Standard @BBL X2D
# process). On a fresh install the GUI lands directly on the right model
# without the user having to pick.
# ---------------------------------------------------------------------------

section "pre-seeding Bambu vendor profiles"

SYSTEM_DIR="$CONFIG_DIR/system"
mkdir -p "$SYSTEM_DIR" || fatal "can't mkdir $SYSTEM_DIR" 3
SRC_PROFILES="$INSTALL_ROOT/resources/profiles"
if [ -f "$SRC_PROFILES/BBL.json" ] && [ -d "$SRC_PROFILES/BBL" ]; then
    cp -f  "$SRC_PROFILES/BBL.json" "$SYSTEM_DIR/"
    cp -rf "$SRC_PROFILES/BBL"      "$SYSTEM_DIR/"
    c_green "BBL vendor profile installed → $SYSTEM_DIR/"
else
    c_yellow "no BBL profiles in $SRC_PROFILES — Device tab will show missing_connection.html"
    c_yellow "until you pick a Bambu printer in the in-app preset switcher."
fi

# ---------------------------------------------------------------------------
# AppConfig pre-seed (idempotent — won't clobber an existing one)
# ---------------------------------------------------------------------------

section "pre-seeding AppConfig"

mkdir -p "$CONFIG_DIR" || fatal "can't mkdir $CONFIG_DIR" 3
APPCONF="$CONFIG_DIR/BambuStudio.conf"

# Always merge — won't overwrite user prefs but WILL ensure the BBL
# vendor / model / preset keys exist. Previous behaviour ("skip if
# present") left existing AppConfigs without these keys, which made
# the Device tab fall back to missing_connection.html.
python3 - "$APPCONF" <<'PY'
import json, sys, os
from pathlib import Path

path = Path(sys.argv[1])
data = {}
if path.exists() and path.stat().st_size > 0:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        # Don't clobber a user-edited file we can't parse — back it up.
        bak = path.with_suffix(path.suffix + ".bak-x2d")
        path.replace(bak)
        print(f"[x2d] could not parse {path}, backed up to {bak}")
        data = {}

# Defaults — only fill missing keys.
defaults_app = {
    "language":   "en_US",
    "region":     "Others",
    "first_run":  "false",
    "user_mode":  "advanced",
    "show_splash":"false",
}
defaults_firstguide = {"finish": "1", "privacyuse": "true"}

app = data.setdefault("app", {})
for k, v in defaults_app.items():
    app.setdefault(k, v)
fg = data.setdefault("firstguide", {})
for k, v in defaults_firstguide.items():
    fg.setdefault(k, v)

# Vendor / model / presets — install ALWAYS sets these (they're the gate).
data.setdefault("vendors", {})["BBL"] = "1"
existing_models = data.get("models") or []
has_bbl_model = any(m.get("vendor") == "BBL" for m in existing_models)
if not has_bbl_model:
    existing_models.append({
        "vendor":          "BBL",
        "model":           "Bambu Lab X2D",
        "nozzle_diameter": '"0.4"',
    })
    data["models"] = existing_models

presets = data.setdefault("presets", {})
presets.setdefault("printer",  "Bambu Lab X2D 0.4 nozzle")
presets.setdefault("filament", "Bambu PLA Basic @BBL X2D")
presets.setdefault("print",    "0.20mm Standard @BBL X2D")
if not isinstance(presets.get("filaments"), list) or not presets["filaments"]:
    presets["filaments"] = ["Bambu PLA Basic @BBL X2D"]

path.write_text(json.dumps(data, indent=4))
os.chmod(path, 0o644)
print(f"[x2d] merged BBL vendor/model/presets into {path}")
PY
c_green "$APPCONF merged (BBL vendor/model/presets ensured)"

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
