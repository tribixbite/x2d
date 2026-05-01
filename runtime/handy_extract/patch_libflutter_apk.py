#!/usr/bin/env python3.12
"""
Binary-patch Bambu Handy v3.19.0's split_config.arm64_v8a.apk in-place.

Two patches in libflutter.so:
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

Function addresses come from `.rela.dyn` parsing of the
`SecurityContext_SetTrustedCertificatesBytes` and `..._TrustBuiltinRoots`
binding-table relocations (R_AARCH64_RELATIVE entries in libflutter.so's
Dart-native function dispatch table).

CAVEAT: third-party domains served by mitmproxy (ip-api.com tested)
correctly capture through the patched chain. But Bambu also performs
*additional* Dart-level SHA-256 fingerprint pinning for its own
domains (api.bambulab.com, ab.bblmw.com, api.lunkuocorp.com,
event.bblmw.com, event.lunkuocorp.com). Those still TLS-handshake-fail
even with this patch. To bypass, additional patches are needed inside
libapp.so (Dart AOT) which has no symbols.

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
