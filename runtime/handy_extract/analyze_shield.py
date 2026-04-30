#!/usr/bin/env python3.12
"""
Scan unpacked Bambu Handy shield dumps for the 0xdead5019 tamper-die constant
load and identify the conditional branch that gates it.

Tries multiple patterns:
  A. MOVZ + MOVK pair producing 0xdead5019 (any Xn/Wn, both halves orderings)
  B. Literal 0xdead5019 (32-bit) in the dump — typically a literal pool that
     gets loaded by an LDR Xn, =const (LDR-literal PC-relative). For each
     literal hit, find the LDR-literal that references that offset.
  C. Quad 0x00000000dead5019 likewise.

For each MAGIC PC-load site, walk back to find:
  - the function prologue (STP X29, X30, [sp,#-N]!)
  - the most recent conditional branch (b.eq/b.ne/cbz/cbnz/tbz/tbnz/cmp+b.cc)
  These are the gate(s) that decide whether the 'BR x0' tamper-die runs.
"""

import os, sys, glob, struct
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

DUMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "cache", "anon_dumps")

def encode_movz(sf, hw, imm16, rd):
    return (sf << 31) | (0b10100101 << 23) | (hw << 21) | (imm16 << 5) | rd

def encode_movk(sf, hw, imm16, rd):
    return (sf << 31) | (0b11100101 << 23) | (hw << 21) | (imm16 << 5) | rd

def gen_pair_patterns():
    LO = 0x5019
    HI = 0xdead
    pats = []
    for sf in (0, 1):
        regname = "X" if sf else "W"
        for rd in range(31):
            a = encode_movz(sf, 0, LO, rd)
            b = encode_movk(sf, 1, HI, rd)
            pats.append((struct.pack("<I", a) + struct.pack("<I", b),
                         f"MOVZ {regname}{rd},#0x{LO:x};MOVK {regname}{rd},#0x{HI:x},lsl#16"))
            a = encode_movz(sf, 1, HI, rd)
            b = encode_movk(sf, 0, LO, rd)
            pats.append((struct.pack("<I", a) + struct.pack("<I", b),
                         f"MOVZ {regname}{rd},#0x{HI:x},lsl#16;MOVK {regname}{rd},#0x{LO:x}"))
    return pats

def find_prologue(buf, end_off, look_back=0x800):
    for off in range(end_off & ~3, max(0, end_off - look_back), -4):
        word = struct.unpack_from("<I", buf, off)[0]
        if (word >> 22) & 0x3ff == 0b1010100110:
            rt  = word & 0x1f
            rt2 = (word >> 10) & 0x1f
            rn  = (word >> 5) & 0x1f
            if rt == 29 and rt2 == 30 and rn == 31:
                return off
        # Also accept 'sub sp, sp, #imm' — bits: 1 1 0 1 0 0 0 1 0 sh imm12 Rn(sp) Rd(sp)
        # Encoding 0xd1 + bits...
        if (word & 0xff8003ff) == 0xd10003ff:  # sub sp, sp, #imm12 (sh=0)
            return off
    return None

def find_gates(md, buf, magic_off, look_back=0x80):
    """Return list of all conditional branches in the look_back window."""
    start = max(0, magic_off - look_back)
    gates = []
    for insn in md.disasm(buf[start:magic_off], start):
        mn = insn.mnemonic.lower()
        if mn.startswith("b.") or mn in ("cbz", "cbnz", "tbz", "tbnz"):
            gates.append((insn.address, insn.mnemonic, insn.op_str,
                          bytes(insn.bytes)))
    return gates

def find_ldr_literal_to(buf, target_off, look_back=0x100000):
    """Scan all 4-byte aligned PC-relative LDR-literal instructions in buf
    and return those whose computed PC + offset == target_off."""
    # LDR (literal) 64-bit: 0x58000000 | (imm19 << 5) | Rt   ; 0x58 prefix
    # LDR (literal) 32-bit: 0x18000000 | (imm19 << 5) | Rt   ; 0x18 prefix
    # LDRSW (literal):      0x98000000
    # PRFM  (literal):      0xd8000000
    hits = []
    # Practical scan window: we don't search the whole buf, just within look_back
    start = max(0, target_off - look_back) & ~3
    end = min(len(buf), target_off + 4)
    for off in range(start, end, 4):
        word = struct.unpack_from("<I", buf, off)[0]
        op = (word >> 24) & 0xff
        if op in (0x18, 0x58, 0x98):  # LDR W, LDR X, LDRSW
            imm19 = (word >> 5) & 0x7ffff
            # sign extend
            if imm19 & 0x40000:
                imm19 -= 0x80000
            tgt = off + imm19 * 4
            if tgt == target_off:
                rt = word & 0x1f
                size = "X" if op == 0x58 else "W"
                hits.append((off, f"LDR {size}{rt}, [PC+0x{imm19*4:x}]  -> 0x{tgt:x}",
                             struct.pack("<I", word)))
        elif op == 0xb0 or op == 0x90 or op == 0xb0 or op == 0x70:
            pass  # ADRP — different math, skip for now
    return hits

