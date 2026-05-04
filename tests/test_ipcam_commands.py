#!/usr/bin/env python3
"""Unit tests for the ipcam record / timelapse / resolution bridge
subcommands (x2d/termux #88).

The three commands all build a `{"camera": {...}}` MQTT payload via
the new `_camera_cmd()` helper and publish unsigned to
`device/<sn>/request`. Tests verify:
  * the payload structure matches BambuStudio source
    (DeviceManager.cpp:2027 / 2038 / 2049)
  * argparse rejects invalid state/resolution values
  * publish path is the same plain MQTT path as set_lamp etc.,
    NOT the Bambu Connect signed path used for print/start_print.

Runs on GHA — mocks the X2DClient.publish call.
"""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _CapturingClient:
    """Stand-in that captures the published payload."""

    def __init__(self):
        self.published: list[dict] = []
        self.connected = False

    def connect(self, *args, **kwargs):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def publish(self, payload: dict, qos: int = 1, **_kwargs):
        self.published.append(payload)


def _patch_bridge_publish(monkey_published: list[dict]) -> None:
    """Install a capturing X2DClient.publish that pushes to the list."""
    import x2d_bridge

    class _Cli(_CapturingClient):
        def publish(self, payload, qos=1, **kw):
            monkey_published.append(payload)

    # _publish_one builds the client and calls connect/publish/disconnect.
    # Inject our shim by patching the X2DClient class.
    x2d_bridge.X2DClient = lambda *a, **kw: _Cli()  # type: ignore


def _make_args(**kw) -> argparse.Namespace:
    """Build an argparse.Namespace with the credential fields the
    Creds.resolve() path expects to find."""
    defaults = dict(
        ip="192.168.0.42",
        code="abcdef12",
        serial="00P9AJ000000000",
        printer=None,
        config=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_record_on_payload() -> None:
    import x2d_bridge

    published: list[dict] = []
    _patch_bridge_publish(published)

    args = _make_args(state="on")
    rc = x2d_bridge.cmd_record(args)
    assert rc == 0, "cmd_record should return 0 on success"
    assert len(published) == 1, "expected one publish"
    p = published[0]
    assert "camera" in p, f"missing camera key: {p}"
    cam = p["camera"]
    assert cam["command"] == "ipcam_record_set", cam
    assert cam["control"] == "enable", cam
    assert "sequence_id" in cam, cam


def test_record_off_payload() -> None:
    import x2d_bridge

    published: list[dict] = []
    _patch_bridge_publish(published)

    rc = x2d_bridge.cmd_record(_make_args(state="off"))
    assert rc == 0
    assert published[0]["camera"]["control"] == "disable"


def test_timelapse_on_payload() -> None:
    import x2d_bridge

    published: list[dict] = []
    _patch_bridge_publish(published)

    rc = x2d_bridge.cmd_timelapse(_make_args(state="on"))
    assert rc == 0
    cam = published[0]["camera"]
    assert cam["command"] == "ipcam_timelapse"
    assert cam["control"] == "enable"


def test_resolution_payload() -> None:
    import x2d_bridge

    for res in ("low", "medium", "high", "full"):
        published: list[dict] = []
        _patch_bridge_publish(published)

        rc = x2d_bridge.cmd_resolution(_make_args(resolution=res))
        assert rc == 0, f"resolution={res} failed"
        cam = published[0]["camera"]
        assert cam["command"] == "ipcam_resolution_set"
        assert cam["resolution"] == res


def test_invalid_state_rejected() -> None:
    import x2d_bridge

    # cmd_record sys.exits on invalid state — catch via SystemExit
    raised = False
    try:
        x2d_bridge.cmd_record(_make_args(state="bogus"))
    except SystemExit:
        raised = True
    assert raised, "cmd_record should sys.exit on invalid state"


def test_invalid_resolution_rejected() -> None:
    import x2d_bridge

    raised = False
    try:
        x2d_bridge.cmd_resolution(_make_args(resolution="ultra"))
    except SystemExit:
        raised = True
    assert raised, "cmd_resolution should sys.exit on invalid value"


def test_camera_helper_builds_correct_shape() -> None:
    """Direct test of the _camera_cmd() helper to lock in the shape."""
    import x2d_bridge

    p = x2d_bridge._camera_cmd("ipcam_test", control="enable", extra=42)
    assert "camera" in p
    cam = p["camera"]
    assert cam["command"] == "ipcam_test"
    assert cam["control"] == "enable"
    assert cam["extra"] == 42
    assert "sequence_id" in cam
    # sequence_id is a stringified integer (BambuStudio source convention)
    assert isinstance(cam["sequence_id"], str)
    int(cam["sequence_id"])  # must parse


if __name__ == "__main__":
    # Allow `python tests/test_ipcam_commands.py` direct run for sanity check.
    import inspect

    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:
                print(f"  FAIL {name}: {e}")
                failed += 1
    sys.exit(1 if failed else 0)
