# Multi-printer setup

The bridge daemon, MCP server, web UI, HA publisher, and queue manager
all support multiple printers from one process. Add a `[printer:NAME]`
section per printer in `~/.x2d/credentials`; everything else
auto-discovers them.

## Credentials

```ini
# ~/.x2d/credentials

[printer:studio]
ip     = 192.168.1.42
code   = 12345678
serial = 03ABC0001234567

[printer:garage]
ip     = 192.168.1.43
code   = 87654321
serial = 03DEF0007654321
```

The plain `[printer]` (no name) is also valid — it's reported as
the empty string in API responses. You can mix `[printer]` plus
`[printer:NAME]` sections; the unnamed one becomes the default.

## What runs against multiple printers

* **`x2d_bridge.py daemon`** — spawns one X2DClient per section, all
  sharing one HTTP server on `--http`. State, /state.events, /metrics,
  /healthz, /control/* are all `?printer=NAME` routed.
* **`x2d_bridge.py status` / `pause` / etc.** — pass `--printer NAME`
  (or set `X2D_PRINTER`) to target a specific section.
* **`x2d_bridge.py ha-publish`** — without `--printer`, spawns one
  HAPublisher thread per section in the same process; each gets its
  own HA Device with namespaced topics. Failures are isolated
  per-printer.
* **`x2d_bridge.py serve`** (the `libbambu_networking.so` shim
  endpoint) — registers each printer with `DeviceManager::on_machine_alive`
  so the BambuStudio GUI's Device-tab dropdown lists all of them.
* **Web UI** — printer dropdown in the header re-targets every card.
* **Print queue** — each job is enqueued for a specific printer;
  the per-printer auto-dispatch loops are independent.

## Routing in the API

```bash
# Pull state for one named printer
curl http://127.0.0.1:8765/state?printer=studio

# Pause the studio printer (not garage)
curl -X POST http://127.0.0.1:8765/control/pause?printer=studio

# Subscribe to live SSE for garage
curl -N http://127.0.0.1:8765/state.events?printer=garage

# /metrics has per-printer labels for every gauge
curl http://127.0.0.1:8765/metrics | grep printer=
```

## SSDP auto-discovery

If you don't want to write the credentials file by hand, the bridge's
`serve` mode listens for Bambu's SSDP NOTIFYs on UDP 2021 multicast
and shows you what's on the LAN:

```bash
python3.12 x2d_bridge.py serve --debug 2>&1 | grep ssdp
```

(Then add the matching `[printer:NAME]` section with the discovered
IP + serial.)

## Test harnesses

```bash
PYTHONPATH=. python3.12 runtime/ha/test_multi_printer.py  # 20/20 PASS
```

That harness drives two daemons + two HAPublishers + one MQTT broker
and verifies command isolation, distinct device_ids, disjoint topic
sets, and graceful per-printer teardown.
