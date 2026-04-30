#!/usr/bin/env python3.12
"""
Scan the shield's unpacked region for XOR-encoded representations of
0xdead5019. The shield obfuscation does:
    x0 = a; x0 ^= b;  -> 0xdead5019
where a and b are stored as 32-bit literals in the data section.

For every 4-byte-aligned 32-bit word in the dump, compute word ^ 0xdead5019
and search for the result anywhere else in the dump. If both the literal and
its XOR-pair exist, that's a strong candidate for the magic-construction.

Also try sums/differences:
    a + b == 0xdead5019
    a - b == 0xdead5019

And ROR/REV:
    rev(0xdead5019) == 0x1950adde
    rbit(0xdead5019) == 0x980ab57b

Print the literal offsets and rough call-site lookups.
"""

import os, struct, sys
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "cache", "anon_dumps", "handy_anon_705e482000_3031040.bin")
BASE = 0x705e482000

MAGIC = 0xdead5019
MAGIC64 = 0x00000000dead5019

def rev32(v):
    return ((v >> 24) & 0xff) | (((v >> 16) & 0xff) << 8) | \
           (((v >> 8) & 0xff) << 16) | ((v & 0xff) << 24)

def rbit32(v):
    out = 0
    for i in range(32):
        if v & (1 << i):
            out |= 1 << (31 - i)
    return out

def main():
    with open(DUMP, "rb") as f:
        buf = f.read()
    print(f"shield dump: size=0x{len(buf):x}")

    # Build a set of every 4-byte-aligned word value -> list of offsets
    word_to_offs = {}
    for off in range(0, len(buf) - 3, 4):
        v = struct.unpack_from("<I", buf, off)[0]
        word_to_offs.setdefault(v, []).append(off)

    print(f"distinct 4-byte words: {len(word_to_offs)}")

    candidates = {
        "0xdead5019 literal":               MAGIC,
        "rev(0xdead5019)=0x1950adde":       rev32(MAGIC),
        "rbit(0xdead5019)=0x980ab57b":      rbit32(MAGIC),
        "~0xdead5019=0x2152afe6":           (~MAGIC) & 0xffffffff,
        "-0xdead5019=0x2152afe7":           (-MAGIC) & 0xffffffff,
    }
    for name, v in candidates.items():
        offs = word_to_offs.get(v, [])
        print(f"\n  {name} (0x{v:08x}): {len(offs)} occurrence(s)")
        for o in offs[:8]:
            print(f"    off=0x{o:x}  abs=0x{BASE+o:x}")

    # XOR-pair search: for every word w, look for (w ^ MAGIC). If found,
    # report. Limit results.
    print("\n  XOR pairs (a XOR b == 0xdead5019):")
    pair_hits = 0
    seen = set()
    for w, offs_a in word_to_offs.items():
        target = w ^ MAGIC
        if target == w: continue
        if (target, w) in seen: continue
        seen.add((w, target))
        offs_b = word_to_offs.get(target)
        if not offs_b: continue
        # Filter: ignore very common values (0, 0xffffffff, small ints) to
        # cut noise
        if w < 0x10000 or target < 0x10000: continue
        if w == 0xffffffff or target == 0xffffffff: continue
        pair_hits += 1
        if pair_hits <= 12:
            print(f"    0x{w:08x} (n={len(offs_a)}) ^ 0x{target:08x} (n={len(offs_b)}) "
                  f"  example: off_a=0x{offs_a[0]:x}, off_b=0x{offs_b[0]:x}")
    print(f"  total non-trivial XOR pairs: {pair_hits}")

    # 64-bit literal sweep: 0x00000000dead5019 (anchor at 8-byte aligned)
    target8 = struct.pack("<Q", MAGIC64)
    off = 0
    print("\n  64-bit literal 0x00000000dead5019:")
    while True:
        idx = buf.find(target8, off)
        if idx < 0: break
        if idx & 7 == 0:
            print(f"    off=0x{idx:x}  abs=0x{BASE+idx:x}")
        off = idx + 1

if __name__ == "__main__":
    main()
