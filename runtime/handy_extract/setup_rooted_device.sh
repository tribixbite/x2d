#!/usr/bin/env bash
# setup_rooted_device.sh — bootstrap a rooted Android device for Frida-hooking
# Bambu Handy v3.19.0 to recover the per-installation X.509 cert + RSA private
# key the app uses to sign LAN MQTT publishes against an X2D / X1C / P1S /
# A1 / H2D printer.
#
# Prereqs on the host (this Termux phone is fine):
#   - adb installed and the rooted device is connected via USB or WiFi adb.
#   - Frida-tools installed: `pip install frida-tools` (any host with python ≥3.9).
#   - The Bambu Handy APK bundle at ~/bbl.intl.bambulab.com/0/ (already present).
#
# Prereqs on the rooted device:
#   - Magisk (or any working `su` in PATH).
#   - USB debugging on, host fingerprint approved.
#   - WiFi connected to the same LAN as the X2D so the app can talk to the
#     printer normally.
#
# Run this once per device. It is idempotent.
set -euo pipefail

DEVICE="${ADB_SERIAL:-}"
ADB="adb${DEVICE:+ -s $DEVICE}"

step()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
fatal() { printf '\033[1;31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------- 1. ADB
step "1/7 — verify adb connectivity"
$ADB get-state >/dev/null 2>&1 || fatal "no adb device. Plug in / 'adb connect <ip:port>' first."
DEV_ID=$($ADB get-serialno)
echo "  device: $DEV_ID"

# ------------------------------------------------------------------- 2. arch
step "2/7 — detect device CPU architecture"
ABI=$($ADB shell getprop ro.product.cpu.abi | tr -d '\r')
case "$ABI" in
  arm64-v8a)   FRIDA_ARCH=android-arm64 ;;
  armeabi-v7a) FRIDA_ARCH=android-arm ;;
  x86_64)      FRIDA_ARCH=android-x86_64 ;;
  x86)         FRIDA_ARCH=android-x86 ;;
  *)           fatal "unsupported ABI: $ABI" ;;
esac
echo "  ABI: $ABI -> frida arch: $FRIDA_ARCH"

# ------------------------------------------------------------------- 3. su
step "3/7 — verify root"
if ! $ADB shell 'su -c id' 2>/dev/null | grep -q 'uid=0'; then
  fatal "device is not rooted (su -c id did not return uid=0). Install Magisk first."
fi
echo "  root: ok (uid=0 via su)"

# ------------------------------------------------------------------- 4. frida-server
# We use the StrongR anti-detect fork because Bambu Handy is wrapped by what
# appears to be Promon SHIELD ≥7.0 + Tencent Tinker hot-patch, both of which
# scan for vanilla frida-server's process name + symbol fingerprints.
step "4/7 — install StrongR-Frida server (anti-detect)"
FRIDA_VER="${FRIDA_VER:-16.5.6}"   # bump if hzzheyang releases a newer build
FRIDA_BIN="hluda-server-${FRIDA_VER}-${FRIDA_ARCH}"
FRIDA_URL="https://github.com/hzzheyang/strongR-frida-android/releases/download/${FRIDA_VER}/${FRIDA_BIN}.xz"
LOCAL_DIR="$(dirname "$(realpath "$0")")/cache"
mkdir -p "$LOCAL_DIR"
LOCAL_XZ="$LOCAL_DIR/${FRIDA_BIN}.xz"
LOCAL_BIN="$LOCAL_DIR/${FRIDA_BIN}"
if [ ! -f "$LOCAL_BIN" ]; then
  echo "  downloading $FRIDA_URL"
  curl -fsSL "$FRIDA_URL" -o "$LOCAL_XZ" || fatal "download failed — check FRIDA_VER (try a newer release)"
  xz -d -f "$LOCAL_XZ"
fi
chmod +x "$LOCAL_BIN"

DEV_FRIDA="/data/local/tmp/frida-server"  # canonical name – StrongR also patches the on-disk filename check
echo "  pushing to $DEV_FRIDA"
$ADB push "$LOCAL_BIN" "$DEV_FRIDA" >/dev/null
$ADB shell "su -c 'chmod 755 $DEV_FRIDA && chown root:root $DEV_FRIDA'"

# ------------------------------------------------------------------- 5. start
step "5/7 — (re)start frida-server"
$ADB shell "su -c 'pkill -9 -f frida-server' 2>/dev/null" || true
$ADB shell "su -c 'nohup $DEV_FRIDA -l 0.0.0.0:27042 >/data/local/tmp/frida.log 2>&1 &'" >/dev/null
sleep 1
if ! $ADB shell "su -c 'ss -ntlp 2>/dev/null | grep 27042 || netstat -ntlp 2>/dev/null | grep 27042'" | grep -q frida; then
  echo "  warning: 27042 listener not visible; check /data/local/tmp/frida.log"
else
  echo "  frida-server listening on :27042 (forwarded via adb)"
fi
$ADB forward tcp:27042 tcp:27042 >/dev/null

# ------------------------------------------------------------------- 6. install Bambu Handy
step "6/7 — install Bambu Handy v3.19.0"
PKG="bbl.intl.bambulab.com"
HANDY_DIR="${HOME}/bbl.intl.bambulab.com/0"
HANDY_TAR="${HANDY_DIR}/source.tar.gz.0"
if $ADB shell "pm list packages $PKG" | grep -q "$PKG"; then
  echo "  $PKG already installed — skipping"
else
  if [ ! -f "$HANDY_TAR" ]; then
    fatal "Bambu Handy backup not found at $HANDY_TAR"
  fi
  TMP=$(mktemp -d)
  trap "rm -rf $TMP" EXIT
  echo "  extracting App-Manager backup"
  tar -xzf "$HANDY_TAR" -C "$TMP"
  echo "  installing split APKs (base + arm64 + xxhdpi + en)"
  $ADB install-multiple \
      "$TMP/base.apk" \
      "$TMP/split_config.arm64_v8a.apk" \
      "$TMP/split_config.xxhdpi.apk" \
      "$TMP/split_config.en.apk" \
    >/dev/null
fi

# ------------------------------------------------------------------- 7. host-side frida-tools
step "7/7 — verify host frida-tools"
if ! command -v frida >/dev/null; then
  echo "  installing frida-tools via pip"
  pip install --user frida-tools >/dev/null
fi
HOST_FRIDA_VER=$(frida --version 2>&1 || true)
echo "  host frida: $HOST_FRIDA_VER"
echo "  device frida: $($ADB shell "$DEV_FRIDA --version" 2>/dev/null | tr -d '\r')"

cat <<EOF

\033[1;32mSetup done.\033[0m

Next:
  1. Launch Bambu Handy on the device, log in, wait for it to find the printer.
  2. From this host, run the trace:
       cd $(dirname "$(realpath "$0")")
       python3 dump_keys.py
  3. While the trace is running, in Bambu Handy:
       - Tap your printer in the device list
       - Try any control that touches LAN-MQTT (pause, resume, light toggle).
     The hook fires on every RSA sign / AES decrypt the app does.
  4. The recovered cert+key land at:
       \$XDG_DATA_HOME/x2d/handy_dump/<timestamp>/{key.pem,cert.pem,trace.log}
  5. Copy them into our bridge:
       cp ~/.local/share/x2d/handy_dump/<ts>/key.pem  ~/.x2d/bambu_app.key
       cp ~/.local/share/x2d/handy_dump/<ts>/cert.pem ~/.x2d/bambu_app.crt
EOF
