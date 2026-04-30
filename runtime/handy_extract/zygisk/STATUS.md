# gadget-zygisk — STATUS

## What was done

1. **Surveyed published Zygisk-FridaGadget projects** ranked by recency and Magisk 26.x compat:

   | repo | last push | stars | verdict |
   | --- | --- | --- | --- |
   | **lico-n/ZygiskFrida** | 2025-10-18 | 960 | **CHOSEN** — actively maintained, ships flashable release, advanced JSON config with per-package targeting, child-gating, configurable injection delay, anti-anti-tamper design goal |
   | hackcatml/zygisk-gadget | 2024-10-10 | 86 | works but minimal — no per-package config, requires global gadget |
   | electrondefuser/ksu-frida | 2026-03-25 | 4 | fork of lico-n with library-remapper anti-detection — interesting but extra moving parts not needed |
   | gmh5225/zygisk-ZygiskFrida | 2023-07-29 | 1 | stale fork of lico-n — abandoned |
   | jussi-sky/Zygisk-FridaGadget | 2022-04-21 | 37 | abandoned 4yr — no Magisk 26.x compat |
   | jakobrs/zygisk-frida-gadget | (404) | — | repo doesn't exist |
   | HZRDevelop/zygisk-frida-gadget | (404) | — | repo doesn't exist |
   | dpnishant/zygisk-frida-gadget | (404) | — | repo doesn't exist |
   | MasaMune-Z/Zygisk-FridaGadget | (404) | — | repo doesn't exist |
   | wuxianlin/Zygisk-FridaGadget | (404) | — | repo doesn't exist |

2. **Picked `lico-n/ZygiskFrida` v1.9.0** because it already ships:
   - prebuilt arm64-v8a Zygisk module (`lib/arm64-v8a.so`)
   - Magisk 26.x customize.sh + verify.sh (`busybox` from `/data/adb/magisk/busybox`, `/data/adb/ksu/bin/busybox`, or `/data/adb/ap/bin/busybox`)
   - JSON config at `/data/local/tmp/re.zyg.fri/config.json` with per-package `app_name` matching, configurable `start_up_delay_ms`, `injected_libraries`, and `child_gating`
   - support for replacing the bundled gadget with a user-supplied build

3. **Repacked the upstream zip** (rather than building from gradle which needs JDK17 + AGP and is impractical from Termux/aarch64):
   - swapped `gadget/libgadget-arm64.so.xz` 17.4.0 → 17.9.3 (verified ELF: arm64 NDK r29 build)
   - dropped `handy.config.json` targeting `bbl.intl.bambulab.com` (renamed to `config.json` in customize.sh post-extract)
   - dropped `libgadget.config.so` with TCP `0.0.0.0:27042` listener and `on_load: wait`
   - patched `customize.sh` to `extract` the two new files via the upstream sha256-validated `extract` helper
   - regenerated `.sha256sum` companions for every modified file (upstream `verify.sh` aborts the install if any hash mismatches)
   - repacked → `gadget-zygisk.zip` (28.7 MB, 40 entries)

4. **Wrote `build.sh`** that's idempotent — `rm gadget-zygisk.zip; ./build.sh` rebuilds cleanly. Includes a verification pass that re-hashes every payload file and aborts if a `.sha256sum` is stale or missing, so the produced zip always passes upstream's verify.sh.

## What's tested

- [x] `build.sh` runs end-to-end on Termux/aarch64 with no errors
- [x] All 19 non-META-INF, non-sha256sum files in the produced zip have matching, current `.sha256sum` companions (verified by build.sh step 5)
- [x] Produced zip is well-formed (`unzip -l` lists 40 entries, expected layout)
- [x] `customize.sh` patch is in the right place (after `config.json.example` extract, before `set_perm_recursive`) and uses the validated `extract` helper, not raw `unzip`
- [x] Gadget xz decompresses to a valid arm64 ELF (`file ... = ARM aarch64 shared object, NDK r29, Android API 21+`)
- [x] `gadget-zygisk.zip` sha256 = `c9e728e999e426aaeab2e1d99c1455bdde2c5d1196036f1c943c9bb672a0bf1b`

## What is NOT tested (requires the Saga)

