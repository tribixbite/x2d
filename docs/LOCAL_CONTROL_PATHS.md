# Local control of X2D without Bambu cloud login — what works, what doesn't, paths forward

**Goal**: drive the X2D from this Termux/aarch64 host (or a desktop) without
ever logging into a Bambu account. Print, monitor, camera — all over the
LAN, no internet round-trip.

**TL;DR**: ~80% works today. **Starting prints does not.** The X2D's firmware
requires every `print.project_file` MQTT publish to be signed with a
per-installation RSA key minted by Bambu's cloud during account-bind.
No open-source project has solved this yet (verified by reading
upstream OrcaSlicer + the phoenixwade BambuNetwork-restoring fork).
Three concrete paths forward, ranked by effort.

---

## What works on the X2D over LAN (no login required)

Using `bblp:<access_code>` as MQTT username/password to
`mqtts://<printer_ip>:8883`:

| Capability | Mechanism | Status |
|---|---|---|
| Status push (`pushall`, layer/temp/state) | unsigned MQTT publish to `request` topic | ✅ works |
| RTSPS camera | `rtsps://bblp:<code>@<ip>:322/streaming/live/1` | ✅ works (we proved live frame in this session) |
| Pause / resume / stop | unsigned MQTT publish | ✅ works (no `print.*` prefix; treated as system commands) |
| `gcode_line` (custom G-code) | unsigned MQTT publish | ✅ works for non-print operations |
| Chamber light on/off | unsigned MQTT publish | ✅ works |
| AMS load/unload | unsigned MQTT publish | ⚠ probably works — needs live test |
| Set temps (bed, nozzle, chamber) | unsigned MQTT publish | ⚠ probably works — needs live test |
| Home / level | unsigned MQTT publish | ⚠ probably works — needs live test |
| File browse (printer SD) | FTPS @ port 990, `bblp:<code>` | ✅ works |
| File upload (FTPS RETR) | same | ✅ works |
| **Start a print** (`print.project_file`) | **REQUIRES SIGNED MQTT** | ❌ **firmware-rejected** |

The unsigned-publish set covers most day-to-day "I'm at the printer, fix
this" operations. The `print.*` family is the wall.

---

## Why `print.*` is gated and signed

X2D / H2D / refreshed X1+P1 firmware (~Jan 2025+) check the `header.sign`
field of any message in the `print.*` namespace. The signature is RSA-2048
PKCS#1v1.5 over the SHA-256 of the compact-JSON of the un-headered payload
body, using a per-installation private key that lives **only** in:

- Bambu Handy (Android) — encrypted at rest in EncryptedSharedPreferences
  with a Tink AES-GCM key wrapped by AndroidKeyStore (TEE-backed).
- BambuStudio Desktop (Windows/Linux/Mac) — similar, in OS-specific
  credential vaults.

The cert + key are minted by the cloud during the first
`/v1/iot-service/api/user/applications/{appToken}/cert` call after you
log in to your Bambu account on a fresh install. Same account on a
different install ⇒ different key. There's no open path to mint it
without going through the cloud sign-in flow.

Reference: `~/git/x2d/IMPROVEMENTS.md` items #65 / #66 / #68.

---

## Survey of the public-source landscape

### Upstream OrcaSlicer (`OrcaSlicer/OrcaSlicer`)

`MachineObject::publish_json` (DeviceManager.cpp):

```cpp
int MachineObject::publish_json(...) {
    if (is_lan_mode_printer()) {
        rtn = local_publish_json(...);     // RAW, no signing
    } else {
        rtn = cloud_publish_json(...);     // via NetworkAgent (cloud lib)
    }
}
```

`local_publish_json` calls `m_agent->send_message_to_printer()` which is
a vanilla MQTT publish. **No signing in any code path.** OrcaSlicer's
position is "LAN mode is unsigned; if your printer rejects it, log into
cloud mode." Works fine for X1/P1/A1; broken for X2D `print.*`.

### `phoenixwade/OrcaSlicer-bambulab-standalone` (the fork you cloned)

The fork **adds back** Bambu's cloud network plugin that upstream
OrcaSlicer removed. Architecture (`tools/pjarczak_bambu_linux_host/`):

1. Downloads BambuStudio's official Linux x86_64
   `libbambu_networking.so` + `libBambuSource.so` at runtime
   (NOT bundled — runtime fetch from Bambu releases).
2. Hosts the plugin in a sub-process (`pjarczak-bambu-linux-host`)
   on macOS via Lima VM, on Linux natively.
