#!/data/data/com.termux/files/usr/bin/bash
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
mitmdump --help 2>&1 | grep -A 2 "^  --mode\|^  -m" | head -30
echo ---options---
mitmdump --help 2>&1 | sed -n '/^Modes/,/^[A-Z]/p' | head -40
