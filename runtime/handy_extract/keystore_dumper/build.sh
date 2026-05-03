#!/data/data/com.termux/files/usr/bin/bash
# Build a standalone classes.dex for the keystore-dumper helpers.
# Phase 1 deps: only android.jar (KeyStore + os.Process).
# Phase 2 will add androidx.security:security-crypto + tink-android.
#
# Run via app_process after Magisk drops to Bambu's UID — see run.sh.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

ANDROID_HOME=${ANDROID_HOME:-$HOME/android-sdk}
PLATFORM_JAR="$ANDROID_HOME/platforms/android-34/android.jar"
BT_DIR="$ANDROID_HOME/build-tools/34.0.0-arm64"
[[ -x "$BT_DIR/d8" ]] || BT_DIR="$ANDROID_HOME/build-tools/34.0.0"

[[ -f "$PLATFORM_JAR" ]] || { echo "missing $PLATFORM_JAR"; exit 1; }
[[ -x "$BT_DIR/d8" ]]    || { echo "missing d8 in $BT_DIR";  exit 1; }

mkdir -p build/classes
echo "[+] javac"
javac -source 1.8 -target 1.8 -bootclasspath "$PLATFORM_JAR" \
      -d build/classes $(find src -name '*.java')

echo "[+] d8 -> classes.dex"
"$BT_DIR/d8" --output build/ --lib "$PLATFORM_JAR" \
    $(find build/classes -name '*.class')

ls -la build/classes.dex
