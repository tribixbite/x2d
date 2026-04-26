#!/usr/bin/env python3
"""Crypto roundtrip self-test for x2d_bridge.

Asserts that the same RSA-SHA256 signature the bridge attaches to every
outgoing MQTT publish verifies cleanly with the public half of the
embedded Bambu Connect cert. A regression here means any printer firmware
that actually checks the signature would silently reject our publishes.

Runs on the GitHub Actions runner (x86_64 Ubuntu) — no printer needed.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

# Importing x2d_bridge requires being able to find bambu_cert.py next to it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from x2d_bridge import sign_payload, BAMBU_CERT_ID
from bambu_cert import BAMBU_PRIVATE_KEY_PEM


def main() -> int:
    sample = {
        "print": {
            "command": "stop",
            "param": "",
            "sequence_id": "42",
        }
    }
    signed = sign_payload(dict(sample))

    header = signed.pop("header", None)
    assert header is not None,                "no header"
    assert header["sign_alg"] == "RSA_SHA256", f"alg: {header['sign_alg']}"
    assert header["cert_id"] == BAMBU_CERT_ID, f"cert_id: {header['cert_id']}"
    assert header["sign_ver"] == "v1.0",       f"sign_ver: {header['sign_ver']}"
    assert header["payload_len"] > 0,          "payload_len 0"

    body = json.dumps(sample, separators=(",", ":")).encode()
    assert header["payload_len"] == len(body), \
        f"payload_len {header['payload_len']} != bytes {len(body)}"

    sig = base64.b64decode(header["sign_string"])

    priv = serialization.load_pem_private_key(
        BAMBU_PRIVATE_KEY_PEM.encode(), password=None
    )
    pub = priv.public_key()
    pub.verify(sig, body, padding.PKCS1v15(), hashes.SHA256())
    print("crypto roundtrip OK "
          f"(payload_len={header['payload_len']}, sig_b64_len={len(header['sign_string'])})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
