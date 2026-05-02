#!/data/data/com.termux/files/usr/bin/bash
# patch_handy_debuggable.sh — patch Bambu Handy v3.19.0 APK to:
#   • android:debuggable="true" on <application>
#     → enables `run-as bbl.intl.bambulab.com` (no root needed)
#     → enables Frida attach via JDWP (no Zygisk needed)
#     → activates the existing <debug-overrides> trust block in
#       res/xml/network_security_config.xml (user CAs trusted with
#       overridePins=true)
#   • base-config of network_security_config.xml ALSO trusts user CAs
#     with overridePins=true — redundant with debug-overrides under
#     debuggable=true, but resilient if the platform parser ever stops
#     applying debug-overrides (defence in depth).
#
# Side effects:
#   • Re-signs all 4 split APKs with our local keystore. Bambu's signing
#     cert is replaced. Any iptables rule keyed on the previous UID
#     (10217) needs re-targeting at the new UID after install (uninstall
#     + install always assigns a fresh UID).
#   • Uninstalls the existing app first (signature mismatch). Any
#     previously-minted per-installation cert in EncryptedSharedPreferences
#     is wiped. Re-login is required after install.
#
# Pre-existing libflutter.so binary patches (commits f588c64, 00992ae,
# e9b2e7a) are preserved — the split_config.arm64_v8a.apk pulled from
# the device already contains them; we only resign it, never recompile.
#
# Usage:
#   bash patch_handy_debuggable.sh
#
# Requires (Termux): apktool, aapt2, zipalign, apksigner, keytool, adb
# Requires (Saga):   adb access, root NOT required for install — just
#                    package-manager allowance to side-load.
set -euo pipefail

PKG="bbl.intl.bambulab.com"
WORK="${TMPDIR:-$PREFIX/tmp}/handy_apk_patch"
KS="${WORK}/handy.keystore"
KS_PASS="android"
KS_ALIAS="handy"

mkdir -p "${WORK}"
cd "${WORK}"

# 1. Pull all currently-installed splits.
echo "[+] pulling installed splits"
APK_PATHS=$(adb shell pm path "${PKG}" | sed 's|^package:||')
[[ -z "${APK_PATHS}" ]] && { echo "ERROR: ${PKG} not installed on device"; exit 1; }

declare -a ORIG=()
declare -a SIGNED=()
while IFS= read -r remote; do
    fn=$(basename "${remote}")
    rm -f "${fn}"
    adb pull "${remote}" "${fn}" >/dev/null
    ORIG+=("${fn}")
    SIGNED+=("signed-${fn}")
done <<< "${APK_PATHS}"

echo "[+] pulled: ${ORIG[*]}"

# 2. Decode base.apk, edit AndroidManifest.xml + network_security_config.
echo "[+] decoding base.apk (no-src — keep DEX bytes intact)"
rm -rf base_decoded
# CRITICAL: --no-src skips dex→smali→dex round-trip. apktool's smali
# encoder produces DEX bytes that the ART verifier hangs on at first
# launch (process stuck in futex_wait_queue_me with libflutter never
# loaded — no logs, no crash, just frozen on splash). With --no-src,
# apktool copies the original classes*.dex untouched on rebuild and
# the verifier accepts them.
apktool d -f --no-src -o base_decoded base.apk >/dev/null

MANIFEST="base_decoded/AndroidManifest.xml"
NSC="base_decoded/res/xml/network_security_config.xml"

if grep -q 'android:debuggable="true"' "${MANIFEST}"; then
    echo "[=] manifest already debuggable=true (idempotent)"
else
    echo "[+] adding android:debuggable=\"true\" to <application>"
    sed -i 's|<application android:allowBackup="false" android:appComponentFactory="androidx.core.app.CoreComponentFactory" android:extractNativeLibs="false"|<application android:allowBackup="false" android:appComponentFactory="androidx.core.app.CoreComponentFactory" android:debuggable="true" android:extractNativeLibs="false"|' "${MANIFEST}"
fi

# Bambu's manifest has a <meta-data android:resource="@null"/> entry that
# Android 14+ rejects on install ("requires an android:value or
# android:resource attribute" — the null resource ref no longer counts).
# Replace @null with @mipmap/ic_launcher so the parser is happy.
if grep -q 'default_notification_icon" android:resource="@null"' "${MANIFEST}"; then
    echo "[+] fixing firebase notification-icon @null -> @mipmap/ic_launcher"
    sed -i 's|default_notification_icon" android:resource="@null"|default_notification_icon" android:resource="@mipmap/ic_launcher"|' "${MANIFEST}"
