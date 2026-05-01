#!/data/data/com.termux/files/usr/bin/bash
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
python3 - <<'EOF'
import sys
print("sys.platform:", repr(sys.platform))
import mitmproxy.platform as p
print("original_addr:", p.original_addr)
print("init_transparent_mode:", p.init_transparent_mode)

# Check linux.py
try:
    from mitmproxy.platform import linux
    print("linux module loaded, original_addr =", linux.original_addr)
except Exception as e:
    print("linux import failed:", e)
EOF
