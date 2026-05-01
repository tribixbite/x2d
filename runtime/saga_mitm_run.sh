#!/data/data/com.termux/files/usr/bin/bash
# Start mitmdump bound to 0.0.0.0:18080 in transparent mode + redirect Bambu UID 10217 traffic.
# Run inside Termux mount NS as uid 10212 first (mitmdump), then root portion separately for iptables.
set -uo pipefail

export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PREFIX/bin/applets:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
export TMPDIR=$PREFIX/tmp

# Clean any prior mitmdump
pkill -f "mitmdump" 2>/dev/null || true
sleep 1

LOG=$HOME/mitm.log
> "$LOG"

# local mode: replaces transparent on Linux, reads SO_ORIGINAL_DST from REDIRECT'd sockets.
# We bind explicitly to 18080 via reverse syntax-free approach: mitmproxy v12 'local' takes
# an optional spec. With no spec, it uses platform redirect. We still need a listen port for
# the iptables REDIRECT target — set via --listen-port.
nohup mitmdump \
  --mode transparent \
  --listen-host 0.0.0.0 \
  --listen-port 18080 \
  --no-http2 \
  --set termlog_verbosity=info \
  --set ssl_insecure=true \
  --set connection_strategy=lazy \
  --showhost \
  --save-stream-file "$HOME/bambu_saga.flow" \
  > "$LOG" 2>&1 &

MITM_PID=$!
echo "mitmdump PID=$MITM_PID"
sleep 4
if ! kill -0 $MITM_PID 2>/dev/null; then
  echo "FAILED to start mitmdump:"
  tail -40 "$LOG"
  exit 1
fi
ss -lntp 2>/dev/null | grep 18080 || netstat -lntp 2>/dev/null | grep 18080 || echo "(no ss/netstat — bound by /proc check below)"
ls -la /proc/$MITM_PID/net/tcp 2>/dev/null | head -3 || true
echo "--- mitm.log first 20 lines ---"
head -20 "$LOG"
