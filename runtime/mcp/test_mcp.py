"""End-to-end test for the MCP stdio server.

Spawns ``python -m mcp_x2d`` as a subprocess, drives the standard MCP
handshake (``initialize`` → ``notifications/initialized`` →
``tools/list`` → ``resources/list`` → ``tools/call list_printers`` →
``ping``), and asserts the responses are well-formed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _frame(req: dict) -> bytes:
    return (json.dumps(req) + "\n").encode("utf-8")


def _read_response(proc: subprocess.Popen, expected_id: int | str | None) -> dict:
    """Read newline-delimited JSON until we get a response with `id`
    matching expected_id (or any response if expected_id is None)."""
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout before responding")
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"non-JSON line from server: {line!r} ({e})")
        if expected_id is None or msg.get("id") == expected_id:
            return msg


def main() -> int:
    env = dict(os.environ)
    # Force the MCP server to use the in-tree bridge.
    env["X2D_BRIDGE"] = str(REPO_ROOT / "x2d_bridge.py")
    # Don't let it try to hit a daemon that isn't running.
    env["X2D_DAEMON_HTTP"] = "http://127.0.0.1:1"  # guaranteed-refused

    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_x2d"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )

    failed: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}: {detail}")
            failed.append(label)

    try:
        # 1. initialize
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test_mcp.py", "version": "0.0"},
            },
        }))
        proc.stdin.flush()
        resp = _read_response(proc, 1)
        check("initialize returns result",
              "result" in resp,
              detail=json.dumps(resp))
        if "result" in resp:
            r = resp["result"]
            check("initialize has serverInfo.name",
                  r.get("serverInfo", {}).get("name") == "x2d-bridge",
                  detail=str(r.get("serverInfo")))
            check("initialize advertises tools capability",
                  "tools" in r.get("capabilities", {}),
                  detail=str(r.get("capabilities")))
            check("initialize advertises resources capability",
                  "resources" in r.get("capabilities", {}),
                  detail=str(r.get("capabilities")))

        # 2. notifications/initialized (no response expected)
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        proc.stdin.flush()

        # 3. tools/list
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }))
        proc.stdin.flush()
        resp = _read_response(proc, 2)
        check("tools/list returns result", "result" in resp)
        tools = resp.get("result", {}).get("tools", [])
        check("tools/list returns at least 14 tools",
              len(tools) >= 14, detail=f"got {len(tools)}")
        names = {t["name"] for t in tools}
        for required in ["status", "pause", "resume", "stop", "gcode",
                          "set_temp", "chamber_light", "ams_load",
                          "ams_unload", "jog", "camera_snapshot",
                          "list_printers", "healthz", "metrics"]:
            check(f"tools includes {required!r}",
                  required in names, detail=f"have {sorted(names)}")
        for t in tools:
            check(f"tool {t['name']!r} has inputSchema.type=object",
                  t.get("inputSchema", {}).get("type") == "object",
                  detail=str(t.get("inputSchema")))

        # 4. resources/list
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {},
        }))
        proc.stdin.flush()
        resp = _read_response(proc, 3)
        check("resources/list returns result", "result" in resp)
        uris = {r["uri"] for r in resp.get("result", {}).get("resources", [])}
        check("resources includes x2d://state", "x2d://state" in uris)
        check("resources includes x2d://camera/snapshot",
              "x2d://camera/snapshot" in uris)

        # 5. tools/call list_printers (real subprocess invocation against
        # the live bridge — proves end-to-end plumbing works)
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "list_printers", "arguments": {}},
        }))
        proc.stdin.flush()
        resp = _read_response(proc, 4)
        check("tools/call list_printers returns result", "result" in resp)
        result = resp.get("result", {})
        content = result.get("content", [])
        check("tools/call list_printers has text content",
              bool(content) and content[0].get("type") == "text",
              detail=str(content))
        if content and content[0].get("type") == "text":
            try:
                payload = json.loads(content[0]["text"])
                check("list_printers returned JSON with printers[]",
                      "printers" in payload,
                      detail=str(payload)[:200])
            except json.JSONDecodeError:
                check("list_printers returned valid JSON",
                      False, detail=content[0]["text"][:200])

        # 6. ping
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 5, "method": "ping", "params": {},
        }))
        proc.stdin.flush()
        resp = _read_response(proc, 5)
        check("ping returns empty result",
              resp.get("result") == {}, detail=str(resp))

        # 7. unknown method → error
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 6, "method": "bogus/method", "params": {},
        }))
        proc.stdin.flush()
        resp = _read_response(proc, 6)
        check("unknown method returns error",
              resp.get("error", {}).get("code") == -32601,
              detail=str(resp))

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            stderr_tail = proc.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            stderr_tail = ""
        proc.wait(timeout=5)
        if stderr_tail.strip():
            print("--- server stderr ---")
            print(stderr_tail.rstrip())

    if failed:
        print(f"\nFAILED: {len(failed)} check(s): {failed}")
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
