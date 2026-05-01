#!/usr/bin/env python3.12
"""
Binary-patch Bambu Handy v3.19.0's split_config.arm64_v8a.apk in-place.

Patches in libflutter.so:
  1. ASN1_item_verify @ libflutter VA 0x6f4794
       8-byte body replaced with `mov w0, #1; ret`.
       Effect: any signature verify returns success — neutralises the
       PEM/DER signature step inside chain validation.

  2. SecurityContext_SetTrustedCertificatesBytes @ libflutter VA 0x85672c
       4-byte first instruction replaced with `b 0x859234`
       (= jump to SecurityContext_TrustBuiltinRoots at 0x859234).
       Effect: Bambu's Dart `SecurityContext.setTrustedCertificatesBytes(...)`
       call silently becomes "trust system roots" — which on Android pulls
       /system/etc/security/cacerts/, including our mitmproxy CA Magisk
       overlay c8750f0d.0.

  3. SSL_CTX_set_cert_verify_callback @ libflutter VA 0x844234
       4-byte first instruction `str x1, [x0, #0x10]` (0xf9000801)
       replaced with `str xzr, [x0, #0x10]` (0xf900081f).
       Effect: any caller installing a BoringSSL custom cert_verify_callback
       (the API used by SSLCertContext for custom chain verification, the
       most likely Dart route for SHA-256-pinning Bambu's own domains) ends
       up storing NULL instead — so BoringSSL falls back to the default
       chain validator that uses ctx->cert_store, which is populated from
       Android system roots (now including our mitmproxy CA via Magisk).

       Function identified by the unique 3-instruction pattern:
           str x1, [x0, #0x10]  ; ctx->cert_verify_callback = cb
           str x2, [x0, #0x20]  ; ctx->cert_verify_arg = arg
           ret
       The cb-store offset 0x10 / arg-store offset 0x20 match BoringSSL's
       SSL_CTX layout (`SSL_CTX::cert_verify_callback` and ::arg in
       boringssl/ssl/ssl_lib.cc).

  4. Bad-cert callback dispatcher @ libflutter VA 0x8525e0
       8-byte body replaced with `mov w0, #1; ret`.
       Effect: belt-and-braces — if Bambu's pinning routes through the
       Dart `onBadCertificate` C++ glue (the function whose body loads the
       error string "BadCertificateCallback returned a value that was not
       a boolean" @ rodata 0x19c168), it now unconditionally accepts the
       cert.  Static analysis shows zero direct refs (no ADRP+ADD, no
       relocation, no literal-pool entry) so this function may be unused
       dead code in the linked binary — but if it IS reached via dynamic
       dispatch we couldn't statically resolve, this patch covers it.

Function addresses come from `.rela.dyn` parsing of the
`SecurityContext_SetTrustedCertificatesBytes` and `..._TrustBuiltinRoots`
binding-table relocations (R_AARCH64_RELATIVE entries in libflutter.so's
Dart-native function dispatch table).

CAVEAT: third-party domains served by mitmproxy (ip-api.com tested)
correctly capture through the patched chain. Patches 3 & 4 target the
remaining bambulab/bblmw/lunkuocorp pinning route hypothesised to live
inside BoringSSL's verify-callback machinery.

Pre-conditions:
  - Saga Magisk-rooted, mitmproxy_ca module active
    (/system/etc/security/cacerts/c8750f0d.0)
  - APK pulled from device:
        adb shell 'su -c "cp /data/app/...split_config.arm64_v8a.apk /sdcard/."'
        adb pull /sdcard/saga_split.apk
  - Run this script:  python3.12 patch_libflutter_apk.py saga_split.apk
  - Push back:
        adb push saga_split.apk /sdcard/
        adb shell 'su -c "cp /sdcard/saga_split.apk /data/app/.../split_config.arm64_v8a.apk"'
        adb shell 'am force-stop bbl.intl.bambulab.com'
        adb shell 'monkey -p bbl.intl.bambulab.com -c android.intent.category.LAUNCHER 1'

The shield does NOT detect the patch — the APK signature is computed
at install time over the on-disk bytes; later in-place mutation is
not re-verified. APK V2/V3 signature scheme hashes remain valid in
PackageManager's cache.
"""
import struct, sys, zipfile

