# gadget-zygisk: ZygiskFrida 1.9.0 + frida-gadget 17.9.3, pre-targeted at Bambu Handy

A flashable Magisk module that injects `frida-gadget.so` into
`bbl.intl.bambulab.com` (Bambu Handy v3.19.0) before its `.init_array`
constructors run. Built on top of [lico-n/ZygiskFrida v1.9.0](https://github.com/lico-n/ZygiskFrida)
with the bundled gadget upgraded from 17.4.0 to **17.9.3** and a
pre-staged config that exposes a TCP listener on `0.0.0.0:27042` inside
the target process.

Why a Zygisk gadget instead of `frida-server`? Bambu Handy ships a
Promon-class anti-tamper shield: native code makes raw ARM64 `svc 0`
syscalls and triggers SIGSEGV-via-null-deref when it detects ptrace,
defeating any libc-wrapper hooks installed by `frida-server` after the
process starts. By having Magisk's Zygote hook map `libgadget.so` into
the process **before** the app's first constructor fires, the gadget is
already in-process when the shield comes online — no ptrace, no late
attach, no `svc` blind spot.

## What this is

```
gadget-zygisk.zip            <- the flashable module
build.sh                     <- repacks upstream + our overrides
ZygiskFrida-v1.9.0-release.zip  <- upstream source artifact
frida-gadget-17.9.3-android-arm64.so.xz  <- 17.9.3 payload
handy.config.json            <- target = bbl.intl.bambulab.com
libgadget.config.so          <- TCP 0.0.0.0:27042, on_load=wait
```

Upstream's `verify.sh` sha256-checks every extracted file, so `build.sh`
regenerates the matching `.sha256sum` companions for any file we
modify. Run it again any time you tweak `handy.config.json` or
`libgadget.config.so` and a fresh `gadget-zygisk.zip` will be produced.

## Install

Prereqs: rooted Solana Saga running Magisk **26.4** (or newer), Zygisk
enabled in Magisk settings (`Magisk → Settings → Zygisk → ON`),
DenyList **off** for `bbl.intl.bambulab.com` (or just leave DenyList off
globally).

```sh
adb push gadget-zygisk.zip /sdcard/Download/
```

On the device:

1. Open Magisk Manager
2. Modules → Install from storage → pick `/sdcard/Download/gadget-zygisk.zip`
3. Wait for install (ZygiskFrida `verify.sh` will print `- Verified <file>` for each payload — if any line is missing the build is corrupt, rerun `build.sh`)
4. Reboot the device

After reboot, verify the staged files exist (need root shell — `adb shell su -c …`):

```sh
adb shell 'su -c ls -la /data/local/tmp/re.zyg.fri/'
# expect:
#   config.json              (target = bbl.intl.bambulab.com)
#   config.json.example      (upstream stock example)
#   libgadget.so             (frida 17.9.3)
#   libgadget.config.so      (TCP 0.0.0.0:27042 listen, on_load=wait)
```

## Use

```sh
# 1. Forward the gadget port to your host
adb forward tcp:27042 tcp:27042

# 2. Launch Bambu Handy on the device — it will pause at startup
#    because the gadget is in `on_load=wait` mode. Logcat will show:
#    Frida: Listening on 0.0.0.0 TCP port 27042
adb shell monkey -p bbl.intl.bambulab.com -c android.intent.category.LAUNCHER 1
adb logcat -s Frida ZygiskFrida

# 3. Attach from host (port 27042 is now reachable via adb forward)
frida -H 127.0.0.1:27042 -F -l /data/data/com.termux/files/home/git/x2d/runtime/handy_extract/handy_hook.js
# -F (--focus) waits for the gadget to come online if you connect early
```

The `-F` flag tells Frida to attach to the gadget's `wait` checkpoint
and resume the process after `handy_hook.js` is loaded. Without `-F` the
app remains paused until you call `frida_main()` over the RPC channel.

## Toggle / disable

To temporarily disable the injection without uninstalling, flip
`enabled: true` to `enabled: false` in the on-device config:

```sh
adb shell 'su -c sed -i s/"enabled": true/"enabled": false/ /data/local/tmp/re.zyg.fri/config.json'
```

To re-enable, flip it back. To switch target package, edit
`app_name`. No reboot required — the module re-reads the config on
every `nativeForkAndSpecialize`.

To remove entirely: Magisk Manager → Modules → ZygiskFrida → Remove → reboot.

## Rebuilding from a fresh gadget version

When frida-gadget 17.9.4+ ships:

```sh
cd /data/data/com.termux/files/home/git/x2d/runtime/handy_extract/zygisk
rm frida-gadget-*.so.xz
curl -fLO https://github.com/frida/frida/releases/download/<NEW_VER>/frida-gadget-<NEW_VER>-android-arm64.so.xz
# update GADGET_XZ= line in build.sh to match the new filename
./build.sh
```

## Caveats

- Bambu Handy is 64-bit, so we only swapped `gadget/libgadget-arm64.so.xz`. The 32-bit gadget in the zip remains 17.4.0; harmless on Saga (arm64-v8a only) but if you reuse this module for a 32-bit app you'd want to replace `gadget/libgadget-arm.so.xz` too.
- The listener binds `0.0.0.0`, not `127.0.0.1`, so the port is reachable from any device on the same Wi-Fi/ADB-net. That's intentional for `frida -H 127.0.0.1:27042` after `adb forward`. Tighten to `127.0.0.1` in `libgadget.config.so` if that's a concern for your threat model.
- `on_port_conflict: fail` will kill the gadget if 27042 is already taken — check that `frida-server` is **stopped** on-device, since the existing 17.9.3 build also listens on 27042 by default.
- ZygiskFrida injects the gadget native-realm on emulators, not Java realm. On a real Saga we get full Java + native instrumentation.
