#!/data/data/com.termux/files/usr/bin/bash
# patch_handy_shareduid.sh — re-patch Bambu Handy v3.19.0 APK adding
# `android:sharedUserId="bbl.shared"` to <manifest>, on top of the
# debuggable + user-CA patches from patch_handy_debuggable.sh.
#
# Why: Bambu's per-installation RSA private key is stored in
# AndroidKeyStore as hardware-backed (TEE-bound). The raw bytes
# never leave silicon, so they can NEVER be extracted. But signing
# with that key DOES work via Java's Signature API — IF you call it
# from a process running under the same UID Bambu is bound to.
#
# Adding sharedUserId makes Bambu (re-installed from this patched
# APK) and our X2D Sign Helper APK both run under the same UID. The
# Helper can then call KeyStore.getInstance("AndroidKeyStore")
# .getKey(<bambu's alias>) and Signature.sign() — producing valid
# X2D-firmware-accepted signatures without ever seeing the raw key.
#
# CAVEAT — this WIPES Bambu's current login session because:
#   1. sharedUserId can only be set on a fresh package install.
#      `pm install` with a sharedUserId change refuses to upgrade.
#      You'll need to `pm uninstall` first, which clears app data.
#   2. The fresh install gets a NEW UID (the bbl.shared one).
#      Re-login is required so the cert mints into the new UID's
#      keystore, where our Helper can use it via shared UID.
#
# After running this script:
#   1. Open Bambu Handy → log in → connect to printer (cert minted)
#   2. Build + install runtime/handy_extract/sign_helper:
#        cd runtime/handy_extract/sign_helper && bash build.sh
#        adb install -r build/sign-helper.apk
#   3. List aliases:
#        adb shell am start -n com.x2d.sign/.SignActivity \
#            -a com.x2d.sign.LIST_ALIASES
#        adb shell run-as com.x2d.sign cat files/x2d_sign_out.txt
#      You should see Bambu's per-installation RSA alias + cert.
#   4. Wire the Helper into x2d_bridge.py's signed-publish path
#      (modify lan_print.py to shell out to the Helper for sign ops).
set -euo pipefail

PKG="bbl.intl.bambulab.com"
WORK="${TMPDIR:-$PREFIX/tmp}/handy_apk_patch"
KS="${WORK}/handy.keystore"
KS_PASS="android"
KS_ALIAS="handy"
SHARED_UID="${SHARED_UID:-bbl.shared}"

[[ -d "$WORK" ]] || { echo "missing $WORK — run patch_handy_debuggable.sh first to mint keystore + extract APK"; exit 1; }
[[ -f "$KS" ]]   || { echo "missing keystore at $KS"; exit 1; }
[[ -d "$WORK/base_decoded" ]] || { echo "missing decoded APK at $WORK/base_decoded — run patch_handy_debuggable.sh first"; exit 1; }

cd "$WORK"

# 1. Patch AndroidManifest.xml: add sharedUserId attribute to <manifest>.
#    apktool's text-XML form lets us do this with sed.
MANIFEST="$WORK/base_decoded/AndroidManifest.xml"
if grep -q 'android:sharedUserId=' "$MANIFEST"; then
    echo "[=] manifest already has sharedUserId (idempotent)"
else
    echo "[+] adding android:sharedUserId=\"$SHARED_UID\" to <manifest>"
    sed -i "s|<manifest |<manifest android:sharedUserId=\"$SHARED_UID\" |" "$MANIFEST"
fi

# 2. Recompile.
echo "[+] apktool b -f --no-src --use-aapt2"
rm -f base-shareduid.apk
apktool b -f --use-aapt2 -a "$(command -v aapt2)" base_decoded -o base-shareduid.apk

# 3. Sign all four splits with the same keystore. We re-use the splits
#    that patch_handy_debuggable.sh already pulled + signed; only the
#    base.apk needed to be regenerated for the manifest change.
sign() {
    local in="$1" out="$2"
    zipalign -p -f 4 "$in" "aligned-$in" >/dev/null
    apksigner sign --ks "$KS" --ks-pass "pass:${KS_PASS}" \
        --ks-key-alias "$KS_ALIAS" --key-pass "pass:${KS_PASS}" \
        --v1-signing-enabled false --v2-signing-enabled true --v3-signing-enabled true \
        --out "$out" "aligned-$in" >/dev/null 2>&1
}

echo "[+] signing base + reusing existing signed splits"
sign base-shareduid.apk signed-base-shareduid.apk

# 4. Uninstall + install fresh (sharedUserId change requires uninstall).
echo "[+] uninstalling existing $PKG (THIS WIPES THE CURRENT LOGIN + CERT)"
adb uninstall "$PKG" >/dev/null 2>&1 || true

echo "[+] installing patched bundle with sharedUserId=$SHARED_UID"
adb install-multiple -r --user 0 \
    signed-base-shareduid.apk \
    signed-split_config.arm64_v8a.apk \
    signed-split_config.en.apk \
    signed-split_config.xxhdpi.apk

# 5. Verify.
echo "[+] verifying"
adb shell dumpsys package "$PKG" | grep -E 'sharedUserId|userId|flags|versionName' | head
echo
echo "[+] done. Next steps:"
echo "    1. Open Bambu Handy on the device, log in, connect to printer."
echo "       This mints a fresh per-installation cert under the shared UID."
echo "    2. Build + install the X2D Sign Helper:"
echo "         cd runtime/handy_extract/sign_helper"
echo "         bash build.sh"
echo "         adb install -r build/sign-helper.apk"
echo "    3. Test: list aliases"
echo "         adb shell am start -n com.x2d.sign/.SignActivity \\"
echo "             -a com.x2d.sign.LIST_ALIASES"
echo "         adb shell run-as com.x2d.sign cat files/x2d_sign_out.txt"
echo "       — Bambu's RSA alias + cert should appear."
