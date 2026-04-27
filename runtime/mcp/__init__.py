"""MCP server bridging the X2D control daemon to MCP-aware clients
(Claude Desktop, Cursor, Continue, Zed, …).

Entry point: ``python -m mcp_x2d`` (top-level wrapper) or
``python -m runtime.mcp.server`` (direct).

The server speaks JSON-RPC 2.0 over stdio per the MCP spec
(modelcontextprotocol.io). Each tool call shells out to the bridge CLI
so behaviour stays in lock-step with the rest of the toolkit; no
duplication of MQTT publishing logic here.
"""
