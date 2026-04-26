## Fix touchscreen taps being dropped on custom Button widgets

### Symptom

On touchscreen / convertible / kiosk deployments, several custom-drawn
Bambu widgets silently drop taps:

* `Cancel` / `OK` / `Skip` buttons in many MsgDialog flows
* AMS spool selectors
* The sidebar `SideButton` items (Printer / Filament tabs etc.)
* `AxisCtrlButton` jog directions on the device tab
* `TabButton` page-selectors inside settings panels

The user sees the press indicator activate, lifts their finger, and
nothing happens. Native widgets like `wxButton` and `wxNotebook` aren't
affected in practice — presumably because the underlying GTK / native
hit regions are more lenient than the strict client-rect check used
here.

### Root cause

All four custom Button widgets
(`src/slic3r/GUI/Widgets/Button.cpp`,
`src/slic3r/GUI/Widgets/AxisCtrlButton.cpp`,
`src/slic3r/GUI/Widgets/SideButton.cpp`,
`src/slic3r/GUI/TabButton.cpp`)
fire their `wxEVT_COMMAND_BUTTON_CLICKED` event in `mouseReleased` only
if the up-coords are still inside `wxRect({0,0}, GetSize())`:

```cpp
if (wxRect({0, 0}, GetSize()).Contains(event.GetPosition()))
    sendButtonEvent();
```

Touch input doesn't deliver pixel-stable coords. Finger contact rolls
between press and release; the up-coord typically lands a few pixels
outside the rect. The strict bounds check then drops the click.

(Side note: `Button::mouseCaptureLost` already calls `mouseReleased`
with a default-constructed `wxMouseEvent`, whose `GetPosition()` is
`{0,0}` — and `wxRect({0,0}, GetSize()).Contains({0,0})` is true, so
the click currently fires on capture-loss anyway. The strict cancel-
on-drag-off behaviour was already only partially honoured.)

### Fix

Add a small slop (`kReleaseSlop = 15` px) to the bounds check on
release. The deliberate desktop "drag off and release to cancel"
gesture is preserved (any release further than 15 px outside still
cancels), and touch users get the few pixels of grace they need.

For consistency, the patch also wraps `AxisCtrlButton` and `TabButton`'s
`ReleaseMouse()` call in a `HasCapture()` guard — wxWidgets asserts in
debug builds if you call `ReleaseMouse()` without holding capture, and
those two widgets were the only ones in this family doing so
unconditionally.

Net diff: +13, -8 across four files.

### Discovery context

Found while making BambuStudio runnable on aarch64 Termux + termux-x11,
where touch input has slightly more finger drift than typical desktop
touchscreens. Full toolkit + the rest of the platform-specific patches
at <https://github.com/tribixbite/x2d>.
