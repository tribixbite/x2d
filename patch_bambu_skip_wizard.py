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

# SHORT prologue (8 bytes): the strict aarch64 function-prologue
# boilerplate. Many functions share this — used only as a fast pre-filter.
ORIG_HEAD_VARIANTS: list[bytes] = [
    bytes.fromhex("ffc301d1fd7b02a9"),  # sub sp,#0x70 ; stp x29,x30,[sp,#0x20]
    bytes.fromhex("ffc301d1fd7b06a9"),  # alt stp offset
]

# LONG signatures (32 bytes): prologue + register-save sequence.
# These are unique enough across the binary to nail down the right
# function. Add a new entry whenever a rebuild changes the byte
# sequence — extract via:
#     dd if=bambu-studio.orig bs=1 skip=$prologue_offset count=32 | xxd
# Multiple known-good signatures so we survive minor rebuilds.
LONG_SIGNATURES: list[bytes] = [
    # Termux build 02.06.00.51 (NDK r28c + clang -O2):
    # ff c3 01 d1   sub  sp, sp, #0x70
    # fd 7b 02 a9   stp  x29, x30, [sp, #0x20]
    # f9 1b 00 f9   str  x25, [sp, #0x30]
    # f8 5f 04 a9   stp  x24, x23, [sp, #0x40]
    # f6 57 05 a9   stp  x22, x21, [sp, #0x50]
    # f4 4f 06 a9   stp  x20, x19, [sp, #0x60]
    # fd 83 00 91   add  x29, sp, #0x20
    # 08 18 48 39   ldrb w8, [x0, #0x206]
    bytes.fromhex(
        "ffc301d1fd7b02a9"  # 0x00 — short prologue
        "f91b00f9f85f04a9"  # 0x08 — register saves
        "f65705a9f44f06a9"  # 0x10 — more saves
        "fd83009108184839"  # 0x18 — frame setup + first body insn
    ),
]

# Fast-path file offset; if the long signature matches here we patch
# in-place without scanning. Becomes obsolete after rebuilds — that's
# fine, the scan handles drift.
LEGACY_OFFSET = 0x0000000002477c9c

# Scan range: the function lives inside .text well after the dynamic
# loader stubs at the start. Bound the scan so we don't melt CPU on
# the 77 MB binary. .text on this build covers roughly 0x100000-0x4f00000.
SCAN_START = 0x00100000
SCAN_END   = 0x05000000


def _is_already_patched(buf: bytes, off: int) -> bool:
    return buf[off:off + 8] == PATCH


def _matches_long_signature(buf: bytes, off: int) -> bool:
    """The 32-byte signature is the gold standard — prologue + register
    saves + first body instruction. Highly unlikely to false-positive."""
    candidate = buf[off:off + 32]
    return any(candidate == sig for sig in LONG_SIGNATURES)


def _matches_short_prologue(buf: bytes, off: int) -> bool:
    """8-byte prefix; many functions match. Used only as a pre-filter
    inside the scanner to keep the candidate list small."""
    return buf[off:off + 8] in ORIG_HEAD_VARIANTS


def _scan_prologue(buf: bytes) -> int:
    """Find config_wizard_startup by scanning for the long signature.

    Strategy: find every short-prologue match (cheap), then verify each
    against the full 32-byte signature. Only signature matches qualify.
    Tie-break by proximity to LEGACY_OFFSET so a small rebuild drift
    matches the same function.

    Returns the file offset of the best candidate, or -1 if none exist."""
    qualified: list[int] = []
    for short in ORIG_HEAD_VARIANTS:
        i = SCAN_START
        end = min(SCAN_END, len(buf))
        while True:
            j = buf.find(short, i, end)
            if j < 0:
                break
            if _matches_long_signature(buf, j):
                qualified.append(j)
            i = j + 4
    if not qualified:
        return -1
    qualified.sort(key=lambda x: abs(x - LEGACY_OFFSET))
    return qualified[0]


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
    if _matches_long_signature(raw, LEGACY_OFFSET):
        target_offset = LEGACY_OFFSET
        print(f"long signature match at legacy offset 0x{LEGACY_OFFSET:x} — "
              f"patching there")
    else:
        scanned = _scan_prologue(raw)
        if scanned < 0:
            print("ERROR: long signature not found anywhere in scan range. Either:",
                  file=sys.stderr)
            print("  - The binary was compiled with a different optimisation level"
                  " (try -O0 vs -O2)", file=sys.stderr)
            print("  - The function was inlined / removed", file=sys.stderr)
            print("  - A rebuild changed the body bytes — extract a fresh 32-byte"
                  " signature via `dd if=… skip=… count=32 | xxd` from the"
                  " function's prologue and add to LONG_SIGNATURES",
                  file=sys.stderr)
            return 3
        if _is_already_patched(raw, scanned):
            print(f"already patched at scanned offset 0x{scanned:x}: {path}")
            return 0
        target_offset = scanned
        drift = target_offset - LEGACY_OFFSET
        print(f"long-signature scan found function at 0x{target_offset:x} "
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
