#!/system/bin/sh
LOG=/data/data/bbl.intl.bambulab.com/cache/handy_capture.log
echo "total_lines=$(wc -l < $LOG)"
echo "hook_installs=$(grep -c '^LOG hooked' $LOG)"
echo "crypto_events=$(grep -cE 'rsa_key|sign_call|blob' $LOG)"
echo "anti_events=$(grep -cE 'faked|blocked|suppressed|exception' $LOG)"
echo
echo "=== non-LOG lines (events) ==="
grep -v '^LOG ' $LOG | head -50
echo
echo "=== Handy state ==="
echo "pid=$(pidof bbl.intl.bambulab.com)"
ACT=$(dumpsys activity activities 2>/dev/null | grep topResumedActivity | head -1)
echo "fg=$ACT"
