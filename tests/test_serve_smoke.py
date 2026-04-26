#!/usr/bin/env python3
"""Smoke test for `x2d_bridge.py serve`.

Spawns the bridge as a subprocess on a temp-dir socket, hits it with the
three opcodes that don't require a real printer (`hello`, `get_version`,
and a deliberately-unknown op), and asserts the responses match the wire
format from runtime/network_shim/PROTOCOL.md.

Runs on the GitHub Actions runner — no printer needed. The
connect_printer / start_local_print path is exercised against a real X2D
by runtime/network_shim/tests/test_shim_e2e.py, which a maintainer runs
locally on a Termux device.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "x2d_bridge.py"


def _wait_for_socket(p: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists():
            return
        time.sleep(0.05)
    raise SystemExit(f"socket {p} never appeared")


def _readline(sock: socket.socket, timeout: float = 3.0) -> dict:
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise SystemExit("bridge closed socket")
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return json.loads(line)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = Path(tmp) / "bridge.sock"
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "serve", "--sock", str(sock_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            _wait_for_socket(sock_path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(sock_path))

            # 1. hello
            s.sendall(json.dumps({
                "kind": "req", "id": 1, "op": "hello",
                "args": {"abi": 1, "shim_version": 1},
            }).encode() + b"\n")
            rsp = _readline(s)
            assert rsp.get("ok") is True, f"hello not ok: {rsp}"
            assert rsp["result"]["abi"] == 1, f"abi mismatch: {rsp}"
            print(f"hello ok: bridge_version={rsp['result']['bridge_version']}")

            # 2. get_version
            s.sendall(json.dumps({
                "kind": "req", "id": 2, "op": "get_version", "args": {}
            }).encode() + b"\n")
            rsp = _readline(s)
            assert rsp.get("ok") is True, f"get_version not ok: {rsp}"
            assert rsp["result"]["version"] == "02.06.00.50", \
                f"version mismatch: {rsp}"
            print(f"get_version ok: {rsp['result']['version']}")

            # 3. unknown op should produce ok:false with a sensible code
            s.sendall(json.dumps({
                "kind": "req", "id": 3, "op": "definitely_not_a_real_op",
                "args": {},
            }).encode() + b"\n")
            rsp = _readline(s)
            assert rsp.get("ok") is False, f"unknown op did not fail: {rsp}"
            assert rsp["error"]["code"] == -1, f"wrong code: {rsp}"
            print(f"unknown op rejected: {rsp['error']['message']}")

            s.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("serve smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
