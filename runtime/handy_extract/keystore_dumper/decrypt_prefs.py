#!/usr/bin/env python3
"""Decrypt FlutterSecureStorage.xml end-to-end (names AND values) given
the raw Tink keys exported by SecureStorageDumper.

The Java helper can only decrypt VALUES (AES-GCM via JCE). Decrypting
the entry NAMES needs AES-SIV which JCE doesn't ship — pycryptodome does.
This script glues the two halves together and emits a readable
{plaintext_key: plaintext_value} map so you can tell *what* each
encrypted prefs entry actually controls.

Usage:
    runtime/handy_extract/keystore_dumper/run.sh com.x2d.dump.SecureStorageDumper \\
        > /tmp/dump.txt
    python3 decrypt_prefs.py /tmp/dump.txt \\
        /data/data/com.termux/files/usr/tmp/curr_bambu/shared_prefs/FlutterSecureStorage.xml
"""
from __future__ import annotations

import base64
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from Crypto.Cipher import AES


def parse_dump(path: Path) -> tuple[bytes, bytes]:
    txt = path.read_text()
    siv = re.search(r"key AesSiv key recovered: \d+ bytes hex=([0-9a-f]+)", txt)
    gcm = re.search(r"value AesGcm key hex=([0-9a-f]+)", txt)
    if not siv or not gcm:
        sys.exit(f"could not find SIV/GCM key hex lines in {path}")
    return bytes.fromhex(siv.group(1)), bytes.fromhex(gcm.group(1))


def aes_siv_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    """Tink's deterministic AES-SIV: ciphertext = SIV-tag(16) || ctr-ciphertext.

    AndroidX EncryptedSharedPreferences passes the SharedPreferences *file
    name* (UTF-8 bytes) as the associated-data for KEY encryption. Without
    that AAD the SIV tag won't verify and decrypt_and_verify raises
    "MAC check failed". Strip Tink's 5-byte output prefix first
    (0x01 + 4-byte big-endian key_id).
    """
    if blob[:1] == b"\x01":
        blob = blob[5:]
    cipher = AES.new(key, AES.MODE_SIV)
    cipher.update(aad)
    return cipher.decrypt_and_verify(blob[16:], blob[:16])


def aes_gcm_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if blob[:1] == b"\x01":
        blob = blob[5:]
    iv, ct = blob[:12], blob[12:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    cipher.update(aad)
    return cipher.decrypt_and_verify(ct[:-16], ct[-16:])


# Flutter EncryptedSharedPreferences value layout: 1-byte type + 4-byte
# big-endian length + payload.
PREF_TYPES = {
    0: "unknown",  # observed in wild
    1: "stringSet",
    2: "string",
    3: "int",
    4: "long",
    5: "float",
    6: "bool",
}


def decode_value(plain: bytes) -> tuple[str, str]:
    if not plain:
        return "empty", ""
    t = plain[0]
    if t == 0 and len(plain) >= 8:
        # type=0 (used by both stockEncryptedSharedPreferences AND the Flutter
        # plugin) lays out [type 1B][padding 0..7][len 4B big-endian][bytes].
        # The 7 zero bytes we see in raw output mean the length field starts
        # at offset 4, not 1.
        ln = struct.unpack(">I", plain[4:8])[0]
        if 8 + ln <= len(plain):
            return "string?", plain[8:8 + ln].decode("utf-8", "replace")
    return PREF_TYPES.get(t, f"type{t}"), plain[1:].decode("utf-8", "replace")


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit("usage: decrypt_prefs.py <java-dump.txt> <FlutterSecureStorage.xml>")
    siv_key, gcm_key = parse_dump(Path(sys.argv[1]))
    xml_path = Path(sys.argv[2])
    print(f"# SIV key: {siv_key.hex()}")
    print(f"# GCM key: {gcm_key.hex()}")
    tree = ET.parse(xml_path)
    root = tree.getroot()
    out: dict[str, str] = {}
    for el in root.findall("string"):
        enc_name = el.attrib["name"]
        enc_val = (el.text or "").strip()
        # Skip the Tink keyset metadata entries.
        if enc_name.startswith("__androidx_security_crypto_"):
            continue
        try:
            # The XML file basename minus ".xml" is the SharedPreferences
            # name and is the AAD for SIV key encryption.
            siv_aad = xml_path.stem.encode("utf-8")
            name = aes_siv_decrypt(siv_key, base64.b64decode(enc_name), siv_aad).decode("utf-8")
        except Exception as ex:
            name = f"<siv-decrypt-failed:{ex}>"
        try:
            plain = aes_gcm_decrypt(gcm_key, base64.b64decode(enc_val), enc_name.encode("utf-8"))
            ptype, value = decode_value(plain)
            out[name] = (ptype, value)
        except Exception as ex:
            out[name] = ("<gcm-decrypt-failed>", str(ex))

    print(f"\n# {len(out)} entries decrypted (name -> (type, value)):")
    for k, (typ, v) in sorted(out.items()):
        truncated = v if len(v) <= 200 else v[:200] + f"…[len={len(v)}]"
        print(f"  {k!r:40} ({typ:10}) = {truncated!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
