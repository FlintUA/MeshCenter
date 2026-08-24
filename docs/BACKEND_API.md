# Backend Protocol v1

Status: **draft, interface-only** (Task 43.5). No implementation exists yet.
`SerialTransport` (Task 44) and `BLETransport` (Task 45) will implement this
contract; the Python interface lives in
[`meshsrv/radio_transport.py`](../meshsrv/radio_transport.py) as an
`abc.ABC` — this document also defines the JSON wire shape the same contract
maps onto once the adapter is moved behind a subprocess boundary (Task 48).
Until then, Core calls the Python interface directly, in-process.

`protocol_version` (currently `1`) is present on every JSON message so the
IPC boundary introduced in Task 48 can detect a Core/adapter version
mismatch instead of failing opaquely.

## Where this comes from

Five call sites in Core (`server.py`, `api/api_chat.py` ×2,
`meshsrv/schedule_actions.py`, `storage/waypoint_sender.py`) import
`meshtastic` (GPLv3) directly today — a real copyleft violation, not a
formality. This protocol is the boundary that isolates all Meshtastic
specifics (Serial today, BLE from Task 45) behind neutral models, so Core
(MIT) never imports `meshtastic` once Task 48 lands.

## Operations

| Method | Purpose | Existing code it replaces |
|---|---|---|
| `connect(descriptor, force=False, timeout=30)` | Open a connection | `SerialInterface(devPath=...)` construction sites |
| `disconnect(timeout=15)` | Close, fully released on return | `interface.close()` |
| `reconnect(timeout=30)` | `disconnect()` + `connect(..., force=True)` | manual retry loops |
| `is_connected()` | Cheap local check | ad hoc `interface is not None` checks |
| `send_text(message, timeout=15)` | One text message | `interface.sendText(...)` |
| `send_packet(payload, destination_id, port_num, want_ack=False, timeout=15)` | Raw application payload | (no current equivalent — escape hatch) |
| `send_messages(messages, timeout=30)` | Batch over one connection | `api/api_chat.py`'s `_process_send_batch` |
| `send_waypoint(waypoint, timeout=15)` | Send + optional notification | `storage/waypoint_sender.py` |
| `get_nodes(timeout=15)` | Full node list | `--info`'s "Nodes in mesh" block |
| `get_local_node(timeout=15)` | Just the local node | `interface.localNode` |
| `get_channels(timeout=15)` | Channel list | `api/api_chat.py`'s `discover_radio_channels()` |
| `get_metadata(timeout=15)` | Firmware/device metadata | `--info`'s "Metadata" block |
| `set_device_time(epoch_seconds, timeout=15)` | One-way clock sync | `meshsrv/node_time_sync.py` / `server.py`'s `_attempt_node_time_sync()` |
| `get_connection_info()` | Non-blocking state read | `is_radio_available()` / `RADIO_IDENTITY_RESULT` |
| `close()` | Final teardown | `interface.close()` in `finally` blocks |

`get_channels` was not in the original Task 43.5 operation list handed
down from the plan document, even though `channel` was already listed
among the neutral models below. `api/api_chat.py`'s
`discover_radio_channels()` reads exactly this today — without a
corresponding method, that functionality has nowhere to go in Task 44. This
mirrors the gap the plan already caught and fixed once, for `send_waypoint`
and `set_device_time`. **Flagged for confirmation before Task 44**, not
assumed.

## Timeouts

Hardened by the live Task 43 BLE test on TAP2: `meshtastic`'s own
`BLEInterface` connect (`_waitConnected(timeout=60.0)` internally) hung for
over 90 seconds with no response and required an external `kill -9` to
recover — the library's documented internal timeout did not fire.

This is a **two-tier guarantee, and the tiers are not equivalent** — do not
read "timeout" as "the operation is guaranteed to actually stop" before
Task 48. `BLEInterface`/`BLEClient` runs its own `asyncio` event loop inside
a daemon `Thread` (confirmed by reading `ble_interface.py` in Task 43,
finding #4) — CPython has no API to force-terminate another thread, only
`Thread.join(timeout)`, which returns without the thread actually stopping.
Only an OS process can be `SIGKILL`ed.

1. **Non-blocking return (mandatory from Task 44/45 onward).** Every method
   that accepts `timeout` must return or raise `TransportError(TIMEOUT)` to
   its caller at or before that many seconds, enforced from outside the
   underlying library call (e.g. `future.result(timeout=...)` on a call
   running in a watchdog thread). The wrapped library's own internal
   timeout is demonstrated-insufficient on its own and must never be the
   only thing Core relies on to detect a hang.
2. **Resource release (only guaranteed from Task 48 onward)**, once the
   adapter is an isolated subprocess and a timeout can actually `SIGKILL`
   it. Before that: when tier 1 fires on a stuck call, the orphaned
   background thread — its event loop, its live bleak/GATT session — keeps
   running unsupervised. It is *abandoned*, not killed. This is the same
   failure mode this whole section is named after: a leftover OS-level BLE
   session from a previous attempt blocking the next `connect()` until an
   out-of-band `bluetoothctl disconnect`.

**Which one is implemented at this stage: tier 1 only.** An implementation
that wants real resource release before Task 48 has to arrange it itself —
e.g. run the risky library call in its own short-lived subprocess (which
*can* be `SIGKILL`ed) rather than a thread. That is an implementation
decision for Task 44/45, not something this interface guarantees for them.

## Reconnect / teardown

Also from the live test:

- A stale OS-level BLE bond/GATT session left connected from a previous
  attempt silently blocked a fresh `--info` connect until explicitly
  disconnected with `bluetoothctl disconnect`.
- A live USB-serial listener had to be fully stopped before a BLE connect
  to the *same physical node* would succeed at all.

`connect()` must not assume the radio, or the local Bluetooth/serial stack,
is in a clean state. `force=True` tells the implementation to tear down any
lower-layer connection it knows about (OS bluez session, held serial fd,
previous listener subprocess) before attempting a new one. `disconnect()`
and `close()` must not return until that teardown has *actually completed* —
not merely been requested — so a caller switching transports (e.g. Serial →
BLE on the same node, Task 46/47) can safely construct the next transport
immediately after `close()` returns, with no extra out-of-band wait.

**This guarantee inherits the same tier-1/tier-2 split.** It holds whenever
`disconnect()`/`close()` completes within its own `timeout`. If
`disconnect()`/`close()` itself times out, the caller gets
`TransportError(TIMEOUT)` promptly (tier 1), but — before Task 48 — there is
no guarantee the lower-layer session was actually released. A subsequent
`connect(force=True)` may still fail against a radio/OS stack that thinks
it's already connected, exactly like the live Task 43 finding. There is no
interface-level fix for this before process isolation exists — it is a
known, named gap carried forward to Task 44/45, not an oversight to paper
over.

## Neutral models

No `meshtastic.*` type, no protobuf object, no `SerialInterface`/
`BLEInterface` reference appears in any signature or model below — see Task
43 finding #6 (nowhere in the current codebase does a protobuf/`Node`/
`Interface` object escape the function that obtained it; every call site
already reduces to primitives at the point of use, e.g.
`getattr(sent_packet, "id", None)` → `int(...)`), so this boundary is not
expected to require new reduction logic in Task 44/45 — just relocating
logic that already exists.

