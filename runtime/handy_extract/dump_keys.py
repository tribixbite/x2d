#!/usr/bin/env python3
"""dump_keys.py — host-side runner for handy_hook.js.

Spawns Bambu Handy under Frida (or attaches if already running), feeds the
hook script in, reassembles RSA private keys from BIGNUM hex dumps, classifies
sniffed AES-decrypt blobs, and writes everything to a timestamped session
directory:

    $XDG_DATA_HOME/x2d/handy_dump/<unix-ts>/
        trace.log         — every send() from the hook
        rsa_<n>.pem       — reconstructed PKCS#8 PEMs (one per unique key seen)
        blob_<n>.bin      — raw AES-cleartext that smells like cert/key
        cert_<n>.pem      — re-formatted PEM if a blob looked like X.509
        SUMMARY.md        — human-readable index with cert subjects/CN/fingerprints

Run after `setup_rooted_device.sh` succeeds. Frida is auto-installed if
missing.

Usage:
    python3 dump_keys.py                      # spawn fresh Handy + trace
    python3 dump_keys.py --attach             # attach to running Handy
    python3 dump_keys.py --device 192.168.1.5:5555  # explicit device
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    import frida
except ImportError:
    sys.exit("frida-tools not installed. Run: pip install frida-tools")

try:
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateNumbers, RSAPublicNumbers
    from cryptography import x509
except ImportError:
    sys.exit("python-cryptography not installed. Run: pip install cryptography")


PKG = "bbl.intl.bambulab.com"
HOOK_JS = Path(__file__).parent / "handy_hook.js"
OUTDIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")) / "x2d" / "handy_dump"


def hex_to_int(s: str) -> int:
    """Decode a possibly-negative hex string from the hook into a Python int."""
    if not s:
        return 0
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    return -int(s, 16) if neg else int(s, 16)


def reconstruct_pkcs8(n_hex, e_hex, d_hex, p_hex, q_hex,
                      dmp1_hex="", dmq1_hex="", iqmp_hex="") -> bytes:
    """Build a PKCS#8-encoded PEM private key from BIGNUM hex strings.
    cryptography's RSAPrivateNumbers requires CRT params; if they were not
    extracted, recompute them from p/q/d."""
    n = hex_to_int(n_hex)
    e = hex_to_int(e_hex) or 65537
    d = hex_to_int(d_hex)
    p = hex_to_int(p_hex)
    q = hex_to_int(q_hex)
    if not (n and d and p and q):
        raise ValueError("missing required RSA params (n/d/p/q)")
    dmp1 = hex_to_int(dmp1_hex) or (d % (p - 1))
    dmq1 = hex_to_int(dmq1_hex) or (d % (q - 1))
    iqmp = hex_to_int(iqmp_hex) or pow(q, -1, p)
    pub = RSAPublicNumbers(e=e, n=n)
    priv = RSAPrivateNumbers(p=p, q=q, d=d, dmp1=dmp1, dmq1=dmq1, iqmp=iqmp,
                             public_numbers=pub)
    key = priv.private_key()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )


class Session:
    def __init__(self) -> None:
        self.dir = OUTDIR / str(int(time.time()))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace = open(self.dir / "trace.log", "w", buffering=1)
        self.keys_seen: set[str] = set()       # dedup by SHA256(n)
        self.blobs_seen: set[str] = set()       # dedup by SHA256(bytes)
        self.key_count = 0
        self.blob_count = 0
        self.cert_count = 0
        self.summary_lines = [
            f"# Bambu Handy dump session {self.dir.name}",
            f"started: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            "",
        ]

    def log(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.trace.write(f"[{ts}] {line}\n")
        print(line, flush=True)

    def handle_rsa_key(self, msg: dict) -> None:
        try:
            pem = reconstruct_pkcs8(
                msg.get("n", ""), msg.get("e", ""), msg.get("d", ""),
                msg.get("p", ""), msg.get("q", ""),
                msg.get("dmp1", ""), msg.get("dmq1", ""), msg.get("iqmp", "")
            )
        except Exception as e:
            self.log(f"  [!] could not reconstruct key (offset probe miss?): {e}")
            return
        n_int = hex_to_int(msg.get("n", ""))
        n_hash = hashlib.sha256(str(n_int).encode()).hexdigest()[:16]
        if n_hash in self.keys_seen:
            return
        self.keys_seen.add(n_hash)
        self.key_count += 1
        path = self.dir / f"rsa_{self.key_count}.pem"
        path.write_bytes(pem)
        path.chmod(0o600)
        # Compute the public-key fingerprint that Bambu's printer uses as cert_id
        priv = serialization.load_pem_private_key(pem, password=None)
        pub_der = priv.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        md5  = hashlib.md5(pub_der).hexdigest()
        sha1 = hashlib.sha1(pub_der).hexdigest()
        keysize = priv.public_key().key_size
        self.log(f"  [+] saved RSA-{keysize} priv key → {path.name}")
        self.log(f"      pubkey MD5  = {md5}")
        self.log(f"      pubkey SHA1 = {sha1}")
        self.summary_lines.append(
            f"## rsa_{self.key_count}.pem (RSA-{keysize}, hook=`{msg.get('mod')}!{msg.get('fn')}`)\n"
            f"- pubkey MD5  : `{md5}`\n"
            f"- pubkey SHA1 : `{sha1}`\n"
            f"- candidate cert_ids the printer might trust:\n"
            f"  - `{md5}CN=GLOF3813734089.bambulab.com`\n"
            f"  - `{sha1}CN=GLOF3813734089.bambulab.com`\n"
        )

    def handle_blob(self, msg: dict) -> None:
        kind = msg.get("kind") or "unknown"
        hex_str = msg.get("bytes_hex") or ""
        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            return
        h = hashlib.sha256(data).hexdigest()[:16]
        if h in self.blobs_seen:
            return
        self.blobs_seen.add(h)
        self.blob_count += 1
        ext = "pem" if kind.endswith("pem") else "der" if kind.endswith("der") else "bin"
        path = self.dir / f"blob_{self.blob_count}_{kind}.{ext}"
        path.write_bytes(data)
        self.log(f"  [+] sniffed AES-decrypt cleartext ({kind}, {len(data)} bytes)"
                 f" → {path.name}")
        # Try to load if it looks like a cert
        if kind.startswith("cert"):
            try:
                if kind == "cert_pem":
                    cert = x509.load_pem_x509_certificate(data)
                else:
                    cert = x509.load_der_x509_certificate(data)
                self.cert_count += 1
                cpath = self.dir / f"cert_{self.cert_count}.pem"
                cpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                self.log(f"      saved as {cpath.name}")
                self.log(f"      subject: {cert.subject.rfc4514_string()}")
                self.log(f"      issuer : {cert.issuer.rfc4514_string()}")
                pub_der = cert.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo)
                md5 = hashlib.md5(pub_der).hexdigest()
                sha1 = hashlib.sha1(pub_der).hexdigest()
                self.log(f"      pubkey MD5  : {md5}")
                self.log(f"      pubkey SHA1 : {sha1}")
                self.summary_lines.append(
                    f"## cert_{self.cert_count}.pem\n"
                    f"- subject : `{cert.subject.rfc4514_string()}`\n"
                    f"- issuer  : `{cert.issuer.rfc4514_string()}`\n"
                    f"- pubkey MD5  : `{md5}`\n"
                    f"- pubkey SHA1 : `{sha1}`\n"
                )
            except Exception as e:
                self.log(f"      cert parse failed: {e}")

    def handle_log(self, msg: str) -> None:
        self.log(msg)

    def finish(self) -> None:
        self.trace.close()
        self.summary_lines.append(
            f"\n---\nfinished: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"keys: {self.key_count}, blobs: {self.blob_count}, certs: {self.cert_count}\n"
        )
        (self.dir / "SUMMARY.md").write_text("\n".join(self.summary_lines))


def on_message(sess: Session, msg, data):
    if msg.get("type") == "error":
        sess.log("[error] " + json.dumps(msg))
        return
    p = msg.get("payload") or {}
    kind = p.get("type")
    if kind == "log":
        sess.handle_log(p.get("msg", ""))
    elif kind == "rsa_key":
        sess.handle_rsa_key(p)
    elif kind == "blob":
        sess.handle_blob(p)
    elif kind == "sign_call":
        sess.log(f"[sign] {p.get('mod')}!{p.get('fn')} tbs={p.get('tbslen')}B")
    else:
        sess.log("[?] " + json.dumps(p)[:200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attach", action="store_true",
                    help="Attach instead of spawn (use if Handy is already running)")
    ap.add_argument("--device", help="Frida device id (USB serial or ip:port)")
    ap.add_argument("--host", help="Remote frida-server host:port "
                                   "(e.g. '127.0.0.1:27042' after `adb forward`)")
    ap.add_argument("--package", default=PKG)
    ap.add_argument("--timeout", type=int, default=0,
                    help="Auto-detach after N seconds (0 = run forever)")
    args = ap.parse_args()

    if args.device:
        # Allow either a frida device id or "host:port" remote.
        if ":" in args.device and "@" not in args.device:
            mgr = frida.get_device_manager()
            try:
                dev = mgr.add_remote_device(args.device)
            except frida.InvalidArgumentError:
                # already added — find it
                dev = next((d for d in mgr.enumerate_devices()
                            if d.id == f"socket@{args.device}" or d.id == args.device),
                           None)
                if dev is None:
                    raise
        else:
            dev = frida.get_device(args.device)
    elif args.host:
        dev = frida.get_device_manager().add_remote_device(args.host)
    else:
        # Default to USB-attached if nothing specified.
        dev = frida.get_usb_device(timeout=10)
    print(f"device: {dev.name} ({dev.id})")

    sess = Session()
    sess.log(f"session dir: {sess.dir}")

    if args.attach:
        proc = next((p for p in dev.enumerate_processes() if p.name == args.package), None)
        if not proc:
            sys.exit(f"package {args.package} is not running. Launch it first.")
        pid = proc.pid
        sess.log(f"attaching to {args.package} pid={pid}")
        session = dev.attach(pid)
    else:
        sess.log(f"spawning {args.package}")
        # Android spawn API takes a single string, NOT a list — passing a
        # list raises "the 'argv' option is not supported when spawning
        # Android apps". Confirmed against frida 17.2.x.
        pid = dev.spawn(args.package)
        session = dev.attach(pid)

    # V8 runtime gives us the Java bridge for okhttp/Conscrypt logging;
    # QuickJS is leaner but Java-less. Default to V8.
    script = session.create_script(HOOK_JS.read_text(), runtime="v8")
    script.on("message", lambda m, d: on_message(sess, m, d))
    script.load()
    if not args.attach:
        dev.resume(pid)

    print("\nHook installed. Drive Bambu Handy now (login + tap your printer +"
          "\ntry pause/resume/light). Ctrl-C to stop and write SUMMARY.md.\n")

    try:
        if args.timeout > 0:
            time.sleep(args.timeout)
        else:
            while True:
                time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except Exception:
            pass
        sess.finish()
        print(f"\nDone. Results in {sess.dir}")


if __name__ == "__main__":
    main()
