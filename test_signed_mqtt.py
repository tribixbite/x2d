#!/usr/bin/env python3
"""Probe whether the X2D's MQTT verifier accepts our payload when signed
with the publicly-leaked Bambu Connect RSA key (Jan 2025 disclosure;
Hackaday + consumerrights.wiki). Read-only test: sends only `pushing.pushall`
which asks the printer to re-push its full state — never starts a print.

Background — see also lan_print.py + the agent research note in the task list.
The X2D rejects unsigned commands with err_code 84033543 ("mqtt message
verify failed"). bambulabs_api 2.6.6 doesn't sign, hence its start_print fails.
The desktop Bambu Studio + Bambu Connect both sign with one global RSA key
embedded in the bambu_networking plugin; that key has been public since
Jan 2025 and current X2D firmware still accepts it.

Usage:
    python3 test_signed_mqtt.py [--ip ...] [--access-code ...] [--serial ...]

Exits 0 on visible state response, 1 on still-rejected, 2 on connect failure.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import ssl
import sys
import time

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# Verbatim from schwarztim/bambu-mcp/src/index.ts and heyixuan2/bambu-studio-ai —
# this is the Bambu Connect signing cert ID that the X2D firmware (April 2026)
# accepts. The leading 32 hex chars are the cert SHA-256 fingerprint, the suffix
# is the embedded cert's CN.
CERT_ID = "GLOF3813734089-524a37c80000c6a6a274a47b3281"

# Leaked PRIVATE KEY (from disclosed bambu_networking plugin). Documented
# publicly since Jan 2025 and republished in dozens of bambu-* projects.
LEAKED_PRIVKEY_PEM = b"""-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDQNp2NfkajwcWH
PIqosa08P1ZwETPr1veZCMqieQxWtYw97wp+JCxX4yBrBcAwid7o7PHI9KQVzPRM
f0uXspaDUdSljrfJ/YwGEz7+GJz4+ml1UbWXBePyzXW1+N2hIGGn7BcNuA0v8rMY
uvVgiIIQNjLErgGcCWmMHLwsMMQ7LNprUZZKsSNB4HaQDH7cQZmYBN/O45np6l+K
VuLdzXdDpZcOM7bNO6smev822WPGDuKBo1iVfQbUe10X4dCNwkBR3QGpScVvg8gg
tRYZDYue/qc4Xaj806RZPttknWfxdvfZgoOmAiwnyQ5K3+mzNYHgQZAOC2ydkK4J
s+ZizK3lAgMBAAECggEAKwEcyXyrWmdLRQNcIDuSbD8ouzzSXIOp4BHQyH337nDQ
5nnY0PTns79VksU9TMktIS7PQZJF0brjOmmQU2SvcbAVG5y+mRmlMhwHhrPOuB4A
ahrWRrsQubV1+n/MRttJUEWS/WJmVuDp3NHAnI+VTYPkOHs4GeJXynik5PutjAr3
tYmr3kaw0Wo/hYAXTKsI/R5aenC7jH8ZSyVcZ/j+bOSH5sT5/JY122AYmkQOFE7s
JA0EfYJaJEwiuBWKOfRLQVEHhOFodUBZdGQcWeW3uFb88aYKN8QcKTO8/f6e4r8w
QojgK3QMj1zmfS7xid6XCOVa17ary2hZHAEPnjcigQKBgQDQnm4TlbVTsM+CbFUS
1rOIJRzPdnH3Y7x3IcmVKZt81eNktsdu56A4U6NEkFQqk4tVTT4TYja/hwgXmm6w
J+w0WwZd445Bxj8PmaEr6Z/NSMYbCsi8pRelKWmlIMwD2YhtY/1xXD37zpOgN8oQ
ryTKZR2gljbPxdfhKS7YerLp2wKBgQD/gJt3Ds69j1gMDLnnPctjmhsPRXh7PQ0e
E9lqgFkx/vNuCuyRs6ymic2rBZmkdlpjsTJFmz1bwOzIvSRoH6kp0Mfyo6why5kr
upDf7zz+hlvaFewme8aDeV3ex9Wvt73D66nwAy5ABOgn+66vZJeo0Iq/tnCwK3a/
evTL9BOzPwKBgEUi7AnziEc3Bl4Lttnqa08INZcPgs9grzmv6dVUF6J0Y8qhxFAd
1Pw1w5raVfpSMU/QrGzSFKC+iFECLgKVCHOFYwPEgQWNRKLP4BjkcMAgiP63QTU7
ZS2oHsnJp7Ly6YKPK5Pg5O3JVSU4t+91i7TDc+EfRwTuZQ/KjSrS5u4XAoGBAP06
v9reSDVELuWyb0Yqzrxm7k7ScbjjJ28aCTAvCTguEaKNHS7DP2jHx5mrMT35N1j7
NHIcjFG2AnhqTf0M9CJHlQR9B4tvON5ISHJJsNAq5jpd4/G4V2XTEiBNOxKvL1tQ
5NrGrD4zHs0R+25GarGcDwg3j7RrP4REHv9NZ4ENAoGAY7Nuz6xKu2XUwuZtJP7O
kjsoDS7bjP95ddrtsRq5vcVjJ04avnjsr+Se9WDA//t7+eSeHjm5eXD7u0NtdqZo
WtSm8pmWySOPXMn9QQmdzKHg1NOxer//f1KySVunX1vftTStjsZH7dRCtBEePcqg
z5Av6MmEFDojtwTqvEZuhBM=
-----END PRIVATE KEY-----"""

PRIV = serialization.load_pem_private_key(LEAKED_PRIVKEY_PEM, password=None)


def sign_payload(payload: dict, user_id: str = "") -> str:
    """Wrap `payload` with a `header` block carrying an RSA-SHA256 signature
    over the un-headered JSON. Returns the compact JSON to publish.
    """
    body = dict(payload)
    if user_id:
        body["user_id"] = user_id
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    sig = PRIV.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    body["header"] = {
        "sign_ver":    "v1.0",
        "sign_alg":    "RSA_SHA256",
        "sign_string": base64.b64encode(sig).decode("ascii"),
        "cert_id":     CERT_ID,
        "payload_len": len(raw),
    }
    return json.dumps(body, separators=(",", ":"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True,
                    help="Printer LAN IP (e.g. 192.168.x.y)")
    ap.add_argument("--access-code", required=True,
                    help="Printer 8-char LAN access code from the printer screen")
    ap.add_argument("--serial", required=True,
                    help="Printer serial (printed on the device sticker)")
    ap.add_argument("--user-id", default="",
                    help="Optional Bambu numeric user_id; signed BEFORE the header is attached")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("test_signed_mqtt")

    seen_verify_fail = False
    seen_state_push = False
    last_msg = ""

    def on_connect(_c, _u, _f, rc, *args):
        log.info("MQTT connected: rc=%s", rc)

    def on_message(_c, _u, msg):
        nonlocal seen_verify_fail, seen_state_push, last_msg
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            return
        last_msg = payload[:600]
        if "84033543" in payload or "mqtt message verify failed" in payload:
            seen_verify_fail = True
            log.warning("Verify-failed echo: %s", payload[:300])
        elif '"print":' in payload and ('"ams":' in payload or '"nozzle_temper"' in payload):
            seen_state_push = True

    c = mqtt.Client(client_id=f"x2d-test-{int(time.time())}",
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set("bblp", args.access_code)
    c.tls_set(cert_reqs=ssl.CERT_NONE)
    c.tls_insecure_set(True)
    c.on_connect = on_connect
    c.on_message = on_message

    try:
        c.connect(args.ip, 8883, 60)
    except Exception as e:
        log.error("connect: %s", e)
        return 2
    c.loop_start()
    c.subscribe(f"device/{args.serial}/report", qos=0)
    time.sleep(1.5)

    payload = {"pushing": {"sequence_id": "100020001",
                           "command": "pushall",
                           "version": 1,
                           "push_target": 1}}
    signed = sign_payload(payload, user_id=args.user_id)
    log.info("Publishing signed pushall (%d bytes) to device/%s/request",
             len(signed), args.serial)
    info = c.publish(f"device/{args.serial}/request", signed, qos=0)
    log.info("publish rc=%s mid=%s", info.rc, info.mid)

    deadline = time.time() + args.timeout
    while time.time() < deadline and not seen_verify_fail:
        time.sleep(0.2)
    c.loop_stop()
    c.disconnect()

    if seen_verify_fail:
        log.error("Signature was REJECTED. cert_id or payload format is wrong.")
        log.error("Last echo: %s", last_msg)
        return 1
    if seen_state_push:
        log.info("Signature ACCEPTED — got a real state push back. ✓")
        return 0
    log.warning("No verify-failed echo and no obvious state push within %s s. "
                "Sometimes a state push lags; rerun with --timeout 30 to confirm.",
                args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
