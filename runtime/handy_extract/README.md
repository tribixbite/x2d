# Bambu Handy key extraction (rooted-Android Frida hook)

Goal: recover the per-installation X.509 cert + RSA private key the Bambu
Handy Android app uses to sign LAN MQTT publishes against an X-series Bambu
printer, so our `x2d_bridge.py` can sign LAN `print.*` commands without
going through Bambu's cloud.

## Why this isn't already done elsewhere

- Static APK extraction failed on Bambu Handy v3.19.0: `libapp.so` is Flutter
  Dart AOT, all strings encrypted in-snapshot; `assets/l6a18f19c_a64.so` is a
  packer loader stub (closest match: Promon SHIELD ≥7.0 + Tencent Tinker
  hot-patch via `assets/patch.dex`); `assets/kqkticwjgzy.dat` is the
  encrypted payload, decoded only at runtime.
- Bambu's desktop plugin `libbambu_networking.so` is fully Virbox-protected.
- The Jan-2025 Bambu Connect cert leak doesn't help — the printer's trust
  list (`security.app_cert_list`) on firmware 01.01.00.00 doesn't include
  it. Per-installation Handy certs have not publicly leaked.

## What this does instead

Hooks every plausible signing primitive (`EVP_PKEY_sign`, `RSA_sign`,
`RSA_private_encrypt`, `EVP_PKEY_get1_RSA`, `mbedtls_pk_sign`) and every
plausible AES decrypt that could carry a PEM/DER-wrapped cert/key
(`EVP_DecryptUpdate`, `EVP_DecryptFinal_ex`, `mbedtls_aes_crypt_cbc`)
inside the running Handy process, walks the in-memory RSA/AES context bytes
out, and reconstructs PKCS#8 PEMs on the host.

Uses `hzzheyang/strongR-frida-android` (anti-detect Frida) because the
packer scans for vanilla `frida-server` symbols + process names.

## Files

| File | Purpose |
|---|---|
| `setup_rooted_device.sh` | One-shot: pushes StrongR-Frida server, installs Bambu Handy from your local backup tarball, exposes :27042 via `adb forward`. |
| `handy_hook.js` | The Frida script — hooks crypto, sniffs decrypts, emits structured events. |
| `dump_keys.py` | Host runner — feeds the hook in, reassembles PKCS#8 PEMs from BIGNUM hex, classifies sniffed blobs, writes a session dir. |
| `cache/` | Cached frida-server binaries (gitignored). |

## Runbook

```bash
# 1) Plug in the rooted device (or `adb connect <ip:port>` over WiFi).
adb devices

# 2) Bootstrap. Idempotent — re-runnable.
./setup_rooted_device.sh

# 3) Launch Bambu Handy on the device, log in.

# 4) From the host, attach:
python3 dump_keys.py --attach
# (or omit --attach to spawn fresh).

# 5) On the device: tap your printer in the device list, try pause / resume
# / light toggle / send-print. Each operation that touches LAN-MQTT will
# fire one or more hooks and emit `rsa_key` / `blob` events.

# 6) Ctrl-C. Output lands in:
ls ~/.local/share/x2d/handy_dump/<unix-ts>/
#   trace.log
#   rsa_1.pem        ← the private key we want
#   cert_1.pem       ← matching X.509 (if sniffed via AES decrypt)
#   SUMMARY.md       ← cert subjects, fingerprints, candidate cert_ids

# 7) Wire into our bridge:
cp ~/.local/share/x2d/handy_dump/<ts>/rsa_1.pem  ~/.x2d/bambu_app.key
cp ~/.local/share/x2d/handy_dump/<ts>/cert_1.pem ~/.x2d/bambu_app.crt
# Then tell sign_payload() to load these instead of bambu_cert.py's hardcoded leak.
```

## What success looks like

`SUMMARY.md` contains a section like:

```markdown
## rsa_1.pem (RSA-2048, hook=`libcrypto.so!EVP_PKEY_sign`)
- pubkey MD5  : `77bcfb6303214f046175eb6681a46d83`
- pubkey SHA1 : `…`
- candidate cert_ids the printer might trust:
  - `77bcfb6303214f046175eb6681a46d83CN=GLOF3813734089.bambulab.com`
```

If MD5 matches one of the X2D's `app_cert_list` entries (which we already
know are `4a63…` and `77bcfb…`), we have a key whose pubkey is in the
factory trust list. Sign with that key + that cert_id and `print.*`
should clear `84033545/47/48` to `result: success`.

## If it doesn't work

In rough order, things that could go wrong and how to diagnose them:

1. **frida-server crashes immediately on launch.** Packer detected the
   server. Try a newer StrongR release: `FRIDA_VER=16.6.x ./setup_rooted_device.sh`.
   Fall back to renaming the on-disk binary and TCP port:
   `adb shell su -c '/data/local/tmp/frida-server -l 0.0.0.0:9999'`.
2. **`dump_keys.py` exits with `_frida.ProcessNotFoundError`.** App is in
   tamper-detect kill-loop. Disable Magisk's MagiskHide for the package, or
   re-launch via `dump_keys.py` (no `--attach`) so we spawn pre-init.
3. **No `rsa_key` events fire even when the app signs.** The packer
   relocates libcrypto symbols, or signing happens in a statically-linked
   crypto blob inside `libapp.so`. Drop `Module.findExportByName` and use
   the SensePost pattern-scan technique against the Dart AOT — find the
   sign primitive by byte signature: PKCS#1v15 padding produces a
   distinctive `00 01 ff ff ff … 00` prefix that's emitted just before
   the modular exponentiation.
4. **Hook fires but `n/d/p/q` are empty.** The RSA struct offset probe
   missed; the loop tries 16/24/32/40/48 — extend if needed. Latest
   BoringSSL puts BIGNUMs at offset 16 from the RSA* (after the refs +
   ENGINE pointer); OpenSSL 3.x uses a different layout via providers.
5. **AES decrypts emit nothing.** App is using libsodium / ChaCha20 instead
   of AES. Add hooks for `crypto_aead_chacha20poly1305_decrypt` and
   `crypto_secretbox_open_easy`.

Each of these has a documented workaround; the README in the parent dir
captures any updates we make as we run this against your actual device.
