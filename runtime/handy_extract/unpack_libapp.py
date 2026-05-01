#!/usr/bin/env python3.12
"""
Unpack and (optionally) repack Bambu Handy v3.19.0's libapp.so.

The shipped libapp.so is a CUSTOM-PACKED Dart AOT artifact. Externally it
looks like an ordinary shared object with 4 dynamic exports:
    _kDartVmSnapshotInstructions  (vaddr 0x1570000 size 0x16940)
    _kDartIsolateSnapshotInstructions (vaddr 0x1586940 size 0x1417fd0)
    _kDartVmSnapshotData          (vaddr 0xb70340 size 0x4750)
    _kDartIsolateSnapshotData     (vaddr 0xb74ac0 size 0x9ed7a0)
But the PT_LOAD that should back the instruction snapshots has filesz=0,
and the VAs reported by dynsym don't correspond to file content.

Internally:
  * Bytes 0..0x390 are ELF/dyn metadata.
  * 0x390..0x768 is .rela.dyn (R_AARCH64_RELATIVE entries that bind the
    Dart "snapshot" pointers at runtime).
  * 0x3970 starts a zstd frame (magic 28 b5 2f fd) of size 0x60e960
    that decompresses to 0x142e910 bytes — the compiled AOT instructions
    blob (Dart isolate instructions).
  * 0x6122d0 starts a second zstd frame of size 0x57e9d8 that
    decompresses to 0x9f20da bytes — the isolate snapshot DATA blob
    (string table + object pool + constants).
  * 0xb70000 holds an embedded ELF header that describes how the unpacker
    must lay these two blobs into memory (PHDR at 0x40, two PT_LOAD
    segments at vaddr 0 and 0xa00000 plus a small dynamic).

The unpacker (likely in libflutter.so or a static-init constructor in
libapp.so itself) maps libapp.so file pages, decompresses each zstd
frame into a fresh anon mapping, then rewrites the dynsym slots (or PT
table entries) to point at the decompressed pages so Dart sees a
"normal" snapshot layout.

Usage:
    # Unpack into raw artefacts:
    python3.12 unpack_libapp.py unpack <libapp.so> <outdir>
    # Repack patched artefacts back into a libapp.so:
    python3.12 unpack_libapp.py pack <libapp.so> <indir> <out_libapp.so>

The repack assumes the patched frames compress to <= the original frame
size (level 3 zstd typically achieves this for these blobs). If they do
not, repack fails — bump the compression level or split a frame.

Why this script exists:
The mitmproxy bypass effort against Bambu Handy needs to inspect / patch
the Dart-level cert-pinning code (PATH #1 — libapp.so / Dart AOT)
because libflutter.so patches alone (PATH #2) only neutralise the
BoringSSL ASN1/SecurityContext calls. Any pinning that Bambu performs
*above* the BoringSSL layer (e.g. badCertificateCallback returning false
based on the SHA-256 of the leaf cert) lives inside the AOT instructions
in Frame 1, with constant string operands (host names, regex of
TLDs) inside Frame 2. We cannot grep those frames inside libapp.so as
shipped — we MUST decompress first.
"""
from __future__ import annotations
import os
import struct
import sys

import zstandard as zstd

# Hard-coded layout for Bambu Handy v3.19.0's libapp.so (12,127,400 bytes,
# build-id 0xb594c0..0xb594e0). Verify by checking SHA-256 before running.
LIBAPP_SIZE = 0xb90ca8
FRAME1_OFF = 0x3970
FRAME1_COMPRESSED_SIZE = 0x60e960  # bytes between magic and start of frame 2
FRAME1_DECOMPRESSED_SIZE = 0x142e910

