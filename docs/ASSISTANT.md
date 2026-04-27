# AI assistant panel

A natural-language assistant in the web UI that calls the same MCP
toolset Claude Desktop sees, plus a pure-Python fallback router that
works without any API key.

## Three providers

| Provider     | Requires             | Behaviour |
|--------------|----------------------|-----------|
| `local`      | nothing              | Pure-Python rule-based router. Maps common phrases (temp / pause / resume / stop / home / level / camera / list printers / status / healthz) to one MCP tool call each. Status is projected to a human sentence. |
| `anthropic`  | `ANTHROPIC_API_KEY`  | POSTs to `api.anthropic.com/v1/messages` with the canonical MCP toolset (imported live from `runtime/mcp/server.py`). `tool_use` blocks execute via the in-process MCP `_call_tool`; `tool_result` blocks are threaded back so Claude can keep reasoning. Up to 4 tool-call loops. Default model `claude-haiku-4-5-20251001`. |
| `auto`       | nothing              | Picks `anthropic` if API key set, else `local`. Network failures during the Anthropic call also fall back gracefully. |

## API

```bash
curl -X POST http://127.0.0.1:8765/assistant/chat \
    -H 'Content-Type: application/json' \
    -d '{
        "message":  "what is the chamber temp?",
        "provider": "auto",
        "history":  []
    }'
```

Response:

```json
{
    "reply":      "nozzle 213.5°C (target 215°); bed 58.7°C (target 60°); chamber 35.0°C; job \"rumi_frame.gcode.3mf\" 42%.",
    "provider":   "local",
    "tool_calls": 1,
    "transcript": [
        {"role": "user", "content": "what is the chamber temp?"},
        {"role": "assistant", "content": "calling tool `status` with {}",
         "tool_calls": [{"name": "status", "arguments": {}}]},
        {"role": "tool", "name": "status", "content": "{...full state JSON...}"},
        {"role": "assistant", "content": "nozzle 213.5°C; …"}
    ]
}
```

## Web UI

The "Assistant" card has a chat log + text input + send button.
User / assistant / tool turns are color-coded; tool turns get a
monospace body so the raw JSON output stays readable. The provider
footnote shows which provider served the response and how many tool
calls were made.

## Anthropic mode

```bash
export ANTHROPIC_API_KEY=sk-...
python3.12 x2d_bridge.py daemon --http :8765
```

Then via the web UI Assistant card or:

```bash
curl -X POST http://127.0.0.1:8765/assistant/chat \
    -H 'Content-Type: application/json' \
    -d '{"message":"pause then tell me what the chamber temp is",
         "provider":"anthropic"}'
```

Claude will issue tool_use blocks for `pause` then `status`; the
router executes them in order, threads the results back, and
returns a single human reply.

## Local-router phrase list

| Trigger phrase                    | Tool        |
|-----------------------------------|-------------|
| `temp` / `temperature` / `how hot`| `status`    |
| `status`                          | `status`    |
| `pause`                           | `pause`     |
| `resume`                          | `resume`    |
| `stop` / `abort` / `cancel print` | `stop`      |
| `home` / `G28`                    | `home`      |
| `level` / `G29` / `bed level`     | `level`     |
| `camera` / `snapshot` / `chamber view` | `camera_snapshot` |
| `AMS` / `spool` / `color`         | `status`    |
| `list/show printers`              | `list_printers` |
| `healthz` / `alive`               | `healthz`   |

Anything else returns a "try one of:" hint.

## Test harness

```bash
PYTHONPATH=. python3.12 runtime/assistant/test_assistant.py  # 35/35 PASS
```

Covers the local router (18 checks across 9 phrases), the unknown-
phrase fallback, `route(auto)` provider selection, a *mocked* Anthropic
API that reproduces the two-iteration `tool_use → tool_result` loop
verbatim (no real API calls during CI), and the HTTP round-trip
including 400 on missing message.
