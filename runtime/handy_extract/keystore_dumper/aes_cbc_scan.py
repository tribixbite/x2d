#!/usr/bin/env python3
"""
Brute-scan a directory for files whose contents AES-CBC-decrypt cleanly
with the legacy flutter_secure_storage AES key recovered from
FlutterSecureKeyStorage.xml.

Two formats are tested per file:
 1. Raw binary: [16 IV][ciphertext, %16==0, PKCS7-padded]
 2. Embedded base64 strings inside XMLs / JSONs: same layout after base64
    decode.

Reports any file/blob whose decrypted plaintext (a) ends in valid PKCS7
padding and (b) contains mostly printable ASCII or a recognizable
file-magic prefix (PEM, DER X.509, RSA private key, etc.).
"""
import base64, os, re, sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

KEY = bytes.fromhex(sys.argv[1] if len(sys.argv) > 1 else "fc1c91f0d74bf994fa3e89e628d4e734")
ROOT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")

def try_decrypt(blob: bytes) -> bytes | None:
    if len(blob) < 32 or len(blob) % 16 != 0:
        return None
    iv, ct = blob[:16], blob[16:]
    try:
        c = Cipher(algorithms.AES(KEY), modes.CBC(iv), backend=default_backend())
        d = c.decryptor()
        pt = d.update(ct) + d.finalize()
    except Exception:
        return None
    pad = pt[-1]
    if pad < 1 or pad > 16: return None
    if pt[-pad:] != bytes([pad]) * pad: return None
    return pt[:-pad]

def looks_useful(pt: bytes) -> str | None:
    if not pt: return None
    if pt.startswith(b"-----BEGIN"): return "PEM"
    if pt[:1] == b"\x30" and pt[1:2] in (b"\x82", b"\x81"): return "DER (ASN.1)"
    # Mostly-printable heuristic
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in pt)
    if printable / len(pt) > 0.92 and len(pt) > 8:
        try: return f"text:{pt[:80].decode('utf-8', 'replace')!r}"
        except: pass
    return None

# Scan: every file's raw content + every base64-looking string inside XML/JSON
B64_RE = re.compile(rb'(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])')
hits = 0
files_scanned = 0
for p in ROOT.rglob("*"):
    if not p.is_file(): continue
    files_scanned += 1
    try: data = p.read_bytes()
    except Exception: continue

    pt = try_decrypt(data)
    if pt:
        tag = looks_useful(pt)
        if tag:
            print(f"[RAW HIT] {p.relative_to(ROOT)} ({len(data)}b) -> {tag}")
            hits += 1

    # base64 strings inside the file
    for m in B64_RE.finditer(data):
        try: blob = base64.b64decode(m.group(0), validate=True)
        except Exception: continue
        pt = try_decrypt(blob)
        if pt:
            tag = looks_useful(pt)
            if tag:
                print(f"[B64 HIT] {p.relative_to(ROOT)} blob={m.group(0)[:40].decode()}... -> {tag}")
                hits += 1

print(f"\n{hits} hits across {files_scanned} files")
