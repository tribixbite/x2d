# Bridge ↔ shim wire protocol

`libbambu_networking.so` (the shim) talks to `x2d_bridge.py serve` (the
bridge daemon) over a single Unix-domain socket at
`$HOME/.x2d/bridge.sock` (path can be overridden via the
`X2D_BRIDGE_SOCK` env var inherited from the parent BambuStudio process).

## Framing

Every message is one **JSON object on one line**, terminated by a single
`\n`. UTF-8. Both directions use the same framing. Reads should be done
with a buffered line-reader; never assume one read returns one message.

Both ends MUST treat unknown fields as ignored, not as errors — we want to
roll out new ops without breaking older shims/bridges.

## Message kinds

There are three kinds of messages:

| Kind | Direction | Has `id` | Has reply |
|------|-----------|----------|-----------|
| `req` (request) | shim → bridge | yes (uint64, monotonic per shim PID) | yes — exactly one `rsp` with the same `id` |
| `rsp` (response) | bridge → shim | yes — matches a prior `req.id` | n/a |
| `evt` (event) | bridge → shim | no | none — fire-and-forget |

Every message has the field `kind` set to one of those three strings.

### Request (`kind:"req"`)

```json
{ "kind":"req", "id":42, "op":"connect_printer",
  "args":{"dev_id":"03ABC...", "dev_ip":"192.168.0.138",
          "username":"bblp", "password":"abcd1234", "use_ssl":true} }
```

`op` is a short verb identifying which procedure to invoke. `args` is an
op-specific object.

### Response (`kind:"rsp"`)

```json
{ "kind":"rsp", "id":42, "ok":true, "result":{...} }
```

```json
{ "kind":"rsp", "id":42, "ok":false,
  "error":{"code":-2, "message":"connect failed: timeout"} }
```

`ok` is a boolean. When `false`, `result` is absent and `error` carries a
short message + an integer `code` matching the
`BAMBU_NETWORK_ERR_*` constants from `bambu_networking.hpp` (negative
integers; `0` is success).

### Event (`kind:"evt"`)

```json
{ "kind":"evt", "name":"local_message",
  "data":{"dev_id":"03ABC", "msg":"<json blob>"} }
```

`name` identifies which callback the shim should fire on the host's GTK
thread. See "Event names" below.

## Operations (req → rsp)

All op argument and result schemas are described as JSON shapes. Strings
are UTF-8. Integers are 64-bit signed.

### `hello` — handshake

Shim sends this immediately after connecting. Bridge MUST reply before
processing any other op.

* Args: `{ "shim_version": <int>, "abi": <int> }` (current ABI = 1)
* Result: `{ "bridge_version": <string>, "abi": <int>,
            "default_printer": <string|null> }`

If `abi` mismatches the bridge bails with `ok:false code:-100`.

### `connect_printer` — open MQTT to a LAN printer

* Args: `{ "dev_id": <string>, "dev_ip": <string>,
           "username": <string>, "password": <string>,
           "use_ssl": <bool> }`
* Result: `{}` on success.
* Side-effects: bridge starts a paho client to the printer, subscribes
  to `device/<dev_id>/report`, and from then on emits
  `evt:local_message` for every received state push.
* On disconnect (TCP/TLS error, auth fail, timeout) bridge emits
  `evt:local_connect` with `status: ConnectStatusLost (2)`.

### `disconnect_printer`

* Args: `{}`
* Result: `{}`
* Stops the paho client and clears the local-message subscription.

### `send_message_to_printer` — signed publish

* Args: `{ "dev_id": <string>, "json": <string>, "qos": <int>,
           "flag": <int> }`
