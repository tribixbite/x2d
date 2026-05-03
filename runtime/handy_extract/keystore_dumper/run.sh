#!/data/data/com.termux/files/usr/bin/bash
# Push the dumper DEX to the device and run it under Bambu's UID via Magisk.
#
# `su` w/o args  -> root  (Magisk binder)
# `su <uid>`     -> setuid + setexeccon to the matching package context
#
# Bambu's UID is whatever `pm list packages -U bbl.intl.bambulab.com`
# reports today; pulled dynamically below since it changes on each
# (re)install.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

CLASS="${1:-com.x2d.dump.ListAliases}"
DEX="${DEX:-build/classes.dex}"
[[ -f "$DEX" ]] || { echo "missing $DEX -- run ./build.sh first"; exit 1; }

PKG="bbl.intl.bambulab.com"

# Grab Bambu's runtime UID from pm. Format: "package:bbl.intl.bambulab.com uid:10232"
BAMBU_UID=$(adb shell pm list packages -U "$PKG" | sed -n 's/.*uid://p' | tr -d '\r')
[[ -n "$BAMBU_UID" ]] || { echo "couldn't resolve UID for $PKG"; exit 1; }
echo "[+] Bambu UID = $BAMBU_UID"

REMOTE=/data/local/tmp/x2d_dumper.dex
echo "[+] pushing DEX -> $REMOTE"
adb push "$DEX" "$REMOTE" >/dev/null
adb shell chmod 644 "$REMOTE"

echo "[+] running $CLASS as uid $BAMBU_UID"
adb shell "su -c 'su $BAMBU_UID -c \"/system/bin/app_process -Djava.class.path=$REMOTE / $CLASS\"'"
