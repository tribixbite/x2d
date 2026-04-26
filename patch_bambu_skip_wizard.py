#!/usr/bin/env python3
"""Binary-patch bambu-studio to skip GUI_App::config_wizard_startup.

The Setup Wizard / WebGuideDialog asserts mid-network or hangs on Bambu
cloud calls (region/login) when run on Termux + termux-x11 with restricted
network. config_wizard_startup is a non-virtual private method whose call
is baked directly into the binary (libslic3r_gui.a is statically linked,
symbol is `-fvisibility=hidden`) — LD_PRELOAD overrides cannot intercept
intra-binary direct calls to it. The cleanest skip is rewriting its
prologue to `mov w0, #0; ret` so it always returns false. BambuStudio
then proceeds to MainFrame without ever calling run_wizard().

Side effect: user has to pick a printer manually inside the main UI
(Filament Settings → Printer) before slicing inside the GUI. The CLI
pipeline (resolve_profile.py + bambu-studio --slice) is unaffected since
it never reads AppConfig.

Rebuild resilience (item #18): instead of hardcoding the file offset
(which moves on every BambuStudio rebuild), this script auto-discovers
it by scanning the binary for the function's known prologue signature.
The hardcoded `LEGACY_OFFSET` is tried first as a fast path; if it
doesn't match the expected prologue, the signature scan kicks in.

Idempotent: reruns are no-ops if already patched. Backs up to .orig
on the first patch only.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

# Patch payload: `mov w0, #0` + `ret`  (8 bytes, little-endian aarch64)
PATCH = struct.pack("<II", 0x52800000, 0xd65f03c0)

# Function prologue we expect to overwrite. The first 4 instructions of
# config_wizard_startup as compiled with NDK r28c + clang -O2:
#   sub  sp, sp, #0x70           ; ff c3 01 d1
#   stp  x29, x30, [sp, #0x20]   ; fd 7b 02 a9   (observed)
#   stp  x29, x30, [sp, #0x60]   ; fd 7b 06 a9   (alt offset)
ORIG_HEAD_VARIANTS: list[bytes] = [
    bytes.fromhex("ffc301d1fd7b02a9"),  # observed on Termux build 02.06.00.51
    bytes.fromhex("ffc301d1fd7b06a9"),  # alt stp offset variant
]

# Fast-path file offset; if the bytes here match a known prologue we
# patch in-place. Becomes obsolete after rebuilds — that's fine, the
# scan handles drift.
LEGACY_OFFSET = 0x0000000002477d14

# Scan range: the function lives inside .text well after the dynamic
# loader stubs at the start. Bound the scan so we don't melt CPU on
# the 77 MB binary. .text on this build covers roughly 0x100000-0x4f00000.
SCAN_START = 0x00100000
SCAN_END   = 0x05000000


def _is_already_patched(buf: bytes, off: int) -> bool:
    return buf[off:off + 8] == PATCH


def _matches_prologue(buf: bytes, off: int) -> bool:
    return buf[off:off + 8] in ORIG_HEAD_VARIANTS


def _scan_prologue(buf: bytes) -> int:
    """Find the function's prologue. Returns the file offset of the
    candidate closest to LEGACY_OFFSET (so a small drift after a minor
    rebuild matches the right function), or -1 if no candidate exists."""
    candidates: list[int] = []
    for variant in ORIG_HEAD_VARIANTS:
        i = SCAN_START
        end = min(SCAN_END, len(buf))
        while True:
            j = buf.find(variant, i, end)
            if j < 0:
                break
            candidates.append(j)
            i = j + 4
    if not candidates:
        return -1
    candidates.sort(key=lambda x: abs(x - LEGACY_OFFSET))
    return candidates[0]


def main(path: Path) -> int:
    if not path.exists():
        print(f"binary not found: {path}", file=sys.stderr)
        return 2
    backup = path.with_suffix(path.suffix + ".orig")
    raw = path.read_bytes()

    # Fast path: legacy offset already shows the patch.
    if _is_already_patched(raw, LEGACY_OFFSET):
        print(f"already patched at legacy offset 0x{LEGACY_OFFSET:x}: {path}")
        return 0

    target_offset: int
    if _matches_prologue(raw, LEGACY_OFFSET):
        target_offset = LEGACY_OFFSET
        print(f"prologue at legacy offset 0x{LEGACY_OFFSET:x} matches — patching there")
    else:
        scanned = _scan_prologue(raw)
        if scanned < 0:
            print("ERROR: prologue not found anywhere in scan range. Either:",
                  file=sys.stderr)
            print("  - The binary was compiled with a different optimisation level"
                  " (try -O0 vs -O2)", file=sys.stderr)
            print("  - The function was inlined / removed", file=sys.stderr)
            print("  - The new prologue isn't in ORIG_HEAD_VARIANTS — extract it"
                  " from `objdump -d` and add", file=sys.stderr)
            return 3
        if _is_already_patched(raw, scanned):
            print(f"already patched at scanned offset 0x{scanned:x}: {path}")
            return 0
        target_offset = scanned
        drift = target_offset - LEGACY_OFFSET
        print(f"signature scan found prologue at 0x{target_offset:x} "
              f"(legacy 0x{LEGACY_OFFSET:x}, drift {drift:+d} bytes)")

    if not backup.exists():
        backup.write_bytes(raw)
        print(f"backup -> {backup}")

    with path.open("r+b") as f:
        f.seek(target_offset)
        f.write(PATCH)
    print(f"patched {path}: config_wizard_startup -> return false "
          f"(offset 0x{target_offset:x})")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path("/data/data/com.termux/files/home/git/x2d/bs-bionic/build/src/bambu-studio")
    sys.exit(main(target))
