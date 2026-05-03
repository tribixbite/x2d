#!/data/data/com.termux/files/usr/bin/bash
# Build the X2D sign-helper APK on Termux/aarch64.
# No external deps — uses only android.jar's KeyStore + Signature APIs.
# Pairs with patch_handy_shareduid.sh (Bambu re-patch with matching
# sharedUserId="bbl.shared") so AndroidKeyStore is shared between UIDs.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

ANDROID_HOME=${ANDROID_HOME:-$HOME/android-sdk}
PLATFORM_JAR="$ANDROID_HOME/platforms/android-34/android.jar"
BT_DIR="$ANDROID_HOME/build-tools/34.0.0-arm64"
[[ ! -x "$BT_DIR/d8" ]] && BT_DIR="$ANDROID_HOME/build-tools/34.0.0"

[[ -f "$PLATFORM_JAR" ]] || { echo "missing $PLATFORM_JAR"; exit 1; }
[[ -x "$BT_DIR/d8" ]] || { echo "missing d8 in $BT_DIR"; exit 1; }
KS_FILE="${TMPDIR:-$PREFIX/tmp}/handy_apk_patch/handy.keystore"
[[ -f "$KS_FILE" ]] || { echo "missing $KS_FILE — run patch_handy_debuggable.sh first"; exit 1; }

mkdir -p build/classes
echo "[+] javac"
javac -source 1.8 -target 1.8 -bootclasspath "$PLATFORM_JAR" \
      -d build/classes $(find src -name '*.java')

echo "[+] d8 → DEX"
"$BT_DIR/d8" --output build/ --lib "$PLATFORM_JAR" \
    $(find build/classes -name '*.class')

echo "[+] aapt2 link"
aapt2 link --manifest AndroidManifest.xml \
    -I "$PLATFORM_JAR" \
    -o build/sign-helper-unsigned.apk

echo "[+] inject DEX"
( cd build && zip -j sign-helper-unsigned.apk classes.dex )

echo "[+] zipalign + apksigner"
zipalign -p -f 4 build/sign-helper-unsigned.apk build/sign-helper-aligned.apk
apksigner sign --ks "$KS_FILE" --ks-pass pass:android \
    --ks-key-alias handy --key-pass pass:android \
    --v1-signing-enabled false --v2-signing-enabled true --v3-signing-enabled true \
    --out build/sign-helper.apk build/sign-helper-aligned.apk

ls -la build/sign-helper.apk
echo
echo "[+] usage:"
echo "    1. ensure Bambu re-patched with sharedUserId='bbl.shared'"
echo "       (run patch_handy_shareduid.sh — wipes Bambu's cert)"
echo "    2. user logs in to Bambu (mints cert into shared UID's KeyStore)"
echo "    3. install helper:   adb install -r build/sign-helper.apk"
echo "    4. list aliases:     adb shell am start -n com.x2d.sign/.SignActivity \\"
echo "                              -a com.x2d.sign.LIST_ALIASES; sleep 1; \\"
echo "                              adb shell cat /sdcard/x2d_sign_out.txt"
echo "    5. sign payload:     adb shell am start -n com.x2d.sign/.SignActivity \\"
echo "                              -a com.x2d.sign.SIGN_PAYLOAD \\"
echo "                              --es alias '<bambu_key_alias>' \\"
echo "                              --es payload '<base64_message_body>'"
