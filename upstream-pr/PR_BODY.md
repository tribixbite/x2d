## Touchscreen taps silently dropped on custom `Button` widgets

### Problem

Bambu Studio's custom `Button` family
(`src/slic3r/GUI/Widgets/Button.cpp`,
`src/slic3r/GUI/Widgets/AxisCtrlButton.cpp`,
`src/slic3r/GUI/Widgets/SideButton.cpp`,
`src/slic3r/GUI/TabButton.cpp`)
fires its `wxEVT_COMMAND_BUTTON_CLICKED` event in `mouseReleased` only if
the mouse-up coordinates are still inside the button's `wxRect`:

```cpp
void Button::mouseReleased(wxMouseEvent& event) {
    event.Skip();
    if (pressedDown) {
        pressedDown = false;
        if (HasCapture()) ReleaseMouse();
        if (wxRect({0, 0}, GetSize()).Contains(event.GetPosition()))
            sendButtonEvent();
    }
}
```

That bounds check is a problem on touchscreen / convertible / kiosk
deployments. A finger tap is rarely pixel-stable: the down-coords and
up-coords often differ by 5–20 px because of finger roll. The user
*sees* the press indicator, lifts the finger, and nothing happens
because the up-coords landed a few pixels outside the rect. Symptom:
"Cancel buttons don't work", "AMS spool selectors don't respond",
"Sidebar tabs randomly do nothing", "Axis-jog buttons fire
intermittently".

The standard `wxButton` doesn't have this issue because GTK's native
button handles the click on press OR on release-anywhere-while-pressed.
`wxNotebook` tabs hit-test on down. The bug is specific to BambuStudio's
own Button class hierarchy.

### Fix

Remove the up-coords bounds check. If `pressedDown` was true on
release, fire the click. Behaviour-equivalent for mouse users who
release inside the button (the common case); fixes touchscreen users
whose finger drifted slightly. Mouse users who deliberately drag off
the button to "cancel" the click lose that gesture, but it's
uncommon enough that the trade-off is worth it for the much larger
touchscreen-input population.

For `AxisCtrlButton`, the wedge selection is already tracked
separately in `current_pos` (updated by `mouseMove`), so dropping the
check still fires the correct axis.

### Provenance

This was reverse-engineered while making BambuStudio runnable on
aarch64 Termux + termux-x11, where touch input has slightly more
finger drift than typical desktop touchscreens. Full Termux build
recipe + the rest of the patches at
<https://github.com/tribixbite/x2d>.

### Files

- `src/slic3r/GUI/Widgets/Button.cpp` — drop bounds check.
- `src/slic3r/GUI/Widgets/AxisCtrlButton.cpp` — drop bounds check.
- `src/slic3r/GUI/Widgets/SideButton.cpp` — drop bounds check.
- `src/slic3r/GUI/TabButton.cpp` — drop bounds check.

Net diff: +9, –4 across four files.
