#!/usr/bin/env python3
"""Bambu Connect signing key — publicly leaked, used by all open-source
Bambu LAN clients (orca-lan-bridge, bambulab-go, etc.).

X2D / H2D / refreshed P1+X1 firmwares (Jan-2025+) require every MQTT
publish to carry an RSA-SHA256 signature in a top-level `header` block,
verified against this certificate's public half. Without it the printer
returns `err_code 84033543 "mqtt message verify failed"` and ignores the
command. The cert leaked when the Bambu Connect plugin shipped without
obfuscating the private key from its on-disk JSON config.

If Bambu ever rotates and revokes this key, every LAN client breaks at
once. There is no good way around it short of finding the new cert in
the next plugin release.

cert_id used by the firmware: GLOF3813734089-524a37c80000c6a6a274a47b3281

== CLI ==

  $ python3 bambu_cert.py validate
        Sends a signed `pushing.pushall` to the [printer] in
        ~/.x2d/credentials and reports if the firmware acked. Exit 0
        on success; non-zero (with err_code if firmware sent one) on
        rejection. Cron monthly to alert on cert rotation BEFORE a
        real print silently fails:
            0 6 1 * *  python3 ~/git/x2d/bambu_cert.py validate \
                       --silent || systemd-cat -p err -t bambu-cert

  $ python3 bambu_cert.py validate --printer X2D2
        Pick a [printer:NAME] section. Multi-printer accounts can
        validate every bound printer with `for n in $(x2d_bridge.py
        printers); do bambu_cert.py validate --printer "$n"; done`.

  $ python3 bambu_cert.py validate --json
        Machine-readable for monitoring stacks. Exit code matches.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


BAMBU_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
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


# ---------------------------------------------------------------------------
# Cert-rotation monitor (item #74).
#
# Sends a single signed `pushing.pushall` to the configured printer over
# the LAN-direct MQTT broker and reports if the firmware acked with
# state. A silent timeout (no state push back, or err_code=84033543 on
# the printer's own report stream) means our embedded Bambu Connect cert
# has been rotated/revoked and every LAN client will stop working.
# ---------------------------------------------------------------------------

# Canonical here — re-exported by x2d_bridge.py to keep its existing
# import surface backwards-compatible.
BAMBU_CERT_ID = "GLOF3813734089-524a37c80000c6a6a274a47b3281"


def _validate(printer_section: str, timeout: float, json_out: bool, silent: bool) -> int:
    # Defer the heavy imports so `import bambu_cert` from a test stays cheap.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import x2d_bridge  # noqa: WPS433

    class _A:
        ip = ""
        code = ""
        serial = ""
        printer = printer_section

    try:
        creds = x2d_bridge.Creds.resolve(_A())
    except SystemExit as e:
        if json_out:
            print(json.dumps({"ok": False, "stage": "creds", "error": str(e)}))
        elif not silent:
            print(f"validate: cannot resolve credentials: {e}", file=sys.stderr)
        return 2

    cli = x2d_bridge.X2DClient(creds)
    started = time.time()
    err_code: int | None = None
    err_stage = ""
    state_seen = False
    try:
        cli.connect(timeout=min(8.0, timeout))
        try:
            state = cli.request_state(timeout=timeout)
            state_seen = bool(state)
            # If the printer fired an err_code in its report stream, surface it.
            err = (state or {}).get("print", {}).get("command_err")
            if isinstance(err, int) and err != 0:
                err_code = err
                err_stage = "command_err"
        except TimeoutError as e:
            err_stage = "timeout"
            err_code = 84033543  # the verify-failed code firmware uses
            if not silent and not json_out:
                print(f"validate: TIMEOUT after {timeout:.0f}s — likely cert rotation",
                      file=sys.stderr)
    except Exception as e:
        err_stage = "connect"
        if not silent and not json_out:
            print(f"validate: connect/publish failed: {e}", file=sys.stderr)
        err_code = -1
    finally:
        try:
            cli.disconnect()
        except Exception:
            pass

    elapsed = time.time() - started
    ok = state_seen and err_code is None
    payload = {
        "ok":        ok,
        "printer":   creds.serial,
        "ip":        creds.ip,
        "elapsed_s": round(elapsed, 2),
        "cert_id":   BAMBU_CERT_ID,
    }
    if err_code is not None:
        payload["err_code"] = err_code
        payload["err_stage"] = err_stage
    if json_out:
        print(json.dumps(payload))
    elif not silent:
        if ok:
            print(f"validate: OK — {creds.serial} acked signed pushall in {elapsed:.1f}s")
        else:
            print(f"validate: FAIL — printer={creds.serial} stage={err_stage} "
                  f"err_code={err_code} elapsed={elapsed:.1f}s", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="bambu_cert.py",
        description="Bambu Connect cert tools — currently `validate`.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser(
        "validate",
        help="Probe a printer with a signed pushall and report whether "
             "the embedded Bambu Connect cert is still accepted.",
        description="Sends one signed pushing.pushall to the printer and "
                    "waits for the next state report. Useful as a cron "
                    "monitor: a sudden FAIL means Bambu rotated the cert "
                    "and every LAN client just broke.",
    )
    v.add_argument("--printer", default="",
                   help="Section name in ~/.x2d/credentials (default = "
                        "[printer]).")
    v.add_argument("--timeout", type=float, default=15.0,
                   help="Seconds to wait for the printer's state report (default 15).")
    v.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human text.")
    v.add_argument("--silent", action="store_true",
                   help="Suppress non-JSON output (exit code only). For cron use.")
    args = ap.parse_args()

    if args.cmd == "validate":
        return _validate(args.printer, args.timeout, args.json, args.silent)
    return 2


if __name__ == "__main__":
    sys.exit(main())
