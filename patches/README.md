# BambuStudio source patches for Termux aarch64

Apply these to a fresh BambuStudio checkout (commit-pinned to the
v02.06.00.51 tag at the time of writing) before running cmake. They fix
behaviour that's broken specifically by the Termux + termux-x11 combination
and is otherwise fine on real Linux/macOS/Windows desktops.

## Apply

```
cd BambuStudio
for p in /path/to/x2d/patches/*.termux.patch; do
    git apply --check "$p" && git apply "$p" || echo "SKIP $p"
done
```

## What each patch does

| Patch | Why |
|-------|-----|
| `Button.cpp.termux.patch` | Drop the strict mouse-up bounds check in `Button::mouseReleased`. Touchscreen taps via termux-x11 have several pixels of finger drift between down and up; the upstream check was eating every Cancel/AMS-spool/sidebar tap. |
| `AxisCtrlButton.cpp.termux.patch` | Same fix for the printer-tab axis-jog widget; the active wedge is tracked in `current_pos` so the right axis still fires. |
| `SideButton.cpp.termux.patch` | Same fix for the left-rail sidebar buttons (tabs in the printer/device panes). |
| `TabButton.cpp.termux.patch` | Same fix for the small tab buttons used inside settings panels. |
| `BBLTopbar.{cpp,hpp}.termux.patch` | termux-x11 has no real WM, so `wxFrame::Maximize()` is a no-op. Make the topbar's max button: (a) call `Maximize()` for the WM hint, (b) unconditionally `SetSize(displayArea)` + `Move(displayTopLeft)` to actually fill the display, (c) temporarily relax `wxFrame::SetMinSize` (BambuStudio sets a 1000×600 floor that exceeds many phone portrait widths and would push the window off-screen), (d) maintain a parallel `m_manual_maximized` flag so Restore + drag-to-unmax still work, (e) update the toolbar icon explicitly because `wxFrame::IsMaximized()` returns false when we filled the screen manually. |
| `MainFrame.cpp.termux.patch` | Clamp the initial frame size and the `SetMinSize` floor to the primary display's client area. Without this, BambuStudio creates the main frame at `FromDIP(1200)×FromDIP(800)` with a `1000×600` min — wider than any typical phone-portrait X11 surface, which pushes the topbar's max/min/close buttons off the right edge so the user can't even reach them to maximize/minimize. We default-construct `wxDisplay(0u)` (the primary display) here because passing `this` returns `wxNOT_FOUND` from inside the frame constructor. |

All patches are minimal — they don't reformat or reorganise surrounding code,
so rebases against upstream BambuStudio should be straightforward.
