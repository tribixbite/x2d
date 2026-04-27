"""Top-level entry point so MCP clients can launch the server with
``python -m mcp_x2d``. Implementation lives in
``runtime/mcp/server.py``.
"""

from runtime.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
