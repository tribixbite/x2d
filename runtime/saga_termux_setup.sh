#!/data/data/com.termux/files/usr/bin/bash
# Run as Termux's user (uid 10212) inside Saga.
# Sets up Termux environment, installs python+mitmproxy.
# Invoked via: su 10212 -c '/data/data/com.termux/files/usr/bin/bash /data/local/tmp/saga_termux_setup.sh'
# Termux's `pkg` script refuses to run as root, so we drop to uid 10212 (com.termux's app uid).
set -uo pipefail

export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PREFIX/bin/applets:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
export TMPDIR=$PREFIX/tmp
export ANDROID_DATA=/data
export ANDROID_ROOT=/system
export TERM=xterm-256color
export LANG=en_US.UTF-8

mkdir -p "$TMPDIR" "$HOME"

echo "=== Termux env ==="
echo "HOME=$HOME PREFIX=$PREFIX"
which pkg apt python3 mitmdump 2>&1 || true

echo "=== Step 1: pkg update (apt update) ==="
yes | pkg update -y 2>&1 | tail -20 || true

echo "=== Step 2: install python + iptables (root utils) ==="
yes | pkg install -y python python-pip iptables openssl libffi 2>&1 | tail -30

echo "=== Step 3: pip install mitmproxy ==="
python3 -m pip install --upgrade pip setuptools wheel 2>&1 | tail -5
python3 -m pip install mitmproxy 2>&1 | tail -20

echo "=== Final check ==="
which mitmdump python3
mitmdump --version 2>&1 | head -5 || echo "mitmdump install failed"