def main():
    pats = gen_pair_patterns()
    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    md.detail = False

    dumps = sorted(glob.glob(os.path.join(DUMP_DIR, "*.bin")))
    print(f"[+] {len(dumps)} dumps; {len(pats)} movz/movk pair patterns")

    for path in dumps:
        name = os.path.basename(path)
        try:
            base = int(name.split("_")[2], 16)
        except Exception:
            base = 0
        with open(path, "rb") as f:
            buf = f.read()
        print(f"\n=== {name}  base=0x{base:x}  size=0x{len(buf):x} ===")

        # Pattern A: MOVZ+MOVK pairs
        movz_hits = 0
        for pat, desc in pats:
            off = 0
            while True:
                idx = buf.find(pat, off)
                if idx < 0: break
                if idx & 3 == 0:
                    movz_hits += 1
                    print(f"  [MOVZ+MOVK] off=0x{idx:x} (abs=0x{base+idx:x})  {desc}")
                    pre_start = max(0, idx - 64)
                    post_end = min(len(buf), idx + 32)
                    for insn in md.disasm(buf[pre_start:post_end], pre_start):
                        marker = " <-- magic" if insn.address == idx else ""
                        print(f"     0x{insn.address:08x}: {insn.mnemonic:8s} "
                              f"{insn.op_str}{marker}")
                    prolog = find_prologue(buf, idx)
                    if prolog is not None:
                        print(f"     prologue off=0x{prolog:x} "
                              f"(abs=0x{base+prolog:x}); magic is +0x{idx-prolog:x}")
                    for gate in find_gates(md, buf, idx, look_back=0x100):
                        g_off, mn, ops, b = gate
                        print(f"     GATE off=0x{g_off:x} (abs=0x{base+g_off:x}): "
                              f"{mn} {ops}  bytes={b.hex()}")
                off = idx + 4

        # Pattern B: literal 0xdead5019 in data — can be loaded via LDR-literal
        lit_hits = 0
        target = struct.pack("<I", 0xdead5019)
        off = 0
        while True:
            idx = buf.find(target, off)
            if idx < 0: break
            if idx & 3 == 0:
                lit_hits += 1
                print(f"\n  [LITERAL 0xdead5019] off=0x{idx:x} (abs=0x{base+idx:x})")
                # Search up to 1 MB before for an LDR-literal pointing here
                ldr_hits = find_ldr_literal_to(buf, idx, look_back=0x100000)
                if not ldr_hits:
                    print("     no LDR-literal references found within 1 MB scan window")
                for ldr_off, ldr_desc, ldr_bytes in ldr_hits:
                    print(f"     LDR-LIT off=0x{ldr_off:x} (abs=0x{base+ldr_off:x}): {ldr_desc}")
                    # Disassemble around the LDR
                    pre_start = max(0, ldr_off - 64)
                    post_end = min(len(buf), ldr_off + 32)
                    for insn in md.disasm(buf[pre_start:post_end], pre_start):
                        marker = " <-- LDR-literal of magic" if insn.address == ldr_off else ""
                        print(f"       0x{insn.address:08x}: {insn.mnemonic:8s} "
                              f"{insn.op_str}{marker}")
                    prolog = find_prologue(buf, ldr_off)
                    if prolog is not None:
                        print(f"       prologue off=0x{prolog:x} "
                              f"(abs=0x{base+prolog:x}); LDR is +0x{ldr_off-prolog:x}")
                    for gate in find_gates(md, buf, ldr_off, look_back=0x100):
                        g_off, mn, ops, b = gate
                        print(f"       GATE off=0x{g_off:x} (abs=0x{base+g_off:x}): "
                              f"{mn} {ops}  bytes={b.hex()}")
            off = idx + 1  # 1-byte step in case literal not aligned

        # Pattern C: 64-bit literal 0x00000000dead5019 (8-byte aligned)
        target64 = struct.pack("<Q", 0xdead5019)
        off = 0
        while True:
            idx = buf.find(target64, off)
            if idx < 0: break
            if idx & 7 == 0:
                print(f"\n  [LITERAL64 0xdead5019] off=0x{idx:x} (abs=0x{base+idx:x})")
                ldr_hits = find_ldr_literal_to(buf, idx, look_back=0x100000)
                if not ldr_hits:
                    print("     no LDR-literal references found within 1 MB")
                for ldr_off, ldr_desc, ldr_bytes in ldr_hits:
                    print(f"     LDR-LIT off=0x{ldr_off:x} (abs=0x{base+ldr_off:x}): {ldr_desc}")
                    pre_start = max(0, ldr_off - 64)
                    post_end = min(len(buf), ldr_off + 32)
                    for insn in md.disasm(buf[pre_start:post_end], pre_start):
                        marker = " <-- LDR-literal of magic" if insn.address == ldr_off else ""
                        print(f"       0x{insn.address:08x}: {insn.mnemonic:8s} "
                              f"{insn.op_str}{marker}")
                    prolog = find_prologue(buf, ldr_off)
                    if prolog is not None:
                        print(f"       prologue off=0x{prolog:x} "
                              f"(abs=0x{base+prolog:x}); LDR is +0x{ldr_off-prolog:x}")
                    for gate in find_gates(md, buf, ldr_off, look_back=0x100):
                        g_off, mn, ops, b = gate
                        print(f"       GATE off=0x{g_off:x} (abs=0x{base+g_off:x}): "
                              f"{mn} {ops}  bytes={b.hex()}")
            off = idx + 1

        if movz_hits == 0 and lit_hits == 0:
            print("  no movz/movk pairs nor 32-bit literal hits.")

if __name__ == "__main__":
    main()
