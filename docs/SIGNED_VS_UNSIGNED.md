# Signed vs unsigned print payloads — what's actually different

Pulled multiple `.gcode.3mf` files from the X2D's FTPS cache (192.168.0.138:990,
`bblp:<access_code>`, `cache/` directory) and diffed against a known-signed
cloud-emitted reference. **Goal**: deduce whether any signing artefact
travels in the `.gcode.3mf` itself, or whether signing is purely
MQTT-transport.

## Source files compared

| | Path | Origin | Size |
|---|---|---|---|
| signed reference | `samples/x2d_cloud_print_mira_official.gcode.3mf` | emitted by BambuStudio Desktop after a successful cloud print | 903 243 b |
| LAN cache (active) | `cache/Mini Crystal Dragon (2 Colors) .gcode.3mf` | currently-printing job, sliced by BambuStudio, FTP-uploaded directly | 1 868 327 b |
| LAN cache (small) | `cache/0.2mm layer, 2 walls, 15% infill.gcode.3mf` | recent print | 833 592 b |
| Plus 5 more in `cache/` from the past ~3 days |  |  |  |

## File-tree shape

Both signed and unsigned packages contain **the exact same set of metadata
entries** (modulo `process_settings_1.config` which the cloud-emitted
sample omits because Bambu's cloud strips it):

```
[Content_Types].xml
_rels/.rels
3D/3dmodel.model
Metadata/plate_<N>.json         ← bbox + filament colours
Metadata/plate_<N>.gcode        ← the actual G-code
Metadata/plate_<N>.gcode.md5    ← integrity hash for above
Metadata/plate_<N>.png          ← preview thumbnail
Metadata/plate_<N>_small.png
Metadata/plate_no_light_<N>.png
Metadata/top_<N>.png
Metadata/pick_<N>.png
Metadata/project_settings.config
Metadata/process_settings_<N>.config   ← absent in cloud-emitted
Metadata/model_settings.config
Metadata/model_settings.config.rels
Metadata/cut_information.xml
Metadata/slice_info.config
Metadata/filament_sequence.json
Auxiliaries/.thumbnails/*.png   ← sometimes
```

## Search for signing markers inside the zip

Tested every path for these markers in **both signed and unsigned** files:

| Marker | Anywhere in zip? |
|---|---|
| `-----BEGIN ... PRIVATE KEY-----` | no |
| `-----BEGIN CERTIFICATE-----` | no |
| `sign_ver`, `sign_seq_id`, `sign_value` | no |
| ASN.1 SEQUENCE prefix `0x30 0x82 ...` (DER cert/key inside any binary file) | no |
| `header.sign` | no |
| Per-installation `app_cert` reference | no |

`slice_info.config` contains a `<header>` block with `X-BBL-Client-Type` and
`X-BBL-Client-Version` — this is HTTP-style request metadata for the
slicer→cloud upload, **not** the MQTT signature header. No cryptographic
material.

## Where the signature actually lives

Confirmed by reading `bs-bionic/src/libslic3r/Format/bbs_3mf.cpp` and the
shim's MQTT code: the per-publish RSA signature is an attribute of the
**MQTT publish payload**, not the file. The slicer:

1. FTPS-uploads `<file>.gcode.3mf` to the printer's filesystem at
   `cache/<file>.gcode.3mf` (no signing — vsftpd accepts the upload as
   long as the access_code authenticates).
2. Publishes a `print.project_file` MQTT message to
   `device/<SN>/request` with payload shape:

   ```json
   {
     "print": {
       "command": "project_file",
       "param": "Metadata/plate_<N>.gcode",
       "url": "ftp:///cache/<file>.gcode.3mf",
       "subtask_id": "0",
       "task_id": "0",
       "md5": "<plate_<N>.gcode.md5>",
       "sequence_id": "<short id>",
       ...
     },
     "header": {
       "sign_ver": "v1.0",
       "sign_seq_id": "<long id>",
       "sign_value": "<base64 RSA-2048 PKCS#1v1.5 over SHA-256 of compact-JSON of payload WITHOUT the header field>"
     }
   }
   ```

3. The signing key is loaded from `EncryptedSharedPreferences`
   (Android Bambu Handy) or the OS credential vault (BambuStudio Desktop)
   per session; it never serialises into a print package.

## Conclusion

**No path to extract the signing key from cached print files.** The cert
+ key only exist:

- inside Bambu Handy's encrypted prefs on a device where the user logged
  into Bambu cloud (TEE-wrapped Tink AES-GCM blob — covered by task #24).
- inside BambuStudio Desktop's OS credential vault on a desktop where the
  user logged in (typically more accessible than Android — see `docs/LOCAL_CONTROL_PATHS.md`
  Path B alternative).

The `.gcode.3mf` upload itself is purely a file transfer; the printer
does not require it to be signed. **Status / monitor / pause / resume /
stop / lights / temps all work without the cert** because their MQTT
shapes don't trigger the firmware's `header.sign` verifier. Only the
`print.*` family does.

Practical takeaway: an "unlock LAN print" path on X2D requires obtaining
the cert by extraction, NOT by any analysis of payload files at rest.
This complements the deduction in `docs/LOCAL_CONTROL_PATHS.md`.

## Also checked: no key material in bs-bionic git history

Ran `git fsck --unreachable --no-reflogs --full` against the bs-bionic
repo. 70 unreachable blobs + 2 unreachable commits found — all
attributable to local stash entries from earlier bisect work. Each
unreachable blob was content-grepped for `^-----BEGIN` (PEM header)
and for ASN.1-DER prefix (`0x30 0x82 ... ...`) at byte 0 of small
binaries. **Zero matches.** Bambu Studio's source repo does not
ship cert material.

## Bonus inventory of the X2D's FTPS surface

| Path | Contents |
|---|---|
| `/cache/` | recent slicer uploads, ready to be triggered by `print.project_file`. ~7 files in our case, sizes 833 KB – 7.3 MB. |
| `/ipcam/` | timestamped MP4 chunks from the on-board camera (`ipcam-record.YYYY-MM-DD_HH-MM-SS.<seq>.mp4`, ~250 MB each). |
| `/timelapse/` | per-print MP4 montages + `thumbnail/` pngs. |
| `/butter-sdcard-backup.tar` | 3 GB tar — printer's own SD-card snapshot. Probably contains `/data/` partition; haven't inspected (large). |
| `/screenshots-archive.tar` | reported size 81 GB (might be FTP overflow display, see ftpd uint32 limit). |
| `/sfclog.txt` | Aug 2025 Windows SxS log — leftover from the SD card's first format. Irrelevant. |
| `/System Volume Information/` | empty — Windows SD format leftover. |
| `/<bare>.stl`, `<bare>.gcode.3mf` | direct uploads, not in cache subdir. |

The `butter-sdcard-backup.tar` is the only surface I haven't dug into;
worth a future pull when bandwidth allows. May contain firmware
artefacts hinting at the printer-side public-key / verification path.
