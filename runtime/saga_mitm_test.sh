#!/system/bin/sh
# Test mitmdump connectivity + log path. Run as root.
set -u

echo "=== mitmdump process ==="
ps -ef | grep mitmdump | grep -v grep | head

echo "=== mitm.log path check ==="
ls -la /data/data/com.termux/files/home/mitm.log 2>&1
ls -la /data/data/com.termux/files/home/bambu_saga.flow 2>&1

echo "=== port 18080 listening (root view) ==="
ss -lntp 2>/dev/null | grep 18080
netstat -lntp 2>/dev/null | grep 18080

echo "=== curl through mitmdump explicit-proxy ==="
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib \
  /data/data/com.termux/files/usr/bin/curl \
  --cacert /data/data/com.termux/files/home/.mitmproxy/mitmproxy-ca-cert.pem \
  -sS -o /dev/null -w 'HTTP %{http_code} time %{time_total}s\n' \
  --max-time 8 \
  -x http://127.0.0.1:18080 \
  https://api.bambulab.com/ 2>&1

echo "=== curl direct to api.bambulab.com (no proxy) ==="
LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib \
  /data/data/com.termux/files/usr/bin/curl \
  -sS -o /dev/null -w 'HTTP %{http_code} time %{time_total}s\n' \
  --max-time 5 \
  https://api.bambulab.com/ 2>&1

echo "=== mitm.log tail (after the curls) ==="
sleep 1
tail -30 /data/data/com.termux/files/home/mitm.log 2>&1
