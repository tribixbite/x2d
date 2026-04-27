"""Natural-language assistant routing via MCP tools (item #57).

Routes chat messages through a configurable LLM provider (Anthropic
Claude, OpenAI, or a local rule-based fallback) that can invoke the
X2D MCP toolset. The fallback works without any API key and handles
the common phrases ("what's the chamber temp?", "pause the print",
etc.) by mapping to direct MCP tool calls — useful for offline or
on-device testing.

Wire surface: `POST /assistant/chat` on the bridge daemon. Web UI
chat panel at `web/index.html` Assistant card.
"""