FRAME2_OFF = 0x6122d0
FRAME2_COMPRESSED_SIZE = 0x57e9d8  # bytes between magic and EOF
FRAME2_DECOMPRESSED_SIZE = 0x9f20da

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def unpack(libapp_path: str, outdir: str) -> None:
    os.makedirs(outdir, exist_ok=True)
    with open(libapp_path, "rb") as fp:
        data = fp.read()
    if len(data) != LIBAPP_SIZE:
        print(
            f"warning: file size 0x{len(data):x} != expected 0x{LIBAPP_SIZE:x}",
            file=sys.stderr,
        )

    if data[FRAME1_OFF : FRAME1_OFF + 4] != ZSTD_MAGIC:
        raise SystemExit(
            f"frame1 zstd magic missing at 0x{FRAME1_OFF:x}: "
            f"got {data[FRAME1_OFF : FRAME1_OFF + 4].hex()}"
        )
    if data[FRAME2_OFF : FRAME2_OFF + 4] != ZSTD_MAGIC:
        raise SystemExit(
            f"frame2 zstd magic missing at 0x{FRAME2_OFF:x}: "
            f"got {data[FRAME2_OFF : FRAME2_OFF + 4].hex()}"
        )

    dctx = zstd.ZstdDecompressor()

    # Frame 1: AOT compiled instructions blob.
    decomp1 = dctx.decompress(
        data[FRAME1_OFF:], max_output_size=FRAME1_DECOMPRESSED_SIZE * 2
    )
    if len(decomp1) != FRAME1_DECOMPRESSED_SIZE:
        print(
            f"warning: frame1 decompressed size 0x{len(decomp1):x} "
            f"!= expected 0x{FRAME1_DECOMPRESSED_SIZE:x}",
            file=sys.stderr,
        )

    # Frame 2: isolate snapshot data blob (strings, object pool).
    decomp2 = dctx.decompress(
        data[FRAME2_OFF:], max_output_size=FRAME2_DECOMPRESSED_SIZE * 2
    )
    if len(decomp2) != FRAME2_DECOMPRESSED_SIZE:
        print(
            f"warning: frame2 decompressed size 0x{len(decomp2):x} "
            f"!= expected 0x{FRAME2_DECOMPRESSED_SIZE:x}",
            file=sys.stderr,
        )

    # Header: bytes 0..FRAME1_OFF (ELF + .rela.dyn + dynsym/dynstr).
    header = data[:FRAME1_OFF]
    # Trailer: bytes FRAME1_OFF + FRAME1_COMPRESSED_SIZE .. FRAME2_OFF
    # (should be empty in this build but written for safety).
    interlude = data[FRAME1_OFF + FRAME1_COMPRESSED_SIZE : FRAME2_OFF]
    # Tail: bytes after FRAME2_OFF + FRAME2_COMPRESSED_SIZE
    # (should be empty / EOF).
    tail = data[FRAME2_OFF + FRAME2_COMPRESSED_SIZE :]

    # Sanity: interlude and tail expected to be empty for this build.
    if interlude:
        print(
            f"note: 0x{len(interlude):x} bytes between frame1 and frame2 "
            f"will be preserved verbatim",
            file=sys.stderr,
        )
    if tail:
        print(
            f"note: 0x{len(tail):x} trailing bytes after frame2 "
            f"will be preserved verbatim",
            file=sys.stderr,
        )

    with open(os.path.join(outdir, "header.bin"), "wb") as fp:
        fp.write(header)
    with open(os.path.join(outdir, "frame1_instructions.bin"), "wb") as fp:
        fp.write(decomp1)
    with open(os.path.join(outdir, "interlude.bin"), "wb") as fp:
        fp.write(interlude)
    with open(os.path.join(outdir, "frame2_snapshot_data.bin"), "wb") as fp:
        fp.write(decomp2)
    with open(os.path.join(outdir, "tail.bin"), "wb") as fp:
        fp.write(tail)
    print(
        f"unpacked: header=0x{len(header):x} frame1=0x{len(decomp1):x} "
        f"frame2=0x{len(decomp2):x} interlude=0x{len(interlude):x} "
        f"tail=0x{len(tail):x}"
    )


def pack(libapp_path: str, indir: str, out_libapp_path: str) -> None:
    """Reassemble a patched libapp.so from a previously-unpacked dir.

    Both zstd frames must compress to <= their original size so the file
    layout stays identical (offset of the embedded ELF metadata and
    .rela.dyn must not move). If a frame is larger after recompression,
    pack() retries with progressively higher compression levels and
    aborts if no level fits.
    """
    with open(libapp_path, "rb") as fp:
        original = fp.read()

    with open(os.path.join(indir, "header.bin"), "rb") as fp:
        header = fp.read()
    with open(os.path.join(indir, "frame1_instructions.bin"), "rb") as fp:
        decomp1 = fp.read()
    with open(os.path.join(indir, "interlude.bin"), "rb") as fp:
        interlude = fp.read()
    with open(os.path.join(indir, "frame2_snapshot_data.bin"), "rb") as fp:
        decomp2 = fp.read()
    with open(os.path.join(indir, "tail.bin"), "rb") as fp:
        tail = fp.read()

    if len(header) != FRAME1_OFF:
        raise SystemExit(
            f"header size 0x{len(header):x} != expected 0x{FRAME1_OFF:x}"
        )

    def compress_to_fit(decomp: bytes, budget: int, name: str) -> bytes:
        # Walk levels low → high; the higher levels burn cpu but rarely
        # buy more than 1-2% extra ratio for already-compressed data.
        for level in (3, 6, 9, 12, 15, 17, 19, 22):
            cctx = zstd.ZstdCompressor(level=level)
            out = cctx.compress(decomp)
            if len(out) <= budget:
                print(
                    f"{name}: level={level} compressed=0x{len(out):x} "
                    f"budget=0x{budget:x}"
                )
                # Pad with zeros up to original budget so file layout
                # remains identical to the unpatched libapp.so.
                return out + b"\x00" * (budget - len(out))
        raise SystemExit(
            f"{name}: no compression level produced a frame <= 0x{budget:x}; "
            f"smallest was level 22 at 0x{len(out):x}"
        )

    c1 = compress_to_fit(decomp1, FRAME1_COMPRESSED_SIZE, "frame1")
    c2 = compress_to_fit(decomp2, FRAME2_COMPRESSED_SIZE, "frame2")

    # NOTE: zstd zero-padding past frame end is tolerated by the
    # ZstdDecompressor (it stops at the frame magic+epilogue), so the
    # unpacker won't read past the legitimate compressed bytes.

    out = header + c1 + interlude + c2 + tail
    if len(out) != LIBAPP_SIZE:
        # Should be impossible if budgets line up; fail fast instead of
        # writing an inconsistent file.
        raise SystemExit(
            f"packed size 0x{len(out):x} != expected 0x{LIBAPP_SIZE:x}"
        )
    if out == original:
        print("packed file is identical to input — no patches applied?")
    with open(out_libapp_path, "wb") as fp:
        fp.write(out)
    print(f"wrote {out_libapp_path} (size=0x{len(out):x})")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "unpack" and len(sys.argv) == 4:
        unpack(sys.argv[2], sys.argv[3])
    elif cmd == "pack" and len(sys.argv) == 5:
        pack(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
