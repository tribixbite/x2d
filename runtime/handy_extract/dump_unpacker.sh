#!/usr/bin/env bash
# Dump Bambu Handy's unpacked shield code from anonymous executable mappings.
#
# Strategy:
#   1. Optionally disable ZygiskFrida targeting Bambu (so unpacker has no
#      shield-tripping race).
#   2. Force-stop and relaunch Bambu.
#   3. Poll /proc/PID/maps until [anon:.bss] r-xp mappings appear (or process
#      exits).
#   4. As root, dd /proc/PID/mem for each anonymous executable region into
#      /data/local/tmp/handy_anon_<base>_<size>.bin.
#   5. Pull the dumps to ./runtime/handy_extract/cache/.
#
# Usage: ./dump_unpacker.sh [--disable-frida] [--no-relaunch]
#
# Note: requires root on device (adb -s ... shell 'su -c ...').

set -euo pipefail

ADB_SERIAL="${ADB_SERIAL:-192.168.0.81:39705}"
PKG="bbl.intl.bambulab.com"
ACT="bbl.intl.bambulab.com/com.bambulab.appsec.MainActivity"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/cache/anon_dumps"
mkdir -p "$OUT_DIR"

ADB() { adb -s "$ADB_SERIAL" "$@"; }
SU()  { ADB shell "su -c \"$*\""; }

DISABLE_FRIDA=0
NO_RELAUNCH=0
for arg in "$@"; do
  case "$arg" in
    --disable-frida) DISABLE_FRIDA=1 ;;
    --no-relaunch)   NO_RELAUNCH=1 ;;
  esac
done

CFG=/data/local/tmp/re.zyg.fri/config.json
CFG_BAK=/data/local/tmp/re.zyg.fri/config.json.dumpbak

if [[ $DISABLE_FRIDA -eq 1 ]]; then
  echo "[+] disabling ZygiskFrida targeting for $PKG"
  SU "cp $CFG $CFG_BAK 2>/dev/null; sed -i 's/\\\"enabled\\\": true/\\\"enabled\\\": false/' $CFG"
fi

restore_frida() {
  if [[ $DISABLE_FRIDA -eq 1 ]]; then
    echo "[+] restoring ZygiskFrida config"
    SU "cp $CFG_BAK $CFG 2>/dev/null"
  fi
}
trap restore_frida EXIT

if [[ $NO_RELAUNCH -eq 0 ]]; then
  echo "[+] force-stopping $PKG"
  SU "am force-stop $PKG"
  sleep 1
  echo "[+] launching $PKG"
  SU "monkey -p $PKG -c android.intent.category.LAUNCHER 1" >/dev/null
fi

# Poll for PID
PID=""
for i in $(seq 1 50); do
  PID="$(SU "pidof $PKG" | tr -d '\r' || true)"
  [[ -n "$PID" ]] && break
  sleep 0.2
done
[[ -z "$PID" ]] && { echo "[!] Bambu PID not found"; exit 1; }
echo "[+] Bambu PID = $PID"

# Poll maps for anon executable mappings; allow up to ~12s.
ANON=""
for i in $(seq 1 60); do
  ANON="$(SU "grep -E 'r-xp 0+ 00:00 0' /proc/$PID/maps 2>/dev/null | grep -E 'anon:|anon\\]' | grep -v vdso | grep -v vvar")"
  if [[ -n "$ANON" ]]; then
    LINES=$(echo "$ANON" | wc -l)
    echo "[+] t=$((i*200))ms — found $LINES anon executable mapping(s):"
    echo "$ANON"
    break
  fi
  sleep 0.2
done
[[ -z "$ANON" ]] && { echo "[!] no anon executable mappings appeared"; exit 1; }

# Dump each region
echo "[+] dumping regions via root /proc/PID/mem"
echo "$ANON" | while IFS= read -r line; do
  RANGE="$(echo "$line" | awk '{print $1}')"
  START_HEX="${RANGE%-*}"
  END_HEX="${RANGE#*-}"
  START=$((16#$START_HEX))
  END=$((16#$END_HEX))
  SIZE=$((END - START))
  OUT="/data/local/tmp/handy_anon_${START_HEX}_${SIZE}.bin"
  echo "  region $RANGE ($SIZE bytes) -> $OUT"
  # dd with skip in 4096-byte blocks to avoid 64-bit offset issues
  SKIP=$((START / 4096))
  COUNT=$((SIZE / 4096))
  SU "dd if=/proc/$PID/mem of=$OUT bs=4096 skip=$SKIP count=$COUNT 2>&1" | tail -2
  ADB pull "$OUT" "$OUT_DIR/" 2>&1 | tail -1
done

echo "[+] done. dumps in $OUT_DIR"
ls -la "$OUT_DIR"