APK_LIBFLUTTER_DATA_OFFSET = 0x63a0000
PATCHES = [
    # (libflutter_va, original_bytes, patch_bytes, description)
    (0x6f4794, b'\xfd\x7b\xba\xa9\x71\x3a\x0c\x94', b'\x20\x00\x80\x52\xc0\x03\x5f\xd6',
     'ASN1_item_verify -> mov w0,#1; ret'),
    (0x85672c, b'\xff\x03\x03\xd1', b'\xc2\x0a\x00\x14',
     'SetTrustedCertificatesBytes -> b TrustBuiltinRoots(+0x2b08)'),
    # ──────────────────────────────────────────────────────────────────
    # POST-V1.0 #64  PATH #2 ATTEMPT: BoringSSL verify-callback bypass.
    # ──────────────────────────────────────────────────────────────────
    # 3. SSL_CTX_set_cert_verify_callback @ 0x844234
    #    Identified by the unique 3-instruction pattern in libflutter.so:
    #         str x1, [x0, #0x10]   ; ctx->cert_verify_callback = cb
    #         str x2, [x0, #0x20]   ; ctx->cert_verify_arg = arg
    #         ret
    #    Patches insn 0 from 0xf9000801 (str x1,[x0,#0x10]) to
    #    0xf900081f (str xzr,[x0,#0x10]) — any caller installing a custom
    #    BoringSSL cert_verify_callback ends up storing NULL, forcing
    #    BoringSSL to fall back to the default X509_STORE chain validator
    #    (which is populated from Android system roots — including the
    #    mitmproxy CA via Magisk overlay).
    #
    # 4. Dart bad-cert dispatcher @ 0x8525e0  (mov w0,#1; ret on entry)
    #    Identified by the *only* xref to .rodata string
    #    "BadCertificateCallback returned a value that was not a boolean"
    #    (@ 0x19c168). 8-byte prologue replaced — if reached, returns 1
    #    (accept).
    #
    # END-TO-END TEST RESULT (Apr 30 23:30 on Saga, mitm.log):
    #   Before patches 3+4:  50× "Client TLS handshake failed. The client
    #                        disconnected during the handshake. ... may
    #                        indicate that the client does not trust the
    #                        proxy's certificate."   (silent client abort)
    #   After patches 3+4:   50× "Client TLS handshake failed. The client
    #                        does not trust the proxy's certificate for
    #                        <bambulab-host> (OpenSSL Error([('SSL routines',
    #                        '', 'ssl/tls alert certificate unknown')]))"
    #                        (BoringSSL X509 chain validator now runs and
    #                         emits an explicit alert.)
    #   Net behavioural change: visible — the patches *did* take effect.
    #   But still 0× 200 OK on bambulab/bblmw/lunkuocorp domains; ip-api
    #   also did not flow during this test window.
    #
    # ROOT CAUSE for residual failure:
    # Patch 3 successfully redirects any caller of SSL_CTX_set_cert_verify_callback
    # to NULL, but Bambu's pinning isn't configured via that API in this
    # binary. The default chain validator runs and decides mitmproxy's CA
    # is unknown — so the SecurityContext used for these connections does
    # not see the system trust store. Patch 2 only redirects callers of
    # SecurityContext_SetTrustedCertificatesBytes; Bambu's TLS path here
    # uses a different SecurityContext setup (likely SetClientAuthorities
    # or a privately-bundled CA list inside libapp.so AOT data).
    #
    # NEXT STEPS (post-this-attempt):
    # • Patch SecurityContext_SetClientAuthoritiesBytes (impl @ resolved-via
    #   .rela.dyn — see find_boringssl.py) to also branch into TrustBuiltinRoots.
    # • Or patch BoringSSL's ssl3_send_client_certificate / X509_verify_cert
    #   internals to always return success (need to identify entry via
    #   the existing find_boringssl.py xref technique with "verify_cert"
    #   tokens once the source-path ".cc" map is broadened).
    # • Or: Frida-runtime hook on BoringSSL's verify chain at process
    #   start — handy_hook.js precedent exists; this static-only attempt
    #   was constrained to never touch handy_hook.js.
    #
    # Patches 3+4 are LEFT IN PLACE because they neutralize potential
    # custom verify callbacks that Bambu *may* install during deeper
    # navigation states (login, printer pairing, MQTT-init) — even if
    # they didn't help the boot-time HTTPS calls observed in this test.
    (0x844234, b'\x01\x08\x00\xf9', b'\x1f\x08\x00\xf9',
     'SSL_CTX_set_cert_verify_callback -> NULL out cert_verify_callback'),
    (0x8525e0, b'\xff\x03\x01\xd1\xfe\x0b\x00\xf9', b'\x20\x00\x80\x52\xc0\x03\x5f\xd6',
     'BadCertificateCallback dispatcher -> mov w0,#1; ret'),
]

def main():
    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} saga_split.apk', file=sys.stderr)
        sys.exit(2)
    apk = sys.argv[1]

    # Validate libflutter.so location inside APK
    with zipfile.ZipFile(apk, 'r') as z:
        info = z.getinfo('lib/arm64-v8a/libflutter.so')
        assert info.compress_type == 0, 'libflutter.so must be STORED (uncompressed)'
        with open(apk, 'rb') as fp:
            fp.seek(info.header_offset)
            assert fp.read(4) == b'PK\x03\x04'
            fp.read(22)
            fname_len, extra_len = struct.unpack('<HH', fp.read(4))
            data_off = info.header_offset + 30 + fname_len + extra_len
            assert data_off == APK_LIBFLUTTER_DATA_OFFSET, \
                f'libflutter.so data offset 0x{data_off:x} != expected 0x{APK_LIBFLUTTER_DATA_OFFSET:x}'

    # Apply each patch
    with open(apk, 'r+b') as f:
        for va, want_orig, new_bytes, desc in PATCHES:
            apk_off = APK_LIBFLUTTER_DATA_OFFSET + va
            f.seek(apk_off)
            actual = f.read(len(want_orig))
            if actual == new_bytes:
                print(f'[skip] already patched: {desc}')
                continue
            assert actual == want_orig, \
                f'unexpected bytes at 0x{apk_off:x}: got {actual.hex()} want {want_orig.hex()} ({desc})'
            f.seek(apk_off)
            f.write(new_bytes)
            print(f'[ok]  patched: {desc}')
    print('done')

if __name__ == '__main__':
    main()
