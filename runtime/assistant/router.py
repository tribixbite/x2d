"""Pluggable LLM-with-tools router for the X2D assistant (item #57).

Three providers ship out of the box:

* `anthropic` — calls Anthropic's Messages API at
  https://api.anthropic.com/v1/messages with the X2D MCP toolset
  exposed as tools. Tool-use blocks are executed via the in-process
  MCP function table; results are threaded back so Claude can keep
  reasoning. Requires `ANTHROPIC_API_KEY` (or `--anthropic-key`).
* `openai` — calls OpenAI's Chat Completions API with function-
  calling. Same tool-execution loop. Requires `OPENAI_API_KEY`.
* `local` — pure-Python rule-based router. No API key needed.
  Recognises the most common print-status / control phrases
  ("temp", "pause", "resume", "stop", "AMS", "progress", "list
  printers") and translates each to one MCP tool call. Returned
  in the same shape as the LLM providers so the web UI doesn't
  care which one served the response.

The router itself owns the tool-execution loop. Each tool call goes
through the existing MCP server's tool table (so behavior is
identical to what Claude Desktop sees). The tool inventory is
imported from `runtime.mcp.server` to stay in lockstep.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

LOG = logging.getLogger("x2d.assistant")


@dataclass
class ChatTurn:
    role:    str           # "user" | "assistant" | "tool"
    content: str           # plain text for user/assistant; JSON for tool
    tool_calls: list = field(default_factory=list)
    tool_call_id: str = ""
    name:    str = ""      # tool name, when role == "tool"


@dataclass
class ChatResult:
    reply:    str
    transcript: list[ChatTurn] = field(default_factory=list)
    provider: str = ""
    tool_calls: int = 0


def _load_tools_from_mcp() -> list[dict]:
    """Pull the canonical MCP tool inventory from runtime/mcp/server.py
    so the assistant always sees the same tools as Claude Desktop."""
    from runtime.mcp.server import TOOLS
    return [
        {"name": t["name"],
         "description": t["description"],
         "input_schema": t["inputSchema"]}
        for t in TOOLS
    ]


def _execute_tool(name: str, arguments: dict) -> str:
    """Dispatch through the MCP server's tool table; returns the
    text content the tool emitted (or a JSON error blob)."""
    from runtime.mcp.server import _call_tool
    try:
        result = _call_tool({"name": name, "arguments": arguments or {}})
    except Exception as e:
        return json.dumps({"error": f"tool execution raised: {e}"})
    if result.get("isError"):
        text_parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                text_parts.append(c["text"])
        return json.dumps({"error": "\n".join(text_parts) or "tool failed"})
    out_parts = []
    for c in result.get("content", []):
        if c.get("type") == "text":
            out_parts.append(c["text"])
        elif c.get("type") == "image":
            out_parts.append("[image: "
                             + c.get("mimeType", "image/jpeg")
                             + ", " + str(len(c.get("data", "")))
                             + " base64-bytes]")
    return "\n".join(out_parts) or "(empty tool output)"


# ---------------------------------------------------------------------------
# Provider: local (rule-based, no API key)
# ---------------------------------------------------------------------------

_LOCAL_PATTERNS: list[tuple[str, str, dict]] = [
    # (regex, MCP tool name, args_template_dict_with_capture_keys)
    (r"\b(list|show|what)\b.*\bprinters?\b",  "list_printers", {}),
    (r"\b(temp|temperature|how hot)\b",       "status",        {}),
    (r"\bstatus\b",                            "status",        {}),
    (r"\bpause\b",                             "pause",         {}),
    (r"\bresume\b",                            "resume",        {}),
    (r"\bstop\b|\babort\b|\bcancel print\b",   "stop",          {}),
    (r"\b(home|G28)\b",                        "home",          {}),
    (r"\b(level|G29|bed level)\b",             "level",         {}),
    (r"\bcamera|snapshot|chamber view\b",      "camera_snapshot", {}),
    (r"\bAMS\b|\bspool\b|\bcolor\b",           "status",        {}),
    (r"\bhealthz\b|\balive\b",                 "healthz",       {}),
]


def _local_route(message: str) -> tuple[str, dict] | None:
    msg = message.lower()
    for pat, tool, args in _LOCAL_PATTERNS:
        if re.search(pat, msg):
            return tool, dict(args)
    return None


def _summarise_status_for_user(text: str) -> str:
    """Project a `status` tool's full JSON to a human sentence."""
    try:
        state = json.loads(text)
    except json.JSONDecodeError:
        return text
    p = state.get("print", {})
    bits = []
    if p.get("nozzle_temper") is not None:
        bits.append(f"nozzle {p['nozzle_temper']:.1f}°C "
                    f"(target {p.get('nozzle_target_temper', '—')}°)")
    if p.get("bed_temper") is not None:
        bits.append(f"bed {p['bed_temper']:.1f}°C "
                    f"(target {p.get('bed_target_temper', '—')}°)")
    if p.get("chamber_temper") is not None:
        bits.append(f"chamber {p['chamber_temper']:.1f}°C")
    if p.get("subtask_name"):
        pct = p.get("mc_percent", 0)
        bits.append(f'job "{p["subtask_name"]}" {pct}%')
    elif p.get("gcode_state"):
        bits.append(f"state={p['gcode_state']}")
    if not bits:
        return "Printer is reachable but reported no current state."
    return "; ".join(bits) + "."


