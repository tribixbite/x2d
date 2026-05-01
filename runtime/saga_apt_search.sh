#!/data/data/com.termux/files/usr/bin/bash
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PATH
export HOME=/data/data/com.termux/files/home
export LD_LIBRARY_PATH=$PREFIX/lib
export TMPDIR=$PREFIX/tmp

echo "=== Searching for cryptography / mitmproxy ==="
apt-cache search cryptography mitmproxy 2>&1 | grep -iE "cryptography|mitmproxy"
echo ---
apt list --installed 2>/dev/null | grep -iE "rust|crypto|ssl|ffi" | head
echo ---
echo "=== rust availability ==="
apt-cache show rust 2>&1 | head -8
