# BambuStudio on Termux aarch64 — X2D / H2D / signed-LAN-MQTT toolkit

This repo collects the patches, runtime shims, and helpers needed to run
[BambuStudio v02.06.00.51](https://github.com/bambulab/BambuStudio/releases/tag/v02.06.00.51)
natively on aarch64 Termux + termux-x11, plus a pure-Python LAN client that
talks to recent Bambu printers (X2D / H2D / refreshed P1+X1) using their new
RSA-SHA256-signed MQTT protocol — no Bambu Network Plug-in, no cloud login.

> The Bambu Network Plug-in `.so` is shipped only for x86\_64 Linux and
> arm64 macOS. On aarch64 Termux it has no equivalent build, so out of the
> box BambuStudio's GUI cannot connect to a LAN printer or sync AMS spool
> data. The bridge in this repo (`x2d_bridge.py`) replaces what the plug-in
> would have done for the LAN-only path.

## Layout

```
.
├── patches/                  # 6 unified diffs against upstream BambuStudio
│   ├── Button.cpp.termux.patch
│   ├── AxisCtrlButton.cpp.termux.patch
│   ├── SideButton.cpp.termux.patch
│   ├── TabButton.cpp.termux.patch
│   ├── BBLTopbar.cpp.termux.patch
│   └── BBLTopbar.hpp.termux.patch
├── runtime/
│   └── preload_gtkinit.c     # LD_PRELOAD shim: GTK pre-init, locale fix,
│                             # wxLocale ICU bypass, wx 3.3 assert silencer,
│                             # hidden config_wizard_startup override
├── patch_bambu_skip_wizard.py   # binary patch: GUI_App::config_wizard_startup → false
├── run_gui_clean.sh          # canonical GUI launcher
├── x2d_bridge.py             # signed-MQTT LAN client (status/upload/print/daemon)
├── bambu_cert.py             # publicly-leaked Bambu Connect signing key
├── lan_upload.py             # FTPS:990 implicit-TLS uploader (helper subset)
├── lan_print.py              # upload + start_print combo
├── make_frame.py             # generates a debossed picture-frame STL
├── inject_thumbnails.py      # injects the 5 PNG thumbnails firmware needs
├── resolve_profile.py        # flatten BambuStudio profile inheritance
└── test_signed_mqtt.py       # diagnostic: pushall with RSA-SHA256 signature
```

`bs-bionic/` (the BambuStudio source tree) and `dist/` (built tarball) are
gitignored — the patches in `patches/` reproduce the source changes against
a clean clone.

## Quick start

### Use the prebuilt distribution

A prebuilt tarball is attached to the GitHub release:

```
curl -L -o bs-x2d.tar.xz \
    https://github.com/tribixbite/x2d/releases/latest/download/bambustudio-x2d-termux-aarch64.tar.xz
tar -xJf bs-x2d.tar.xz
cd bambustudio-x2d-termux-aarch64
./run_gui.sh                  # needs termux-x11 running on DISPLAY=:1
```

### Termux dependencies

```
pkg install x11-repo
pkg install \
    wxwidgets gtk3 webkit2gtk-4.1 \
    glew glfw mesa libllvm llvm \
    glib pango cairo gdk-pixbuf atk \
    fontconfig freetype libpng libjpeg libtiff \
    openssl curl libcurl \
    opencv libdbus libwebp \
    libavcodec libswscale libavutil \
    python python-cryptography xdotool \
    openbox
pip install paho-mqtt
```

**Why openbox is in the list**: termux-x11 ships without a window manager.
Without one, `wxFileDialog`s open undecorated at (0,0) and stack *under*
the main frame so Cancel-button taps land on the main frame instead of
the dialog ("Cancel buttons don't work"); transient dialogs can't be
dragged; `wxFrame::Maximize` is a no-op. Openbox (≈600 KB) supplies a
minimal EWMH-aware WM that fixes all of those. `run_gui.sh` will spawn
it automatically on launch if it's installed and not already running.

The most version-sensitive of these is `libllvm` — Mesa requires it at
the same major version. If `pkg upgrade` ever leaves them mismatched
you'll get `EGL_BAD_PARAMETER` / "Unable to get EGL Display"; recover
with `pacman -S libllvm llvm`.

You also need a working X server. The reference setup is **termux-x11**
(install the Android app and run `termux-x11` in a Termux session,
reachable on `:1`).

### Build from source

```
git clone --recurse-submodules https://github.com/bambulab/BambuStudio
cd BambuStudio
git checkout v02.06.00.51
for p in /path/to/x2d/patches/*.termux.patch; do git apply "$p"; done
# Use the bionic-toolchain steps from
# ~/.claude/skills/bambustudio-on-termux-aarch64.md
mkdir build && cd build && cmake -GNinja .. && ninja bambu-studio
gcc -fPIC -shared ../runtime/preload_gtkinit.c \
    $(pkg-config --cflags --libs gtk+-3.0) -ldl -o ../runtime/libpreloadgtk.so
python3 ../patch_bambu_skip_wizard.py src/bambu-studio
```

## Using the LAN bridge (`x2d_bridge.py`)

Save credentials once:

```
mkdir -p ~/.x2d && chmod 700 ~/.x2d
cat > ~/.x2d/credentials <<EOF
[printer]
ip = 192.168.x.y
code = <8-char access code from printer screen>
serial = <printer serial from device sticker>
EOF
chmod 600 ~/.x2d/credentials
```

Then:

```
# One-shot state dump
x2d_bridge.py status

# Upload + start print on AMS slot 4
x2d_bridge.py print myfile.gcode.3mf --slot 3

# Long-running monitor — polls every 5s, exposes JSON at http://127.0.0.1:8765/state
x2d_bridge.py daemon --http 127.0.0.1:8765 --quiet
```

Credentials can also come from `--ip / --code / --serial` flags or
`X2D_IP / X2D_CODE / X2D_SERIAL` environment variables.

## What's broken on Termux without these patches

| Symptom | Root cause | Patch |
|---|---|---|
| GUI aborts at start with `Gtk-ERROR: Can't create a GtkStyleContext without a display connection` | wxFont static init touches GTK CSS before `gtk_init` | `runtime/preload_gtkinit.c` (constructor 101) |
| GUI shows "Switching language en\_US failed" then exits | wx 3.3 `wxUILocale::IsAvailable` is ICU-backed, Termux libicu has no `en_US` | shim overrides the symbol |
| `setlocale("en_US", …)` returns NULL → modal exit | bionic accepts `en_US.UTF-8` but not bare `en_US` | shim retries with `.UTF-8` suffix |
| GUI runs ~20s then dies on first GL draw with `zink_kopper.c:720` assert | Mesa picks zink (Vulkan→GL); kopper needs DRI3/Present which termux-x11 lacks | `run_gui_clean.sh` forces `GALLIUM_DRIVER=llvmpipe` |
| Setup Wizard hangs on Bambu cloud calls | wizard tries cloud region/login that times out on Termux | `patch_bambu_skip_wizard.py` (binary patch) |
| Cancel buttons / AMS spool taps / sidebar buttons silently dropped | custom `Button::mouseReleased` strict bounds check vs. touch-drift | `patches/{Button,AxisCtrlButton,SideButton,TabButton}.cpp.termux.patch` |
| Maximize button does nothing / window goes off-screen in portrait | termux-x11 has no WM; BBLTopbar relies on `wxFrame::Maximize()`; min-size 1000×600 exceeds portrait width | `patches/BBLTopbar.{cpp,hpp}.termux.patch` |
| LAN connect / AMS sync / print impossible | Network Plug-in is x86\_64 only | `x2d_bridge.py` |
| `mqtt message verify failed` (err\_code 84033543) on every command | Jan-2025+ firmware requires RSA-SHA256 signature in `header` block | `bambu_cert.py` (publicly-leaked Bambu Connect cert) |

## Provenance

Built and tested on:

* Termux aarch64, x11-repo packages (`wxwidgets 3.3`, `gtk3`, `webkit2gtk-4.1`,
  `mesa 26.0.5`, `libllvm 21`, …)
* termux-x11 Android app, software-rendering display `:1`
* Bambu Lab X2D, dual-extruder, AMS HT 4-slot, firmware ≥ Jan 2025

GPL-3.0+ (matches upstream BambuStudio). Bambu and BambuStudio are
trademarks of Shenzhen Bambu Lab Technology Co., Ltd. — this repo is not
affiliated with or endorsed by them.
