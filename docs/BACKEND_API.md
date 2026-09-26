# Backend Protocol v1

Status: **implemented and live** — `SerialTransport` (Task 44) and
`BLETransport` (Task 45) implement this contract; the Python interface lives
in [`meshsrv/radio_transport.py`](../meshsrv/radio_transport.py) as an
`abc.ABC`. Task 48's process-isolation boundary has also landed: Core
(`server.py`) constructs `AdapterIPCTransport`/`AdapterSupervisor`
(`meshsrv/adapter_ipc_client.py`) and wires them into `TransportRouter` —
the concrete transports run in a separate OS process (the "adapter"
subprocess), not in-process inside Core, and this document's "JSON wire
shape" section below is the real, in-use serialization for that boundary
(implemented in `meshsrv/ipc_protocol.py`), not a forward-looking sketch.

`protocol_version` (currently `1`) is present on every JSON message so the
IPC boundary introduced in Task 48 can detect a Core/adapter version
mismatch instead of failing opaquely.

## Where this comes from

Five call sites in Core (`server.py`, `api/api_chat.py` ×2,
`meshsrv/schedule_actions.py`, `storage/waypoint_sender.py`) used to import
`meshtastic` (GPLv3) directly — a real copyleft violation, not a formality.
This protocol is the boundary that isolates all Meshtastic specifics
(Serial, BLE) behind neutral models, and Task 48 landing closed that
violation for real: `import meshtastic`/`from meshtastic ...` no longer
appears anywhere in Core (verified by grep, not assumed — CI now enforces
this on every PR/push, see `.github/workflows/ci.yml`'s "GPLv3
license-boundary check" step). The five call sites above now go through
`TransportRouter`/`RadioTransport` instead.

## Operations

| Method | Purpose | Existing code it replaces |
|---|---|---|
| `connect(descriptor, force=False, timeout=30)` | Open a connection | `SerialInterface(devPath=...)` construction sites |
| `disconnect(timeout=15)` | Close, fully released on return | `interface.close()` |
| `reconnect(timeout=30)` | `disconnect()` + `connect(..., force=True)` | manual retry loops |

IPC-only additions (`meshsrv/adapter_ipc_client.py`, not part of the `RadioTransport` ABC): `reconnect` may carry an optional `descriptor` (the endpoint to reconnect to - an adapter process forgets its last `connect()` whenever it is killed and respawned, so Core always supplies the accepted profile's address), and a `connection_info` operation returns the adapter's own in-memory view of the link (no radio I/O) so Core can refresh its cache-only `get_connection_info()` and notice a session whose reader thread has died.
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
`discover_radio_channels()` reads exactly this today. This mirrored the gap
the plan already caught and fixed once, for `send_waypoint` and
`set_device_time` — **resolved**: `get_channels` is implemented in both
`SerialTransport` and `BLETransport` (`adapters/meshtastic/`), not just
declared on the interface.

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

**Both tiers are implemented now that Task 48 has landed.** Tier 1
(non-blocking return) has held since Task 44/45; tier 2 (resource release)
is real as of Task 48's process boundary — `AdapterSupervisor.call()`
(`meshsrv/adapter_ipc_client.py`) waits up to the caller's declared budget
for a response and `SIGKILL`s the adapter subprocess on expiry, so a
wedged call's orphaned resources (event loop, live bleak/GATT session) go
away with the process, not just get abandoned in a still-running thread.
Tier 2 covers a fifth case too (radio-stability review, P0-A): the
adapter's own internal `TimeoutEnforced` watchdog
(`adapters/meshtastic/_timeout_support.py`) can fire *first* and report
the timeout back as a well-formed response instead of the caller-side
reader ever timing out waiting for one — `AdapterSupervisor.call()`
recognizes this specific shape (`ok: false`, `error.code == "timeout"`)
and kills the subprocess before returning, same as the no-response/dead-
process/malformed-output cases, since the daemon thread that was running
the timed-out operation is only abandoned inside it, never stopped,
exactly like the no-response case above. Every other `ok: false` domain
error (`NOT_CONNECTED`, `UNSUPPORTED`, `IDENTITY_MISMATCH`, ...) means the
adapter's own call stack already unwound cleanly and does not trigger a
kill — only `TIMEOUT` specifically indicates an abandoned daemon thread
with unknown resource-ownership state.
BLE cleanup on a kill is handled explicitly too: a `SIGKILL`'d adapter's
serial file descriptors are reclaimed by the kernel automatically, but a
BLE GATT session (brokered through BlueZ over D-Bus) can outlive the
process that opened it, so Core runs `bluetoothctl disconnect <address>`
itself when a kill happens mid-BLE-operation (see
`meshsrv/adapter_ipc_client.py`'s module docstring for the full detail,
including the two independent layers — `KillMode=control-group` and
`PR_SET_PDEATHSIG` — that separately cover Core itself dying with the
adapter still alive).

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
- `ReceivedTextEvent` / `ReceivedWaypointEvent` — one inbound packet, plain types only (see "Received events" below)
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

## Received events (inbound traffic) — models only so far

Field set and envelope follow the TCP-inbound plan (sections 21-27).

`ReceivedTextEvent` (from_node_id, to_node_id, text, received_at,
**local_radio_node_id**; optional: packet_id, from_num, to_num, channel_index,
reply_id, rx_time, rx_rssi, rx_snr, hop_limit, hop_start, relay_node) and
`ReceivedWaypointEvent` (waypoint_id, sender_id, name, description, received_at,
**local_radio_node_id**; optional: packet_id, latitude, longitude, icon,
expire_at, channel_index) in `meshsrv/radio_transport.py`. Frozen, validated at
construction (plain `str`/`int`/`float`/`None` only - bytes, protobuf objects
and `bool`-as-int are rejected), and on the wire only through the explicit
per-field functions in `meshsrv/ipc_protocol.py`. Never `asdict()`: the
library's packet carries `raw` (a protobuf `MeshPacket`), `decoded.payload`
(bytes) and `decoded.waypoint.raw` (a string), none of which may cross the
boundary.

`local_radio_node_id` is the radio the event came from. It cannot be omitted or
empty; Core compares it with the active accepted profile before persisting and
drops a mismatch, so events buffered from radio A are never written into radio
B's profile. `latitude`/`longitude` are optional so a remote waypoint *delete*
(expire 0, no coordinates) is representable; ingesting it is out of the first
inbound PR's scope.

Node ids are `!xxxxxxxx` (or `^all` for a broadcast `to_node_id`) built from the
packet's NUMERIC `from` / `to` (also carried as `from_num` / `to_num`), not from
the library's `fromId`, which is `None` while the sender is not yet in the local
NodeDB. `rx_time` is the radio's clock, `received_at` the adapter's.

Wire shape of one event - a discriminated envelope:

```json
{"kind": "text",     "text":     {"from_node_id": "!1fa065f0", "to_node_id": "^all", "text": "...", ...}}
{"kind": "waypoint", "waypoint": {"waypoint_id": 4242, "sender_id": "!1fa065f0", "name": "...", ...}}
```

`drain_received(limit=100, timeout=5.0)` returns a `ReceivedBatch(events, dropped,
malformed, connection_generation)`; on the wire
`{"events": [<envelope>...], "dropped": n, "malformed": n, "connection_generation": n|null}`.
`dropped` = events the transport's bounded buffer discarded (oldest first) since
the previous drain; `malformed` = events discarded because they could not be
decoded - one bad event never costs the rest of the batch. Unknown keys in
input are dropped, an unknown `kind` raises. The method is optional: the default
raises `UNSUPPORTED` (so "cannot receive" is distinguishable from "nothing
received" = an empty batch). **`TCPTransport` implements it**, and Core consumes it
through `meshsrv/inbound_worker.py` (below); Serial and Bluetooth keep the default.

### TCP inbound worker (Core side)

`radio_inbound_worker` (thread, started for every transport) polls
`transport_router.drain_received(limit=100)` about once a second and hands each
event to the shared ingest (`meshsrv/inbound_events.py`). It ingests only while
`inbound_eligibility()` is true - the accepted radio is TCP, identity is `MATCH`
and the router is actually on TCP - re-read every tick, so a Settings switch
needs no restart; otherwise it idles (5 s ticks). `BUSY` / `NOT_CONNECTED` /
`ADAPTER_UNAVAILABLE` / `TIMEOUT` / `UNSUPPORTED` from the drain are waited out
silently (one log line per five minutes, never an ERROR per second); one event
that cannot be ingested is dropped on its own (only its exception *type* is
logged - a message can be inside the text); a full batch is followed by another
in the same tick (at most 5). Nothing here logs message text.

Every event must name the radio it came from (`local_radio_node_id`); the ingest
drops it, with a warning and a counter, unless it is the active accepted radio -
so events buffered for radio A are never written into radio B's profile. When a
session is torn down because its identity was refused (MISMATCH / NOT_FOUND),
whatever the adapter queue captured is drained and discarded.

Counters (counts and states only) are in `GET /api/radio_health` under
`inbound`: `worker` (drained_events, text_events, waypoint_events, ingest_errors,
malformed_events, overflow_dropped, discarded_on_identity_refusal, soft_errors,
status, waiting_reason, last_drain_age_s, connection_generation) and `ingest`
(text_stored, duplicates, waypoint created/updated/duplicate,
stale_identity_dropped). The adapter's own `received_*` / `queue_depth` counters
stay adapter-side (`TCPTransport.get_receive_stats()`).

Delivery is best-effort with Core-side duplicate suppression: an event captured
while the TCP link is down, or buffered only inside an adapter process that is
killed before the next drain, can be lost. Out of scope (later stages): NodeInfo /
position / telemetry, routing ACKs, Bluetooth receive, remote waypoint deletion.

### TCP capture (adapter side)

`TCPTransport` subscribes to `meshtastic.receive.text` and
`meshtastic.receive.waypoint` **once per instance lifetime** (not per
connect/reconnect; unsubscribed on the final `close()`). `pub` is global to the
adapter process, which also hosts the Serial and BLE transports, so the callback
accepts an event only if it came from this transport's current interface (or the
one whose handshake is still in flight). Each accepted packet is normalized
field by field (never reading `raw` or `decoded.payload`) into a neutral event
and appended to a bounded queue (256): when full, the OLDEST event is dropped,
counted, and reported in the next batch (`dropped`), with at most one WARNING per
minute. An undecodable packet is counted in `malformed`, never fatal, and never
logged (message text is private).

`drain_received` is the ordinary request/response IPC operation
`{"operation": "drain_received", "params": {"limit": 100}}` ->
`{"ok": true, "result": {"events": [...], "dropped": n, "malformed": n,
"connection_generation": n}}`. It reads memory only - no radio I/O - so polling
it about once a second costs the radio nothing, and it adds no unsolicited
frame to stdout (Protocol v1 stays strictly one response per request). Through
`TransportRouter` it takes the same lock and bounded wait as every other
operation (a switch or long reconnect makes it `BUSY`, never a hang). The queue
is not cleared by disconnect/reconnect; it dies with the adapter process
(delivery is best-effort with Core-side duplicate suppression).

Receive contract checked against the real library with
`adapters/meshtastic/verify_receive_topics.py` (a real `FromRadio` frame through
`MeshInterface`, in a throwaway venv per version): `meshtastic.receive.text` and
`meshtastic.receive.waypoint` each fire exactly once with the same packet shape,
and `meshtastic.receive.data.TEXT_MESSAGE_APP` / `.WAYPOINT_APP` do not fire (the
library replaces that topic name for known protocols), on **2.7.9, 2.7.10 and
2.7.11** - every release inside the pinned `>=2.7.9,<2.8.0`. Re-run it when that
pin moves.

## JSON wire shape (Task 48's subprocess IPC boundary — implemented, in use)

Implemented in `meshsrv/ipc_protocol.py` (Core side) and
`adapters/meshtastic/ipc_server.py` (adapter side); this is the real,
current serialization for every call between Core and the adapter
subprocess, not a forward-looking sketch.

One deliberate addition beyond what was originally sketched here: every
request also carries a `transport_type` field (`"serial"` or
`"bluetooth"`), telling the adapter subprocess which of its two live
transport instances (`SerialTransport`/`BLETransport`) to route this call
to. This keeps the adapter subprocess stateless about "which transport is
active" — `TransportRouter` on Core's side already tracks that, and
forwarding it on every request means Core and the adapter can never
disagree about it after a partial failure the way two independently
tracked "active" flags could (see `adapters/meshtastic/ipc_server.py`'s
module docstring, "STATELESS ROUTING"). Shown below on the `connect()`
example.

```jsonc
// Request - params.message is a full OutgoingMessage dict, nested under
// a "message" key (matching AdapterIPCTransport.send_text()'s actual
// {"message": outgoing_message_to_dict(message)} call, not a flat dict
// of the message's own fields)
{
  "protocol_version": 1,
  "operation": "send_text",
  "transport_type": "serial",
  "params": {
    "message": {
      "text": "hello mesh",
      "destination_id": "^all",
      "channel_index": 0,
      "want_ack": false,
      "reply_id": null
    }
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
// connect() - transport_type is the stateless-routing addition, see above.
// `timeout` is top-level only, not duplicated inside `params` - `params`
// here is exactly {descriptor, force}, matching AdapterIPCTransport.
// connect()'s actual call site (meshsrv/adapter_ipc_client.py).
{
  "protocol_version": 1,
  "operation": "connect",
  "transport_type": "bluetooth",
  "params": {
    "descriptor": {
      "type": "bluetooth",
      "address": "3C:DC:75:6F:99:61",
      "label": "FLT2_9960"
    },
    "force": true
  },
  "timeout": 30.0
}
```

```jsonc
// connect()'s response - this ConnectionInfo shape is what populates
// AdapterIPCTransport's local cache, which get_connection_info() then
// serves from directly (Task 48 design decision: get_connection_info()
// itself is non-blocking/cache-only and never crosses the IPC boundary at
// all - see AdapterIPCTransport.get_connection_info() in
// meshsrv/adapter_ipc_client.py, "never cross the IPC boundary, per the
// approved investigation report"). Shown here as a response because this
// exact shape is real wire traffic on every connect()/reconnect() call,
// just not one this specific method name triggers itself.
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