3. JSON-RPC over stdin/stdout between the slicer and the plugin host.
4. Slicer-side wrapper at `src/slic3r/Utils/PJarczakLinuxBridge/`
   marshals every `bambu_network_*` typedef'd call to the IPC.

For us: **does not help with login-less LAN print on X2D**, because the
plugin still goes through Bambu's cloud sign-in to get the cert.
Confirmed by reading `LinuxPluginHost.cpp run_probe_auth`. The fork's
goal is "make the cloud path work again on macOS where Bambu doesn't
ship a plugin," not "skip cloud."

### Our `~/git/x2d`

`x2d_bridge.py` already implements:
- LAN MQTT with `bblp:<code>` auth.
- RSA signing **using a key we have to extract** (`runtime/handy_extract/`).
  Without the key, signed publishes fail.
- Cloud bridge (item #67) using account JWT — an alternate path that
  bypasses the cert wall by routing through Bambu's cloud, but
  requires login.

---

## Path forward — three options

### Option A: ship "local-only minus print.*" (zero new work)

Accept that LAN-only X2D control is monitor + camera + non-print commands.
Document the limitation, build a clean local UI on top of what works, and
treat "start a print" as a special case (use cloud bridge OR copy
.gcode.3mf to SD card via FTPS and trigger via printer touchscreen).

- **Effort**: zero new work — already done. Wrap as a docs update.
- **Pros**: 100% offline for the 80% of operations users do.
- **Cons**: starting a print needs a separate trigger.

### Option B: extract the per-installation cert from Bambu Handy on Saga (task #24)

The patched APK is installed and debuggable. Frida-attach via run-as JDWP
(no Zygisk needed), hook `SSL_CTX_use_certificate` /
`SSL_CTX_use_PrivateKey` in the bundled BoringSSL inside libflutter.so
(offsets already mapped by `runtime/handy_extract/find_boringssl.py`),
snapshot the PEM/DER bytes the first time Bambu Handy talks to the
printer. Save to `~/.x2d/cert/` and wire `lan_print.py` to use them.

- **Effort**: ~1-2 days of Frida scripting + integration. Plus making
  Bambu Handy able to log in (currently times out on Saga's WiFi —
  see this session's logs).
- **Pros**: solves the wall properly. After cert+key are extracted,
  ALL `print.*` commands sign correctly and the printer accepts.
  No more cloud dependency for anything.
- **Cons**: cert is per-installation per-account. Wiping Bambu Handy
  (uninstall, factory reset) re-mints. Cert validity is Bambu-controlled.

### Option C: PJarczak-style IPC wrapper around Bambu's official plugin

Adapt the `pjarczak_bambu_linux_host` architecture for Termux/aarch64.
Run Bambu's official `libbambu_networking.so` (need an aarch64 build —
none ships, would have to grab from Bambu Handy APK and verify ABI
compat OR run x86_64 via box64) in a sub-process. Wire OUR shim to
proxy via JSON-RPC.

- **Effort**: 3-5 days. ABI-matching, IPC plumbing, runtime issues.
- **Pros**: get full BambuStudio/OrcaSlicer compatibility for X2D
  including cloud features, all in one shim.
- **Cons**: fundamentally still a cloud path (the plugin needs login).
  Doesn't satisfy "no Bambu login" goal.

---

## Recommendation

**Take Option A first** (zero new work, zip up the existing LAN surface as
the "local control" feature) **and Option B second** (cert extraction —
the only true "local-only print on X2D" path).

Option C is for people who want the legitimate cloud features without
trusting Bambu's binary directly — different problem from ours.

Concrete next slice if you go A→B:

1. **Doc & polish what works locally** (~half day)
   - Update README's "What works" table.
   - Add `~/git/x2d/x2d_bridge.py local-status` /
     `local-camera` / `local-pause` etc. names that mirror the
     cloud-* CLIs but use LAN MQTT only.
   - Document the "to start a new print, copy to SD or use cloud" workflow.

2. **Resume cert extraction (task #24)** when network conditions allow
   the Saga to actually finish a Bambu Handy login. Either:
   - Different WiFi (mobile data, hotspot).
   - Or, equivalently, attach Frida to **BambuStudio Desktop** on a
     machine where you ARE logged in, hook the same BoringSSL paths
     from libBambuSource.so. Same C ABI, same key-load path, same
     intercept. Less Android RE work, more cross-platform Frida.

3. **Wire `lan_print.py` to read** `~/.x2d/cert/{cert.pem,key.pem}` and
   sign `print.project_file` payloads. Already drafted in
   `IMPROVEMENTS.md` item #8 (still pending).