* Result: `{}` on success.
* `json` is the un-headered payload BambuStudio constructed; the bridge
  always wraps it with the RSA-SHA256 `header` block before publishing
  (matches the firmware's signed-MQTT requirement).
* `flag` mirrors `BBL::MessageFlag` — `MSG_SIGN | MSG_ENCRYPT`. We
  always sign; encryption is reserved for a future firmware that
  requires it.

### `start_local_print` — upload + start print on LAN

* Args: full `PrintParams` serialized as JSON. Critical fields:
  `{ "dev_id":..., "dev_ip":..., "username":"bblp", "password":...,
     "use_ssl_for_ftp":true, "use_ssl_for_mqtt":true,
     "filename":"<absolute path to .gcode.3mf>",
     "ams_mapping":"[ints, …]", "task_use_ams":<bool>,
     "task_bed_type":"<plate id>", "task_bed_leveling":<bool>,
     "task_flow_cali":<bool>, "task_vibration_cali":<bool>,
     "task_layer_inspect":<bool>, "task_record_timelapse":<bool> }`
* Result: `{}` on success; bridge does the FTPS upload then publishes a
  signed `project_file` MQTT command.
* Streams progress as `evt:print_status` while running.

### `start_send_gcode_to_sdcard` — upload only

Same args as `start_local_print` but skips the MQTT publish. Result: `{}`.

### `subscribe_local` — start/stop pushall polling

* Args: `{ "dev_id": <string>, "interval_s": <int>, "enable": <bool> }`
* Result: `{}`. While enabled, bridge polls signed `pushall` every
  `interval_s` seconds so the GUI's printer-status panel stays warm.

### Cloud / catalog ops (return success-with-empty)

For LAN-only operation we return `ok:true` with empty results. The shim
should never even send these; they're listed here so the bridge's
op-dispatch table is exhaustive and forward-compatible.

* `connect_server` → `{}`
* `is_user_login` → `{ "logged_in": false }`
* `get_user_id` → `{ "id": "" }`
* `get_user_presets` → `{ "presets": {} }`
* `get_user_tasks` → `{ "tasks": [] }`
* `start_print` (cloud) → `{}`  (bridge will route to start_local_print
  if `connection_type` is `lan` in PrintParams)
* All other cloud entry points behave the same way.

## Events (bridge → shim)

These are async pushes. The shim translates each into the matching
NetworkAgent callback — but only after marshalling the call onto the
host's GTK thread via the host-registered `QueueOnMainFn`.

| Event name | Data shape | Maps to host callback |
|------------|-----------|------------------------|
| `local_message` | `{"dev_id":..., "msg":"<json>"}` | `OnMessageFn` (set via `set_on_local_message_fn`) |
| `local_connect` | `{"status":<0\|1\|2>, "dev_id":..., "msg":...}` | `OnLocalConnectedFn` (set via `set_on_local_connect_fn`) |
| `printer_connected` | `{"topic":...}` | `OnPrinterConnectedFn` (set via `set_on_printer_connected_fn`) |
| `print_status` | `{"status":..., "code":..., "msg":...}` | The `OnUpdateStatusFn` passed to `start_local_print` |
| `subscribe_failed` | `{"topic":...}` | `GetSubscribeFailureFn` (set via `set_on_subscribe_failure_fn`) |
| `http_error` | `{"http_code":..., "body":...}` | `OnHttpErrorFn` (set via `set_on_http_error_fn`) |

## Lifecycle

1. Shim is `dlopen`'d by BambuStudio.
2. Host calls `bambu_network_create_agent(log_dir)`. Shim returns an
   opaque `void*` handle.
3. Host registers callbacks via `set_on_*_fn`. Shim stashes them in the
   handle.
4. Host calls `bambu_network_start(agent)`. Shim:
   * Reads `X2D_BRIDGE_SOCK` (default `$HOME/.x2d/bridge.sock`).
   * Spawns a background daemon if no socket exists yet (`x2d_bridge.py
     serve --sock $X2D_BRIDGE_SOCK`).
   * Connects, sends `hello`, awaits `rsp`.
   * Spins up the worker thread that owns the socket.
5. Host calls `connect_printer` → shim sends `req:connect_printer` →
   bridge opens paho → returns `rsp{ok:true}` → shim returns `0`.
6. Bridge starts emitting `evt:local_message` as the printer publishes
   state. Shim marshals each to GTK.
7. On host calling `bambu_network_destroy_agent`, shim sends a `goodbye`
   request, waits up to 1s, then closes the socket and joins the worker
   thread.

## Error handling

* If the socket dies, the worker thread reconnects with exponential
  backoff (1s → 2s → 4s → … capped at 30s). During the gap, sync ops
  return `BAMBU_NETWORK_ERR_DISCONNECT_FAILED (-3)`; events are dropped.
* If the bridge daemon isn't running and we can't spawn it, `start`
  returns `BAMBU_NETWORK_ERR_INVALID_HANDLE (-1)` and the GUI shows the
  "Network Plug-in is not detected" notification.
* Per-op timeouts: 8s for `connect_printer`, 5s for `send_message_to_printer`,
  300s for `start_local_print` (large 3MFs over slow Termux LAN).