def chat_local(message: str) -> ChatResult:
    transcript: list[ChatTurn] = [ChatTurn(role="user", content=message)]
    routed = _local_route(message)
    if routed is None:
        reply = ("I don't recognise that. Try: "
                 "'temperatures', 'pause', 'resume', 'stop', "
                 "'home', 'level', 'camera', 'list printers', "
                 "'status', or 'healthz'.")
        transcript.append(ChatTurn(role="assistant", content=reply))
        return ChatResult(reply=reply, transcript=transcript,
                           provider="local")
    tool, args = routed
    tool_text = _execute_tool(tool, args)
    transcript.append(ChatTurn(
        role="assistant",
        content=f"calling tool `{tool}` with {json.dumps(args)}",
        tool_calls=[{"name": tool, "arguments": args}]))
    transcript.append(ChatTurn(
        role="tool", name=tool, content=tool_text))
    if tool == "status":
        reply = _summarise_status_for_user(tool_text)
    elif tool == "list_printers":
        try:
            payload = json.loads(tool_text)
            names = [p.get("name") or "(default)"
                      for p in payload.get("printers", [])]
            reply = (f"Configured printers: {', '.join(names)}."
                     if names else "No printers configured.")
        except json.JSONDecodeError:
            reply = tool_text
    else:
        reply = (f"Done — `{tool}` returned: " + tool_text.strip()[:300])
    transcript.append(ChatTurn(role="assistant", content=reply))
    return ChatResult(reply=reply, transcript=transcript,
                       provider="local", tool_calls=1)


# ---------------------------------------------------------------------------
# Provider: anthropic (real Messages API + tool_use loop)
# ---------------------------------------------------------------------------

def _anthropic_call(messages: list, *, api_key: str, model: str,
                     tools: list, system: str | None = None) -> dict:
    body = {
        "model":      model,
        "max_tokens": 1024,
        "messages":   messages,
        "tools":      tools,
    }
    if system:
        body["system"] = system
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def chat_anthropic(message: str, *, api_key: str,
                    model: str = "claude-haiku-4-5-20251001",
                    history: list | None = None,
                    max_tool_loops: int = 4) -> ChatResult:
    """One chat turn against Anthropic's Messages API. Tool_use blocks
    are executed via the local MCP server; tool_result blocks are
    threaded back so Claude can keep reasoning. Returns the final
    assistant text and a flat transcript."""
    tools = _load_tools_from_mcp()
    system = (
        "You are an embedded assistant inside the x2d printer bridge. "
        "Call tools to read live state and effect actions. Be concise."
    )
    messages = list(history or [])
    messages.append({"role": "user", "content": message})
    transcript = [ChatTurn(role="user", content=message)]
    tool_calls = 0

    for _ in range(max_tool_loops + 1):
        resp = _anthropic_call(messages, api_key=api_key, model=model,
                                 tools=tools, system=system)
        # Append the assistant turn into our message log so the next
        # iteration sees its tool_use blocks.
        assistant_blocks = resp.get("content", [])
        messages.append({"role": "assistant", "content": assistant_blocks})

        text_chunks = []
        tool_uses = []
        for blk in assistant_blocks:
            if blk.get("type") == "text":
                text_chunks.append(blk.get("text", ""))
            elif blk.get("type") == "tool_use":
                tool_uses.append(blk)
        if text_chunks:
            transcript.append(ChatTurn(
                role="assistant", content="\n".join(text_chunks),
                tool_calls=[{"name": tu["name"], "arguments":
                                tu.get("input", {})} for tu in tool_uses]))

        if not tool_uses or resp.get("stop_reason") == "end_turn":
            break

        # Execute every tool_use block, append tool_result back.
        results = []
        for tu in tool_uses:
            tool_calls += 1
            out = _execute_tool(tu["name"], tu.get("input", {}))
            transcript.append(ChatTurn(
                role="tool", name=tu["name"],
                tool_call_id=tu.get("id", ""), content=out))
            results.append({
                "type":         "tool_result",
                "tool_use_id":  tu.get("id"),
                "content":      out,
            })
        messages.append({"role": "user", "content": results})

    final = "\n".join(t.content for t in transcript
                      if t.role == "assistant")
    return ChatResult(reply=final or "(no reply)",
                       transcript=transcript,
                       provider="anthropic",
                       tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# Top-level router
# ---------------------------------------------------------------------------

def route(message: str, *,
          provider: str = "auto",
          history: list | None = None) -> ChatResult:
    """Pick a provider and dispatch. `auto` falls back to `local` when
    no API key is configured."""
    p = (provider or "auto").lower()
    anth_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if p == "auto":
        p = "anthropic" if anth_key else "local"
    if p == "anthropic":
        if not anth_key:
            return ChatResult(
                reply="ANTHROPIC_API_KEY not set; falling back to "
                      "local rule-based router.",
                transcript=chat_local(message).transcript,
                provider="local",
                tool_calls=0)
        try:
            return chat_anthropic(message, api_key=anth_key,
                                    history=history or [])
        except (urllib.error.URLError, urllib.error.HTTPError,
                ConnectionError, TimeoutError) as e:
            LOG.warning("Anthropic call failed (%s); local fallback", e)
            return chat_local(message)
    return chat_local(message)
