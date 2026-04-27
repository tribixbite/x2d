# X2D MCP server — Claude Desktop integration

The bridge ships an MCP (Model Context Protocol) server at
`runtime/mcp/server.py`, callable as `python -m mcp_x2d`. Once wired
into Claude Desktop (or any other MCP-aware client), Claude can:

- read live printer state (temperatures, AMS spools, print progress)
- pause / resume / stop the current job
- send arbitrary G-code
- set bed / nozzle / chamber targets
- toggle the chamber LED
- load / unload AMS slots
- jog any axis
- upload a sliced `.gcode.3mf` and start a print
- snapshot the chamber camera
- list all configured printers

## 1. Verify the server runs locally

Before wiring it into a client, smoke-test it:

```bash
cd /path/to/x2d
python3.12 runtime/mcp/test_mcp.py
```

Expected: 47/47 PASS lines and a final `ALL TESTS PASSED`. The harness
spawns the real server, drives the full MCP handshake, and confirms
the tool catalogue is intact. If the bridge can reach your X2D, the
`tools/call status` step will return live temperatures.

## 2. Claude Desktop config

Claude Desktop's MCP config lives at:

| Platform | Path |
|---|---|
| **Termux (Android)** | `~/.config/Claude/claude_desktop_config.json` *(if you've set up the Linux desktop wrapper)* — the canonical use case is to run the server **on Termux** and point an MCP client running elsewhere at it (see §4 for SSH-tunnel pattern). |
| **macOS** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` |
| **Linux** | `~/.config/Claude/claude_desktop_config.json` |

Add an `mcpServers` entry pointing at this repo. Copy-paste verbatim,
edit the path, and restart Claude Desktop:

```json
{
  "mcpServers": {
    "x2d": {
      "command": "python3.12",
      "args": ["-m", "mcp_x2d"],
      "cwd": "/absolute/path/to/x2d",
      "env": {
        "PYTHONPATH": "/absolute/path/to/x2d"
      }
    }
  }
}
```

Notes:
- `command` must be the Python that has `paho-mqtt` and the bridge's
  other deps installed (`python3.12` on Termux, the venv interpreter
  on a desktop install).
- `cwd` MUST be the repo root so `mcp_x2d.py` is importable and the
  bundled `bambu_cert.py` is found.
- `PYTHONPATH` is belt-and-braces for clients that strip cwd from
  `sys.path`. Drop it if your client preserves cwd.

After restarting Claude Desktop, the `x2d` server should appear in the
MCP indicator (hammer icon). Click it; you should see the 18 tools
listed (`status`, `pause`, `resume`, `stop`, `gcode`, `home`, `level`,
`set_temp`, `chamber_light`, `ams_load`, `ams_unload`, `jog`, `upload`,
`print`, `camera_snapshot`, `list_printers`, `healthz`, `metrics`).

## 3. Per-platform install notes

### 3.1 Termux (Android)

```bash
# Bridge deps (one-time)
pkg install python python-cryptography
pip install paho-mqtt requests

# Clone the repo
git clone https://github.com/tribixbite/x2d ~/git/x2d
cd ~/git/x2d
./install.sh                    # builds the bridge runtime; idempotent

# Configure the printer
mkdir -p ~/.x2d
cat >~/.x2d/credentials <<'EOF'
[printer]
ip     = 192.168.1.42
code   = 12345678
serial = 03ABC0001234567
EOF

# Smoke-test the MCP server
python3.12 runtime/mcp/test_mcp.py
python3.12 -m mcp_x2d </dev/null    # should print "[mcp] x2d-bridge MCP server up …" then exit on EOF
```

Termux can't run Claude Desktop directly. Two patterns work:

1. **SSH tunnel to a desktop client** — Run an SSH server on Termux
   (`sshd` from `pkg install openssh`), then in Claude Desktop on your
   laptop set `command = "ssh"` and pass the `python -m mcp_x2d`
   invocation as args. See §4 for an example block.
2. **Local API surface** — Run `x2d_bridge.py daemon --http 0.0.0.0:8765 --auth-token …`
   and have any MCP client on the same network point at that HTTP API
   directly via `X2D_DAEMON_HTTP`.

### 3.2 Desktop Linux

Same as Termux, except `python3.12` may already be on `$PATH` and you
can edit `~/.config/Claude/claude_desktop_config.json` directly. If
your distro ships only Python 3.11 or older, install 3.12 from
`deadsnakes` (Ubuntu) or your package manager equivalent — the bridge
uses 3.10+ syntax.

### 3.3 macOS

```bash
brew install python@3.12
git clone https://github.com/tribixbite/x2d
cd x2d
python3.12 -m venv .venv
source .venv/bin/activate
pip install paho-mqtt cryptography requests
```

Then point Claude Desktop's config at the venv interpreter:

```json
{
  "mcpServers": {
    "x2d": {
      "command": "/absolute/path/to/x2d/.venv/bin/python",
      "args": ["-m", "mcp_x2d"],
      "cwd": "/absolute/path/to/x2d"
    }
  }
}
```

### 3.4 Windows

```powershell
winget install Python.Python.3.12
git clone https://github.com/tribixbite/x2d
cd x2d
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install paho-mqtt cryptography requests
```

The Claude Desktop config (at `%APPDATA%\Claude\claude_desktop_config.json`)
should reference the venv:

```json
{
  "mcpServers": {
    "x2d": {
      "command": "C:\\absolute\\path\\to\\x2d\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_x2d"],
      "cwd": "C:\\absolute\\path\\to\\x2d"
    }
  }
}
```

## 4. Remote MCP via SSH tunnel (Termux backend, desktop client)

When the bridge runs on a phone over Termux but you want Claude Desktop
on your laptop:

```json
{
  "mcpServers": {
    "x2d-phone": {
      "command": "ssh",
      "args": [
        "-T",
        "-o", "ServerAliveInterval=30",
        "u0_a364@192.168.1.50",
        "cd ~/git/x2d && python3.12 -m mcp_x2d"
      ]
    }
  }
}
```

Pre-authorize the key with `ssh-copy-id` so SSH doesn't prompt for a
password. The MCP framing (newline-delimited JSON) tunnels cleanly
over SSH stdio.

## 5. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `X2D_BRIDGE` | `<repo>/x2d_bridge.py` | Path to the bridge CLI the MCP server shells out to. |
| `X2D_BRIDGE_PYTHON` | `sys.executable` | Python interpreter used to invoke the bridge. |
| `X2D_DAEMON_HTTP` | `http://127.0.0.1:8765` | Base URL of the bridge daemon for `x2d://state`, `camera_snapshot`, `healthz`, `metrics`. |
| `X2D_DAEMON_TOKEN` | _(none)_ | Bearer token sent to the daemon's HTTP if it was launched with `--auth-token`. |
| `X2D_MCP_CALL_TIMEOUT` | `30` | Per-tool subprocess timeout (seconds). |

## 6. Verifying from inside Claude

Once the server is wired in, ask Claude:

> "Use the x2d-bridge tools. List my printers, then pull current
>  temperatures."

Expected behaviour: Claude calls `list_printers` → receives the JSON
array of `[printer]` sections, then calls `status` and quotes back
the bed/nozzle/chamber temperatures. Sub-30s round-trip on a healthy
LAN.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| MCP indicator shows red dot | Server crashed on startup. Check Claude Desktop's Logs (Help → Logs → MCP). | Usually a missing dep — `pip install paho-mqtt cryptography`. |
| `tools/call status` returns "credentials missing" | `~/.x2d/credentials` not present in the cwd Python opens. | Verify `cwd` in the config block points at the repo root and that `~/.x2d/credentials` exists. |
| `camera_snapshot` errors "daemon unreachable" | The camera daemon isn't running. | Run `python3.12 x2d_bridge.py camera --bind 127.0.0.1:8765 &` first. |
| Tool call hangs >30s | MQTT connect to printer timed out (printer offline / wrong creds / firewall). | `python3.12 x2d_bridge.py status` from a shell to debug. |