fi

if grep -q 'overridePins="true" src="user"' "${NSC}" | head -1; then
    : # already patched (the debug-overrides block has it; we want it in base-config too)
fi
if ! awk '/<base-config/,/<\/base-config>/' "${NSC}" | grep -q 'src="user"'; then
    echo "[+] adding user-CA trust to <base-config>"
    sed -i 's|<certificates src="system" />\n        </trust-anchors>\n    </base-config>|<certificates src="system" />\n            <certificates overridePins="true" src="user" />\n        </trust-anchors>\n    </base-config>|' "${NSC}"
    # Fall back if multi-line sed didn't match (some sed builds):
    if ! awk '/<base-config/,/<\/base-config>/' "${NSC}" | grep -q 'src="user"'; then
        python3 - "${NSC}" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(
    r'(<base-config[^>]*>\s*<trust-anchors>\s*<certificates src="system"\s*/>)\s*(</trust-anchors>\s*</base-config>)',
    r'\1\n            <certificates overridePins="true" src="user" />\n        \2',
    s, count=1)
open(p, 'w').write(s)
PY
    fi
fi

# 3. Recompile base.apk (use Termux's native aapt2 — apktool ships an
#    x86_64 binary that fails ELF-exec on aarch64).
echo "[+] recompiling base.apk via native aapt2"
apktool b -f --use-aapt2 -a "$(command -v aapt2)" base_decoded -o base-patched.apk

# 4. Generate keystore on first run (idempotent).
if [[ ! -f "${KS}" ]]; then
    echo "[+] generating keystore"
    keytool -genkey -v -keystore "${KS}" \
        -alias "${KS_ALIAS}" -storepass "${KS_PASS}" -keypass "${KS_PASS}" \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=x2d-bridge,O=local,OU=local,L=local,S=local,C=US" >/dev/null 2>&1
fi

# 5. Align + sign every split with the same key. v2/v3 only — Android 11+
#    rejects v1-only signatures for split APKs.
sign() {
    local in="$1" out="$2"
    zipalign -p -f 4 "${in}" "aligned-${in}" >/dev/null
    apksigner sign --ks "${KS}" --ks-pass "pass:${KS_PASS}" \
        --ks-key-alias "${KS_ALIAS}" --key-pass "pass:${KS_PASS}" \
        --v1-signing-enabled false --v2-signing-enabled true --v3-signing-enabled true \
        --out "${out}" "aligned-${in}" >/dev/null 2>&1
}

echo "[+] signing all splits"
sign base-patched.apk signed-base-patched.apk
for s in "${ORIG[@]}"; do
    [[ "${s}" == "base.apk" ]] && continue
    sign "${s}" "signed-${s}"
done

# 6. Uninstall + install. The install-multiple list must include base
#    first (some PackageManager versions are picky about ordering).
echo "[+] uninstalling existing ${PKG}"
adb uninstall "${PKG}" >/dev/null 2>&1 || true

echo "[+] installing patched bundle"
adb install-multiple -r --user 0 \
    signed-base-patched.apk \
    $(for s in "${ORIG[@]}"; do
        [[ "${s}" == "base.apk" ]] && continue
        echo "signed-${s}"
      done)

# 7. Verify debuggable + run-as.
echo "[+] verifying"
adb shell dumpsys package "${PKG}" | grep -E 'flags|userId|versionName' | head
NEW_UID=$(adb shell dumpsys package "${PKG}" | awk '/userId=/{print $1; exit}' | tr -d 'userId=')
echo "    uid=${NEW_UID} (iptables rules targeting old UIDs are now stale)"
adb shell run-as "${PKG}" id | head -1

echo
echo "[+] done. cert-extraction next steps:"
echo "    1. launch app, log in to Bambu account"
echo "    2. attach Frida via 'frida -U -p \$(adb shell pidof ${PKG})' or"
echo "       'adb shell run-as ${PKG} ./frida-server &' for run-as'd server"
echo "    3. hook BoringSSL SSL_CTX_use_certificate / SSL_CTX_use_PrivateKey"
echo "       in libflutter.so — those fire on first signed publish, cert"
echo "       and key are PEM/DER bytes in cleartext at that point"
