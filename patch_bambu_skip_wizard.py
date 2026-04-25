#!/usr/bin/env python3
"""Binary-patch bambu-studio to skip GUI_App::config_wizard_startup.

The Setup Wizard / WebGuideDialog asserts mid-network or hangs on Bambu
cloud calls (region/login) when run on Termux + termux-x11 with restricted
network. config_wizard_startup is a private method whose call is baked
directly into the binary (libslic3r_gui.a is statically linked) — LD_PRELOAD
overrides don't intercept intra-binary calls. The cleanest skip is rewriting
its prologue to `mov w0, #0; ret` so it always returns false. BambuStudio
then proceeds to MainFrame without ever calling run_wizard().

Side effects: user has to pick a printer manually inside the main UI
(Filament Settings → Printer) before slicing inside the GUI. The CLI
pipeline (resolve_profile.py + bambu-studio --slice) is unaffected since
it never reads AppConfig.

Idempotent: the script reads the existing prologue first; if already
patched (mov w0,0 / ret), it's a no-op. Always backs up to .orig.

Verified working on bambu-studio v02.06.00.51 built locally on Termux.
The function offset 0x2477c9c is build-specific; if you rebuild
BambuStudio, regenerate this offset:
    objdump -d bambu-studio | grep '<_ZN6Slic3r3GUI7GUI_App21config_wizard_startupEv>:'
and update OFFSET below (file offset == VMA on this build because the
.text segment loads at 0x0).
"""
import struct, sys
from pathlib import Path

OFFSET = 0x0000000002477c44                # file offset == VMA on this build
PATCH = struct.pack('<II',
                    0x52800000,    # mov w0, #0
                    0xd65f03c0)    # ret
ORIG_HEAD = bytes.fromhex('ffc301d1fd7b02a9')  # sub sp, #0x70 ; stp x29,x30 — original prologue

def main(path: Path) -> int:
    backup = path.with_suffix(path.suffix + '.orig')
    with path.open('r+b') as f:
        f.seek(OFFSET)
        cur = f.read(8)
        if cur == PATCH:
            print(f"already patched: {path}")
            return 0
        if cur != ORIG_HEAD:
            print(f"unexpected bytes at 0x{OFFSET:x}: {cur.hex()}", file=sys.stderr)
            print(f"expected:                       {ORIG_HEAD.hex()}", file=sys.stderr)
            return 2
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
            print(f"backup -> {backup}")
        f.seek(OFFSET)
        f.write(PATCH)
    print(f"patched {path}: config_wizard_startup -> return false")
    return 0

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path("/data/data/com.termux/files/home/git/x2d/bs-bionic/build/src/bambu-studio")
    sys.exit(main(target))
