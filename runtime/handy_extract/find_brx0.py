#!/usr/bin/env python3.12
"""
Map all BR x0 sites in the shield unpacked region. Print 16 instructions of
context for each so we can identify the tamper-die trampoline by visual
inspection (reg-clearing signature, x0 set from XOR of stack/reg values, etc).
"""

import os, glob, struct
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

DUMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "cache", "anon_dumps")

BR_X0 = struct.pack("<I", 0xd61f0000)
SHIELD_FILE = "handy_anon_705e482000_3031040.bin"

def main():
    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    path = os.path.join(DUMP_DIR, SHIELD_FILE)
    with open(path, "rb") as f:
        buf = f.read()
    base = int(SHIELD_FILE.split("_")[2], 16)
    print(f"shield base=0x{base:x} size=0x{len(buf):x}")

    sites = []
    off = 0
    while True:
        idx = buf.find(BR_X0, off)
        if idx < 0: break
        if idx & 3 == 0:
            sites.append(idx)
        off = idx + 4

    print(f"BR x0 count: {len(sites)}\n")
    # Print the first 30 sites with full context
    for i, idx in enumerate(sites[:30]):
        pre = max(0, idx - 60)
        post = min(len(buf), idx + 24)
        print(f"--- site #{i}  off=0x{idx:x}  abs=0x{base+idx:x}")
        for insn in md.disasm(buf[pre:post], pre):
            marker = " <-- BR x0" if insn.address == idx else ""
            print(f"  0x{insn.address:08x}: {insn.mnemonic:8s} "
                  f"{insn.op_str}{marker}")
        print()

if __name__ == "__main__":
    main()
