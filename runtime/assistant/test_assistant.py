"""End-to-end test for the AI assistant router (item #57).

Covers:

* `chat_local` rule-based routing for the common phrases
* tool-execution loop calls into the in-process MCP server
* `route(provider="auto")` picks `local` when no API key is set
* `chat_anthropic` against a *mocked* Anthropic API: serialises a
  fake tool_use block, executes it via the MCP server, threads the
  tool_result back, and returns the final assistant text.
* HTTP integration: POST /assistant/chat round-trips with provider
  field, transcript, tool-call count.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import x2d_bridge
from runtime.assistant import router


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post_json(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    failed: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    # Stub the MCP execute path so we don't try to spawn x2d_bridge
    # subprocesses (those would dial real MQTT). Each tool returns
    # a canned text payload that the local router can parse.
    canned: dict[str, str] = {
        "status": json.dumps({
            "print": {
                "nozzle_temper": 213.5, "nozzle_target_temper": 215,
                "bed_temper":     58.7, "bed_target_temper":    60,
                "chamber_temper": 35.0,
                "subtask_name":   "rumi_frame.gcode.3mf",
                "mc_percent":     42,
                "gcode_state":    "RUNNING",
            }
        }),
        "list_printers": json.dumps({
            "printers": [{"name": "studio", "ip": "192.168.0.42"},
                          {"name": "garage", "ip": "192.168.0.43"}]
        }),
        "pause":  json.dumps({"ok": True, "verb": "pause"}),
        "resume": json.dumps({"ok": True, "verb": "resume"}),
        "stop":   json.dumps({"ok": True, "verb": "stop"}),
        "home":   "homed",
        "level":  "leveled",
        "camera_snapshot": "(image: image/jpeg, 12345 base64-bytes)",
        "healthz": json.dumps({"healthy": True}),
    }
    def fake_exec(name, args):
        return canned.get(name, json.dumps({"error":
            f"no canned response for {name}"}))
    router._execute_tool = fake_exec  # monkey-patch for the test

    # Also remove ANTHROPIC_API_KEY so route(auto) → local.
    os.environ.pop("ANTHROPIC_API_KEY", None)

    # ----- 1. chat_local pattern matrix -----
    cases = [
        ("what's the chamber temperature?", "status", "chamber 35"),
        ("temp",                              "status", "nozzle 213"),
        ("pause the print",                   "pause",  "Done"),
        ("please resume",                     "resume", "Done"),
        ("stop everything",                   "stop",   "Done"),
        ("home all axes",                     "home",   "Done"),
        ("show me the camera",                "camera_snapshot", "image"),
        ("list printers please",              "list_printers", "studio"),
        ("healthz",                           "healthz", "Done"),
    ]
    for prompt, expected_tool, fragment in cases:
        result = router.chat_local(prompt)
        check(f"local: {prompt!r} → tool {expected_tool}",
              result.tool_calls == 1
              and any(t.name == expected_tool
                      for t in result.transcript if t.role == "tool"),
              detail=f"reply={result.reply[:120]!r}")
        check(f"local: {prompt!r} reply contains {fragment!r}",
              fragment.lower() in result.reply.lower(),
              detail=result.reply[:200])

    # ----- 2. unknown phrase yields helpful fallback -----
    r = router.chat_local("xyzzy plover")
    check("unknown phrase → no tool call",
          r.tool_calls == 0)
    check("unknown phrase reply mentions 'try'",
          "try:" in r.reply.lower(),
          detail=r.reply)

    # ----- 3. route(auto) picks local without API key -----
    r = router.route("status please", provider="auto")
    check("route(auto) without API key → provider=local",
          r.provider == "local", detail=r.provider)
    check("route(auto) status produces tool call",
          r.tool_calls == 1)

    # ----- 4. mocked Anthropic provider -----
    # Two-iteration LLM dance: first response uses tool_use, second
    # is plain text after seeing the tool_result.
    api_responses = [
        # iteration 1: tool_use
        {"content": [
            {"type": "text", "text": "Let me check the bed temp."},
            {"type": "tool_use", "id": "toolu_1",
             "name": "status", "input": {}},
        ], "stop_reason": "tool_use"},
        # iteration 2: final text
        {"content": [{"type": "text", "text":
            "The bed is currently at 58.7°C, target 60."}],
         "stop_reason": "end_turn"},
    ]
    call_log: list[list] = []
    def fake_anthropic_call(messages, *, api_key, model, tools, system=None):
        call_log.append([(m["role"], m["content"]) for m in messages])
        return api_responses.pop(0)
    with mock.patch.object(router, "_anthropic_call", fake_anthropic_call):
        r = router.chat_anthropic("what's the bed temp?",
                                   api_key="sk-stub",
                                   history=[])
    check("anthropic mock: provider=anthropic", r.provider == "anthropic")
    check("anthropic mock: 1 tool call",
          r.tool_calls == 1, detail=str(r.tool_calls))
    check("anthropic mock: reply mentions 58.7",
          "58.7" in r.reply, detail=r.reply)
    check("anthropic mock: 2 API calls (initial + after tool_result)",
          len(call_log) == 2, detail=str(len(call_log)))
    if len(call_log) >= 2:
        # Second call must include the tool_result block
        last_msgs = call_log[1]
        check("anthropic mock: second call includes tool_result",
              any("tool_result" in str(c) for _r, c in last_msgs),
              detail=str(last_msgs)[:300])

    # ----- 5. HTTP /assistant/chat -----
    port = _free_port()
    threading.Thread(
        target=x2d_bridge._serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     lambda _p: {"print": {"nozzle_temper": 27.0}},
            "get_last_ts":   lambda _p: time.time() - 1,
            "max_staleness": 30.0,
            "auth_token":    None,
            "printer_names": [""],
            "clients":       {"": object()},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True, name="ass-http",
    ).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"

    s, body = _post_json(base + "/assistant/chat",
                         {"message": "what's the temp?"})
    check("POST /assistant/chat status 200", s == 200, str(s))
    check("response includes reply",
          "reply" in body and isinstance(body["reply"], str),
          detail=str(body)[:200])
    check("response includes provider=local",
          body.get("provider") == "local", detail=str(body.get("provider")))
    check("response includes transcript",
          isinstance(body.get("transcript"), list)
          and len(body["transcript"]) >= 2,
          detail=str(body.get("transcript"))[:200])
    check("response includes tool_calls=1",
          body.get("tool_calls") == 1, detail=str(body.get("tool_calls")))
    if body.get("transcript"):
        check("transcript first turn = user",
              body["transcript"][0]["role"] == "user")
        check("transcript contains tool turn",
              any(t["role"] == "tool" for t in body["transcript"]))

    s, body = _post_json(base + "/assistant/chat", {})
    check("POST /assistant/chat without message → 400",
          s == 400, str(s))

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — assistant router (#57)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
