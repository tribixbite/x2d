#!/usr/bin/env bash
# Repack lico-n/ZygiskFrida v1.9.0 with:
#   - frida-gadget 17.9.3 (arm64) replacing the bundled 17.4.0
#   - pre-staged config.json targeting bbl.intl.bambulab.com
#   - pre-staged libgadget.config.so (TCP 0.0.0.0:27042, on_load=wait)
#
# Output: ./gadget-zygisk.zip  (flashable via Magisk Manager)
#
# Why repack instead of build from source?
# Upstream's verify.sh sha256-checks every extracted file, so we must
# regenerate the matching .sha256sum companions for any file we modify.
# Building the project from gradle requires JDK17 + AGP + a host that
# can drive android-build-tools — none of which are practical here on
# Termux/aarch64. The shipped release .so's are arch-portable Magisk
# Zygisk libs and are kept verbatim; we only swap the gadget payload
# and inject our two pre-staged config files.

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$PWD"

UPSTREAM_ZIP="ZygiskFrida-v1.9.0-release.zip"
GADGET_XZ="frida-gadget-17.9.3-android-arm64.so.xz"
WORK="${ROOT}/_work"
OUT="${ROOT}/gadget-zygisk.zip"

[[ -f "$UPSTREAM_ZIP" ]] || { echo "missing $UPSTREAM_ZIP — run 'curl -fLO https://github.com/lico-n/ZygiskFrida/releases/download/v1.9.0/ZygiskFrida-v1.9.0-release.zip'"; exit 1; }
[[ -f "$GADGET_XZ"   ]] || { echo "missing $GADGET_XZ — run 'curl -fLO https://github.com/frida/frida/releases/download/17.9.3/frida-gadget-17.9.3-android-arm64.so.xz'"; exit 1; }

rm -rf "$WORK" "$OUT"
mkdir -p "$WORK"
unzip -q -o "$UPSTREAM_ZIP" -d "$WORK"

echo "[1/6] swapping gadget arm64 17.4.0 -> 17.9.3"
cp "$GADGET_XZ" "$WORK/gadget/libgadget-arm64.so.xz"
( cd "$WORK/gadget" && sha256sum libgadget-arm64.so.xz | awk '{print $1}' > libgadget-arm64.so.xz.sha256sum )

echo "[2/6] dropping target config.json"
cp "$ROOT/handy.config.json" "$WORK/handy.config.json"
( cd "$WORK" && sha256sum handy.config.json | awk '{print $1}' > handy.config.json.sha256sum )

echo "[3/6] dropping libgadget.config.so"
cp "$ROOT/libgadget.config.so" "$WORK/libgadget.config.so"
( cd "$WORK" && sha256sum libgadget.config.so | awk '{print $1}' > libgadget.config.so.sha256sum )

echo "[4/6] patching customize.sh to install both new files"
# Append a block that uses upstream's `extract` helper (verify.sh sourced
# earlier in the script) so each new file is sha256-validated like the
# rest of the payload. We also rename handy.config.json -> config.json
# so ZygiskFrida picks it up automatically (overrides config.json.example).
# Leaves config.json.example in place for reference.
python3 - <<'PY'
from pathlib import Path
p = Path("_work/customize.sh")
src = p.read_text()
marker = 'extract "$ZIPFILE" "config.json.example" "$TMP_MODULE_DIR" true'
add = '''
ui_print "- Extracting handy.config.json -> config.json"
extract "$ZIPFILE" "handy.config.json" "$TMP_MODULE_DIR" true
mv "$TMP_MODULE_DIR/handy.config.json" "$TMP_MODULE_DIR/config.json"

ui_print "- Extracting libgadget.config.so (TCP 0.0.0.0:27042, on_load=wait)"
extract "$ZIPFILE" "libgadget.config.so" "$TMP_MODULE_DIR" true
'''
assert marker in src, "marker not found in customize.sh — upstream layout changed"
src = src.replace(marker, marker + "\n" + add, 1)
p.write_text(src)
PY
( cd "$WORK" && sha256sum customize.sh | awk '{print $1}' > customize.sh.sha256sum )

echo "[5/6] verifying every file has a fresh .sha256sum"
( cd "$WORK" && \
  while IFS= read -r -d '' f; do
    case "$f" in
      *.sha256sum) continue ;;
      ./META-INF/*) continue ;;
    esac
    [[ -f "${f}.sha256sum" ]] || { echo "FATAL: ${f}.sha256sum missing"; exit 2; }
    expect=$(cat "${f}.sha256sum")
    actual=$(sha256sum "$f" | awk '{print $1}')
    if [[ "$expect" != "$actual" ]]; then
      echo "FATAL: hash mismatch for $f"
      echo "  expect: $expect"
      echo "  actual: $actual"
      exit 3
    fi
  done < <(find . -type f -print0)
)

echo "[6/6] repacking -> $OUT"
( cd "$WORK" && zip -qr "$OUT" . )

echo
echo "OK -> $OUT"
ls -la "$OUT"
sha256sum "$OUT"