- [ ] `gadget-zygisk.zip` actually flashes via Magisk Manager 26.4 — needs adb push + manual install on-device
- [ ] After reboot, `/data/local/tmp/re.zyg.fri/{config.json,libgadget.config.so,libgadget.so}` are all present and 0644 owned by root:root
- [ ] Launching Bambu Handy triggers the gadget — `adb logcat -s Frida ZygiskFrida` should show `Listening on 0.0.0.0 TCP port 27042`
- [ ] `adb forward tcp:27042 tcp:27042 && frida -H 127.0.0.1:27042 -F -l handy_hook.js` attaches successfully
- [ ] The Promon shield is actually defeated (i.e. Frida hooks survive the shield's tamper checks). If the shield specifically detects `frida-gadget` strings or `libgadget.so` mappings in `/proc/self/maps` we may need to additionally rename the .so. ZygiskFrida already mitigates this by loading from `/data/local/tmp/re.zyg.fri/libgadget.so` (no `frida` substring in the path) and the gadget's exposed thread/symbol names have been historically scrubbed.

## Run instructions for the user

```sh
# from your dev host with adb
cd /data/data/com.termux/files/home/git/x2d/runtime/handy_extract/zygisk

# 1. push the flashable to the Saga
adb push gadget-zygisk.zip /sdcard/Download/

# 2. on the Saga: Magisk Manager → Modules → Install from storage → gadget-zygisk.zip
#    confirm output ends with "Verified" lines for every file, then reboot

# 3. after reboot, sanity-check staging
adb shell 'su -c ls -la /data/local/tmp/re.zyg.fri/'
# expect: config.json, libgadget.so, libgadget.config.so, libgadget32.so, config.json.example

# 4. forward the gadget port
adb forward tcp:27042 tcp:27042

# 5. launch the app
adb shell am start -n bbl.intl.bambulab.com/.MainActivity || \
  adb shell monkey -p bbl.intl.bambulab.com -c android.intent.category.LAUNCHER 1

# 6. watch the gadget come online (should take <5s)
adb logcat -s Frida ZygiskFrida

# 7. attach from host
frida -H 127.0.0.1:27042 -F -l ../handy_hook.js
```

## If it doesn't work

1. **No "Listening on 0.0.0.0 TCP port 27042" in logcat after launching the app**
   - Check `adb shell 'su -c cat /data/local/tmp/re.zyg.fri/config.json'` — if `app_name` is wrong, fix and relaunch the app (no reboot needed)
   - Check `adb logcat -s ZygiskFrida` — module logs the matched/unmatched app_name on every fork
   - Verify Zygisk is enabled: `adb shell 'su -c magisk --denylist status'` and `Magisk → Settings → Zygisk` shows `Yes`
   - Verify the module is loaded: `adb shell 'su -c ls /data/adb/modules/'` — `zygiskfrida` should be present

2. **"FATAL: Failed to verify <file>" during install**
   - The build is corrupt — re-run `./build.sh` to regenerate sha256sums

3. **Gadget loads but Frida can't connect**
   - Confirm port forward: `adb forward --list`
   - Try `frida-ls-devices` — `127.0.0.1:27042` should appear as a remote device
   - Check the gadget config loaded properly: `adb logcat -s Frida` will show `Loaded config from libgadget.config.so` if the config file was found

4. **Bambu Handy crashes on launch**
   - The Promon shield may be detecting the gadget. Try `start_up_delay_ms: 2000` in `/data/local/tmp/re.zyg.fri/config.json` to delay injection until after the shield's initial scan
   - Try renaming the gadget on-device: `cp /data/local/tmp/re.zyg.fri/libgadget.so /data/local/tmp/re.zyg.fri/libfoo.so` and update the `path` in `config.json` — defeats path-string scans
   - Last resort: enable child_gating with `mode: "freeze"` if the app's anti-tamper runs in a forked child

## Files

```
/data/data/com.termux/files/home/git/x2d/runtime/handy_extract/zygisk/
├── README.md                            ← user-facing install/use docs
├── STATUS.md                            ← this file
├── build.sh                             ← repack script
├── gadget-zygisk.zip                    ← flashable module (28.7 MB)
├── handy.config.json                    ← target = bbl.intl.bambulab.com
├── libgadget.config.so                  ← TCP 0.0.0.0:27042, on_load=wait
├── ZygiskFrida-v1.9.0-release.zip       ← upstream artifact (28.6 MB)
└── frida-gadget-17.9.3-android-arm64.so.xz  ← 17.9.3 payload (6.6 MB)
```
