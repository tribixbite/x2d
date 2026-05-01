#!/data/data/com.termux/files/usr/bin/bash
# Restore and patch mitmproxy platform module to treat 'android' as Linux for transparent mode.
export PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib

F=$PREFIX/lib/python3.13/site-packages/mitmproxy/platform/__init__.py

# Restore canonical content first (in case prior sed corrupted it)
python3 - <<'EOF'
import os, re, pathlib
F = "/data/data/com.termux/files/usr/lib/python3.13/site-packages/mitmproxy/platform/__init__.py"
canonical = '''import re
import socket
import sys
from collections.abc import Callable


def init_transparent_mode() -> None:
    """
    Initialize transparent mode.
    """


original_addr: Callable[[socket.socket], tuple[str, int]] | None
"""
Get the original destination for the given socket.
This function will be None if transparent mode is not supported.
"""

if re.match(r"linux(?:2)?|android", sys.platform):
    from . import linux

    original_addr = linux.original_addr
elif sys.platform == "darwin" or sys.platform.startswith("freebsd"):
    from . import osx

    original_addr = osx.original_addr
elif sys.platform.startswith("openbsd"):
    from . import openbsd

    original_addr = openbsd.original_addr
elif sys.platform == "win32":
    from . import windows

    resolver = windows.Resolver()
    init_transparent_mode = resolver.setup  # noqa
    original_addr = resolver.original_addr
else:
    original_addr = None

__all__ = ["original_addr", "init_transparent_mode"]
'''
pathlib.Path(F).write_text(canonical)
# Drop bytecode cache
import shutil
cache = "/data/data/com.termux/files/usr/lib/python3.13/site-packages/mitmproxy/platform/__pycache__"
if os.path.isdir(cache):
    shutil.rmtree(cache)
print("Patched", F)
EOF

# Verify
python3 -c "import sys, mitmproxy.platform as p; print('platform:', sys.platform, 'original_addr:', p.original_addr)"
