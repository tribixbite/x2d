#!/data/data/com.termux/files/usr/bin/bash
# Stage 2 Termux setup: install rust, then mitmproxy via pip.
# Run inside Termux mount NS as uid 10212.
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
mkdir -p "$TMPDIR" "$HOME/.cargo"

echo "=== Step A: install rust + build deps for cryptography ==="
yes | pkg install -y rust binutils 2>&1 | tail -8

echo "=== Step B: pip install mitmproxy (cryptography will compile via rust) ==="
# Tell cryptography's build system where rust lives + force android target it actually has prebuilt for
export CARGO_HOME=$HOME/.cargo
export RUSTUP_HOME=$HOME/.rustup
# Termux's rust pkg ships the std for aarch64-linux-android prebuilt
export CARGO_BUILD_TARGET=aarch64-linux-android
# pip honors --no-binary only for things WITHOUT wheels; but mitmproxy itself has a pure wheel.
# cryptography is the slow one. brotli, zstandard, lxml may also need build.
python3 -m pip install --upgrade pip 2>&1 | tail -3
python3 -m pip install --no-cache-dir mitmproxy 2>&1 | tail -40

echo "=== Final check ==="
command -v mitmdump python3
mitmdump --version 2>&1 | head -5 || echo "mitmdump install FAILED"
