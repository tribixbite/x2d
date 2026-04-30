#!/system/bin/sh
# run_capture.sh — full orchestration on-device.
# Push to /data/local/tmp/, run via `adb shell su -c 'sh /data/local/tmp/run_capture.sh'`
#
# - Pushes the latest init.js (concatenated by build_init.sh on host)
# - Force-stops Bambu Handy, clears capture log
# - Launches Handy via monkey
# - Drives a sequence of UI taps to provoke crypto activity
# - Waits long enough for Handy to talk to cloud + memscan
# - Streams the capture log to stdout when done
#
# Usage:
#   adb push run_capture.sh /data/local/tmp/run_capture.sh
#   adb shell 'su -c "sh /data/local/tmp/run_capture.sh"'

set -e

PKG=bbl.intl.bambulab.com
LOG=/data/data/$PKG/cache/handy_capture.log

echo "=== run_capture starting at $(date) ==="

# Restart cleanly
am force-stop $PKG
sleep 2
rm -f "$LOG" 2>/dev/null

# Wake the screen
input keyevent KEYCODE_WAKEUP
sleep 0.5
wm dismiss-keyguard 2>/dev/null

# Launch
monkey -p $PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 6

PID=$(pidof $PKG)
echo "Handy PID: $PID"
[ -z "$PID" ] && { echo "FAILED: Handy didn't spawn"; exit 1; }

# Drive UI in a loop — taps the printer card, scrolls, light toggle, etc.
echo "=== driving UI ==="
for cycle in 1 2 3 4 5; do
  echo "--- cycle $cycle ---"
  input swipe 540 600 540 1500 250
  sleep 4
  input tap 540 850
  sleep 5
  input swipe 540 1500 540 600 250
  sleep 3
  input tap 540 1100
  sleep 4
  input tap 200 700
  sleep 3
  # Foregound check
  pidof $PKG > /dev/null || { echo "Handy died during cycle $cycle"; break; }
done

echo
echo "=== final wait (15s) for memscan + late events ==="
sleep 15
pidof $PKG > /dev/null && echo "Handy still alive" || echo "Handy DIED at end"

echo
echo "=== capture log size ==="
wc -l "$LOG" 2>/dev/null

echo
echo "=== HOOK INSTALL EVENTS ==="
grep -c "LOG hooked" "$LOG" 2>/dev/null

echo
echo "=== CRYPTO EVENTS (rsa_key, sign_call, blob, sniff) ==="
grep -cE "rsa_key|sign_call|blob|PEM_FOUND|DER_FOUND" "$LOG" 2>/dev/null

echo
echo "=== MEMSCAN summary ==="
grep "memscan" "$LOG" 2>/dev/null

echo
echo "=== ANY PEM/DER FOUND ==="
grep -B1 -A30 "PEM_FOUND\|DER_FOUND" "$LOG" 2>/dev/null | head -80

echo
echo "=== full log size + path ==="
ls -la "$LOG"
echo "=== run_capture done at $(date) ==="
