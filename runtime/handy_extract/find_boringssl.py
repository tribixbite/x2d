#!/usr/bin/env python3.12
"""
Static analysis of libflutter.so to locate BoringSSL function entry points
inside Flutter's bundled (and otherwise stripped) BoringSSL.

Approach: BoringSSL's .rodata embeds source-path strings for CHECK / abort
diagnostics. Each function in `crypto/x509/x509_vfy.cc` references the
file-path string from inside the function body via an ARM64 ADRP+ADD
instruction pair. By scanning .text for ADRP+ADD/LDR pairs whose computed
target address falls inside one of the source-path string regions, we
obtain a list of (PC inside function, source-file) pairs. Walking each
PC backward to the nearest function prologue (`stp x29, x30, [sp, #-N]!`,
opcode pattern `0xa9..7bfd`) yields the function entry offset.

This map is consumed by handy_hook.js to add per-function Interceptor.attach
hooks against libflutter.so even though the symbols are not exported.

Outputs JSON to stdout / argv[2].

Usage:
  python3.12 find_boringssl.py libflutter.so out.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
from elftools.elf.elffile import ELFFile

# Source-path tokens we care about — each maps to the BoringSSL functions
# whose entry we want to discover. The token is matched as a substring
# inside the .rodata path string at the xref target.
TOKENS_OF_INTEREST = {
    "ssl/handshake_client.cc": ["SSL_do_handshake", "ssl_run_handshake"],
    "ssl/s3_pkt.cc":           ["SSL_read", "SSL_write", "ssl3_read_app_data", "ssl3_write_app_data"],
    "ssl/ssl_lib.cc":          ["SSL_read", "SSL_write", "SSL_get_error"],
    "ssl/ssl_cert.cc":         ["ssl_check_client_certificate_signature"],
    "ssl/ssl_privkey.cc":      ["ssl_private_key_sign"],
    "crypto/x509/x509_vfy.cc": ["X509_verify_cert", "X509_verify_cert_chain"],
    "crypto/x509/a_verify.cc": ["ASN1_item_verify"],
    "crypto/rsa/rsa_crypt.cc": ["RSA_sign", "RSA_verify", "RSA_private_encrypt"],
    "crypto/evp/evp_ctx.cc":   ["EVP_PKEY_sign", "EVP_PKEY_verify"],
    "crypto/evp/p_rsa.cc":     ["pkey_rsa_sign"],
    "crypto/evp/digestsign.cc":["EVP_DigestSignFinal", "EVP_DigestVerifyFinal"],
    "ssl/ssl_aead_ctx.cc":     ["AEAD_open", "AEAD_seal"],
}


def find_string_xrefs(elf: ELFFile, raw: bytes, tokens: list[str]) -> dict[int, str]:
    """Locate string addresses in .rodata whose body contains a target token."""
    rodata = next((s for s in elf.iter_sections() if s.name == ".rodata"), None)
    if rodata is None:
        raise SystemExit("no .rodata in this ELF")
    base = rodata["sh_addr"]
    body = rodata.data()
    locations: dict[int, str] = {}
    # Walk null-terminated strings, record their start virtual address.
    pos = 0
    n = len(body)
    while pos < n:
        end = body.find(b"\x00", pos)
        if end < 0:
            break
        s = body[pos:end].decode("ascii", errors="ignore")
        for tok in tokens:
            if tok in s:
                locations[base + pos] = s
                break
        pos = end + 1
    return locations


def disas_text(elf: ELFFile) -> tuple[int, bytes]:
    text = next((s for s in elf.iter_sections() if s.name == ".text"), None)
    if text is None:
        raise SystemExit("no .text in this ELF")
    return text["sh_addr"], text.data()


def find_xrefs_to(text_base: int, text_bytes: bytes,
                  string_addrs: set[int]) -> dict[int, int]:
    """For every reference to one of our target string addresses, return
    {ref_pc: string_addr}.

    Handles three ARM64 patterns the compiler may emit for "load address
    of __FILE__ string":

      1. ADRP + ADD (RIP-relative computation)
              adrp x0, #PAGE
              add  x0, x0, #PAGE_OFF
      2. ADRP + LDR <reg>, [<reg>, #imm]  (load via .got or .data.rel.ro)
              adrp x0, #PAGE
              ldr  x0, [x0, #SLOT]
      3. Literal pool LDR  (compiler emits the 8-byte address into the
         function's trailing data island and references it PC-relatively)
              ldr  x0, [pc, #LITPOOL_OFFSET]
         Capstone resolves this to "ldr x0, #<target_addr>".

    For pattern 3, we ALSO scan the raw .text bytes for 8-byte aligned
    occurrences of each target string address — those positions ARE
    literal pool entries that some `ldr` instruction references. This
    is a fallback that finds references the disassembler-walking
    approach might miss (literal pools occasionally land in odd places).
    """
    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    md.detail = False
    pending: dict[str, int] = {}
    found: dict[int, int] = {}

    # Pre-scan: find literal-pool occurrences of the string addresses by
    # treating .text as raw bytes. For each 8-byte aligned position whose
    # u64 value matches a target string, remember it — we'll later scan
    # for `ldr <reg>, #target_litpool_addr` instructions that consume it.
    litpool_addrs: dict[int, int] = {}  # pool_va -> string_addr
    for offset in range(0, len(text_bytes) - 7, 4):
        # Try u64 at this offset
        v = int.from_bytes(text_bytes[offset:offset + 8], "little")
        if v in string_addrs:
            litpool_addrs[text_base + offset] = v

    for ins in md.disasm(text_bytes, text_base):
        # Pattern 3: literal-pool ldr — Capstone resolves these directly
        if ins.mnemonic == "ldr" and ins.op_str.count(",") == 1:
            # "ldr x0, #0x500abc"  (PC-relative literal load)
            try:
                parts = [p.strip() for p in ins.op_str.split(",")]
                if parts[1].startswith("#"):
                    target_va = int(parts[1].lstrip("#"), 0)
                    if target_va in litpool_addrs:
                        found[ins.address] = litpool_addrs[target_va]
                        continue
            except Exception:
                pass

        if ins.mnemonic == "adrp":
            try:
                reg, target = ins.op_str.split(", ")
                pending[reg.strip()] = int(target.lstrip("#"), 0)
            except Exception:
                pass
        elif ins.mnemonic in ("add", "ldr") and ins.op_str.count(",") >= 2:
            parts = [p.strip() for p in ins.op_str.split(",")]
            try:
                if ins.mnemonic == "add" and parts[1] == parts[0]:
                    base = pending.get(parts[0])
                    if base is not None:
                        offs = int(parts[2].lstrip("#"), 0)
                        target = base + offs
                        if target in string_addrs:
                            found[ins.address] = target
                elif ins.mnemonic == "ldr":
                    if len(parts) >= 3 and parts[1].startswith("["):
                        reg = parts[1].lstrip("[").rstrip("]").strip()
                        if reg.endswith(","):
                            reg = reg[:-1].strip()
                        offs_token = parts[2].rstrip("]").strip()
                        base = pending.get(reg)
                        if base is not None and offs_token.startswith("#"):
                            offs = int(offs_token.lstrip("#"), 0)
                            target = base + offs
                            if target in string_addrs:
                                found[ins.address] = target
            except Exception:
                pass
    return found


def find_function_entries(text_base: int, text_bytes: bytes,
                          xref_pcs: list[int]) -> dict[int, int]:
    """For each xref PC, walk backward looking for the closest stp x29, x30
    instruction (function prologue marker on ARM64). Return {xref_pc:
    function_entry_addr}. Returns 0 entry if not found within 4 KB."""
    out: dict[int, int] = {}
    text_end = text_base + len(text_bytes)
    for pc in xref_pcs:
        # Walk back up to 1024 instructions (4 KB) checking for the
        # function-entry signature: STP X29,X30,[SP,#-N]! (pre-indexed).
        # ARM64 encoding: 0xa9bn7bfd — bits 31..22 = 1010100110, where
        # bit 23..15 covers immediate. Mask 0xff_c0_7c_00 detects:
        #   0xa9_80_7b_fd  STP X29,X30,[SP,#-N]!
        # Sanity-aware: also accept SUB SP, SP, #N (stack alloc) which
        # prologues often start with for larger frames. Most BoringSSL
        # functions use the STP form.
        cursor = pc - 4
        floor = max(text_base, pc - 0x4000)
        while cursor >= floor:
            off = cursor - text_base
            if off + 4 > len(text_bytes):
                break
            insn = int.from_bytes(text_bytes[off:off + 4], "little")
            # STP X29, X30, [SP, #-N]!  — pre-indexed
            #   sf=1, opc=10, V=0, L=0, base=11111 (sp), Rt=11101, Rt2=11110
            #   imm7 in bits 21..15
            # Encoding mask: 0xff c0 7f ff ?  Easier: check the recognizable
            # pattern of `a9 b? 7b fd` (LE bytes). i.e. byte 3 == 0xa9 and
            # (byte 2 & 0xc0) == 0x80 and bytes 1..0 == 0x7bfd.
            b0 = insn & 0xff
            b1 = (insn >> 8) & 0xff
            b2 = (insn >> 16) & 0xff
            b3 = (insn >> 24) & 0xff
            if b3 == 0xa9 and (b2 & 0xc0) == 0x80 and b0 == 0xfd and b1 == 0x7b:
                out[pc] = cursor
                break
            cursor -= 4
        else:
            out[pc] = 0
    return out


def main():
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} libflutter.so [out.json]")
        sys.exit(2)
    libpath = Path(sys.argv[1])
    raw = libpath.read_bytes()
    with libpath.open("rb") as fp:
        elf = ELFFile(fp)
        text_base, text_bytes = disas_text(elf)
        # Aggregate all tokens we care about
        all_tokens = sorted({tok for v in TOKENS_OF_INTEREST.keys() for tok in [v]})
        # Find their string addresses in .rodata
        string_addrs = find_string_xrefs(elf, raw, all_tokens)

    print(f"# .text base 0x{text_base:x} size 0x{len(text_bytes):x}", file=sys.stderr)
    print(f"# {len(string_addrs)} relevant rodata strings located", file=sys.stderr)
    for a, s in sorted(string_addrs.items()):
        print(f"   0x{a:x}: {s}", file=sys.stderr)

    xrefs = find_xrefs_to(text_base, text_bytes, set(string_addrs.keys()))
    print(f"# {len(xrefs)} xref instructions found", file=sys.stderr)

    entries = find_function_entries(text_base, text_bytes, list(xrefs.keys()))

    # Group by function entry → set of (string, source_path)
    by_entry: dict[int, dict] = defaultdict(lambda: {"strings": set(), "xref_count": 0})
    for pc, str_addr in xrefs.items():
        entry = entries.get(pc, 0)
        by_entry[entry]["xref_count"] += 1
        by_entry[entry]["strings"].add(string_addrs[str_addr])

    out = []
    for entry, info in sorted(by_entry.items()):
        if entry == 0:
            continue
        # Collapse strings into the dominant source-file token
        seen_tokens = set()
        for s in info["strings"]:
            for tok in TOKENS_OF_INTEREST:
                if tok in s:
                    seen_tokens.add(tok)
        # Pick the most-specific token (longest match) as the "source_file"
        if not seen_tokens:
            continue
        primary = sorted(seen_tokens, key=len, reverse=True)[0]
        # Suggest function names from the lookup
        suggested = TOKENS_OF_INTEREST.get(primary, [])
        out.append({
            "entry_offset": f"0x{entry:x}",
            "entry_addr_decimal": entry,
            "xref_count": info["xref_count"],
            "source_file": primary,
            "candidate_names": suggested,
            "source_files_seen": sorted(seen_tokens),
        })

    print(f"# {len(out)} candidate function entries identified", file=sys.stderr)
    output_path = sys.argv[2] if len(sys.argv) >= 3 else "-"
    body = json.dumps(out, indent=2)
    if output_path == "-":
        print(body)
    else:
        Path(output_path).write_text(body)
        print(f"# wrote {output_path} ({len(out)} entries)", file=sys.stderr)


if __name__ == "__main__":
    main()