- `ConnectionDescriptor` — `type` (`serial`/`bluetooth`/`tcp`), `address`,
  `label`
- `ConnectionInfo` — `state`, `descriptor`, `node_id`, `connected_since`,
  `last_error`
- `ConnectionEvent` — `state`, `descriptor`, `detail`, `timestamp` (polled
  via `get_connection_info()` in this stage — see "Events" below)
- `NodeUser` / `NodeInfo` — id, names, hw model, telemetry sub-dicts,
  position
- `ChannelInfo` — index, name, role
- `OutgoingMessage` / `SendResult`
- `OutgoingWaypoint` / `WaypointResult`
- `TelemetryEvent` — node_id, kind, metrics, timestamp
- `TransportError` — `code` (`TransportErrorCode` enum), `message`

## Events

No push/callback mechanism in this version. Task 47.3 requires that a BLE
disconnect not crash the rest of the app, but with `listen_meshtastic()`
staying inside Core through Task 48 ("Stage A" per section 8.3 of the plan),
Core observes transport state changes by polling `get_connection_info()` —
the same pattern `is_radio_available()` / `RADIO_IDENTITY_RESULT` already
use today. A normalized event stream (`message_received`, `node_updated`,
`telemetry`, `connection_state`) is explicitly deferred to Stage B (Task
49+, "Stage B listener").

## JSON wire shape (for Task 48's subprocess IPC boundary)

Not used yet — Core calls the Python interface in-process through Task 47.
Documented now so Task 48 has no interface redesign to do, only a
serialization layer.

```jsonc
// Request
{
  "protocol_version": 1,
  "operation": "send_text",
  "params": {
    "text": "hello mesh",
    "destination_id": "^all",
    "channel_index": 0,
    "want_ack": false,
    "reply_id": null
  },
  "timeout": 15.0
}
```

```jsonc
// Response — success
{
  "protocol_version": 1,
  "ok": true,
  "result": {
    "accepted": true,
    "packet_id": 123456789,
    "error": null
  }
}
```

```jsonc
// Response — failure (structured, no traceback in the public protocol)
{
  "protocol_version": 1,
  "ok": false,
  "error": {
    "code": "timeout",
    "message": "connect() exceeded 30.0s"
  }
}
```

```jsonc
// connect()
{
  "protocol_version": 1,
  "operation": "connect",
  "params": {
    "descriptor": {
      "type": "bluetooth",
      "address": "3C:DC:75:6F:99:61",
      "label": "FLT2_9960"
    },
    "force": true,
    "timeout": 30.0
  }
}
```

```jsonc
// get_connection_info() response
{
  "protocol_version": 1,
  "ok": true,
  "result": {
    "state": "connected",
    "descriptor": {
      "type": "bluetooth",
      "address": "3C:DC:75:6F:99:61",
      "label": "FLT2_9960"
    },
    "node_id": "!756f9960",
    "connected_since": 1787557475.0,
    "last_error": null
  }
}
```

## Batching

`send_messages()` holds **one** underlying connection open for the entire
batch — this is existing, load-bearing behavior
(`api/api_chat.py`'s `_process_send_batch`, and the
`BATCH_ACCUMULATION_WINDOW_SECONDS` draining logic in its `send_worker`)
that must not regress when this moves behind the transport interface.
Pausing the listener and reconnecting per-message previously produced
"Timed out waiting for connection completion" failures under quick
back-to-back sends.

## Explicitly out of scope for this version

Matches section 10 of the plan document, restated here for the parts that
touch this protocol directly: pairing/PIN UI, full bonding recovery after
reboot, live RSSI, a watchdog that recreates the interface, a diagnostics
panel, "forget device", TCP transport, multi-radio. None of these require
a method on `RadioTransport` today; adding one prematurely was avoided.
