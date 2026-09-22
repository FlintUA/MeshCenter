# `server.py` decomposition audit (D0)

**Status:** read-only audit, no behavior changed. Produced against `origin/main` @ `2adf32a` ("docs(readme): tie Quick Install's .local address to the imager hostname (#275)").

**Method:** every line of `server.py` (6,233 lines) was read directly (not sampled) and cross-checked against `tests/` via grep for direct references. Line numbers below are stable against that commit; they will drift on the next merge to `server.py` — re-grep before relying on them for an actual extraction PR (see `[[hex_registry.json line numbers: compute last]]`-style caution: this file itself will go stale the moment `server.py` changes).

This document answers "what is in `server.py` and what does each piece need" — not "how do we split it." A proposed PR sequence is at the bottom, informed by this inventory plus `dependency-map.md`.

---

## 1. Module-level import side effects (what happens merely by `import server`)

This is the single most important fact for D1+ bootstrap decomposition: **`import server` is not side-effect-free.** Importing the module (before any route runs, before `start_runtime()` is ever called) does all of the following, top to bottom, at module scope:

| Order | Lines | Side effect | Can it fail the whole import? |
|---|---|---|---|
| 1 | 6–89 | Imports `camera`, `telemetry`, `meshsrv.*`, `api.*`, `system.cpu_history`, `storage.*`, `hardware.hardware_config`, `weather.*` — pulls in the entire dependency graph transitively (see `dependency-map.md` for what each of those imports in turn) | Yes — any import error anywhere in that graph is fatal |
| 2 | 90–98 | `from config import *` | Yes — `exit(1)` if `config.py` is missing |
| 3 | 151–196 | `resolve_app_version()` shells out to `git describe` (subprocess, `cwd=PROJECT_DIR`, 5s timeout each, two attempts) | No — falls back to `"dev"` on any failure |
| 4 | 203–209 | `resolve_meshtastic_cli()` — resolves the CLI binary path (adapter venv → interpreter-adjacent → project venv → `~/.local/bin` → `PATH`) | **Yes** — `SystemExit(1)` if the CLI binary can't be found anywhere. This is deliberate (CLAUDE.md: "nothing radio-related can ever work without it") but means **`import server` cannot succeed in an environment with no `meshtastic` CLI installed anywhere on the search path** — directly relevant to a Docker Core image that doesn't bundle the adapter venv |
| 5 | 228–252 | `resolve_serial_port()` — resolves/validates `MESHTASTIC_PORT` | No — degrades gracefully, prints a warning, continues with a possibly-nonexistent port string |
| 6 | 266–267 | `os.makedirs(DATA_DIR, exist_ok=True)` | No |
| 7 | 272–273 | `update_service.configure(UPDATE_CHECK_FILE)` — sets a module-level path in `meshsrv/update_service.py` | No |
| 8 | 277–325 | `InstanceManager(INSTANCE_FILE).load_or_create(...)` — **reads and writes `data/instance.json`** (creates it on first run, including `os.uname().nodename`) | Yes — `SystemExit(1)` on any exception |
| 9 | 328–367 | `ProfileManager(DATA_DIR).ensure_profile(...)` — **creates `data/profiles/<id>/` and its files if missing, migrates legacy flat files into it, resolves `HISTORY_FILE`/`NODES_FILE`/`CHATS_FILE`/`WAYPOINTS_DB_FILE`/etc. as module globals**; `telemetry.configure_storage(...)`; `DeviceManager(PROFILE_DATA_DIR).load_or_create()` | Yes — `SystemExit(1)` on any exception. This is real, non-idempotent filesystem mutation happening at **import time**, driven by whatever `config.py`/`instance.json` on disk says — a serious concern for any test or container context that imports `server` without meaning to touch real profile data (this is exactly why `tests/conftest.py` redirects `DATA_DIR` to a temp dir before importing) |
| 10 | 458–461 | Creates `data/screenshots/` | No |
| 11 | 463 | `app = Flask(__name__)` | No |
| 12 | 464 | `WaypointStore(WAYPOINTS_DB_FILE)` — opens/creates the SQLite file | Possibly |
| 13 | 470–492 | `_load_or_create_secret_key()` — **reads or writes `data/secret_key.txt`**, `chmod 0o600` | No (best-effort) |
| 14 | 506–507 | `load_auth_state(AUTH_FILE, ...)` — **reads/creates `data/auth.json`** | Depends on `api_auth.py` |
| 15 | 541–727 | Ten `register_*_routes(...)` calls (camera, camera_manager, system, cpu_history, updates, attachments, hardware_display, hardware_i2c, hardware_bme280) plus `hardware_config.reconcile_pending(DATA_DIR)` — registers Flask routes and, for hardware ones, reconciles pending-setup state | Varies — camera routes construct nothing heavyweight yet (lazy), but `hardware_config.reconcile_pending()` does real I2C/filesystem work |
| 16 | 560–572 | Builds `epaper_config` (reads `data/epaper_config.json`), `GpioRegistry`, and `display_manager` via `build_display_manager()` — **CLAUDE.md confirms this never touches SPI/GPIO itself**, only `.start()` does (called later, gated) | No |
| 17 | 803–946 | Module globals: `state_lock`, `radio_lock`, `messages`, `nodes`, `chats`, `settings`, `pause_listen`, then **constructs `listener_supervisor` (`SerialPortSupervisor`)**, **`adapter_supervisor` (`AdapterSupervisor`)**, `serial_ipc_transport`/`ble_ipc_transport` (`AdapterIPCTransport`), `transport_router` (`TransportRouter`) | No — these are just object construction, no subprocess spawned yet |
| 18 | 3469–3475 | `radio_connection_manager = RadioConnectionManager(...)` | No |
| 19 | 4459–4763 | Six more `register_*_routes(...)` calls (chat, weather incl. constructing both `OpenWeatherProvider`/`WeatherApiProvider` singletons, node_tools, node_icon, waypoints, auth, settings, meshtastic) | No (route registration + provider object construction; providers don't hit the network until first request) |
| 20 | 4765+ | ~230 `@app.route` decorators execute (route registration into the Flask URL map) | No |

**None of steps 1–20 start a background thread or open the serial port.** The listener subprocess, all `threading.Thread(...)` workers, and `mca_runtime.start_attachments_service()` only start inside `start_runtime()` (§4), which `wsgi.py` calls explicitly and which `tests/conftest.py` deliberately never calls. This is the load-bearing fact that makes the whole test suite possible without hardware — and the fact any Docker "Core has no serial device" story has to preserve.

**Implication for Docker/D-series work:** steps 4, 8, 9 make `import server` itself fail or mutate disk in ways that assume a specific filesystem layout and an installed CLI binary. A container split needs either (a) config/env-driven stubs for these three steps, or (b) to accept that Core's container image always needs the adapter CLI resolvable even though it never calls it directly (today's actual production behavior — see §7 GPL blockers).

---

## 2. Global mutable state inventory

| Global | Type | Guarded by | Written from |
|---|---|---|---|
| `messages` | `list[dict]` | `state_lock` | `add_message`, `load_messages`, `api_clear_chat`, `api_delete_chat`, `api_delete_all_dm`, `update_message_status`, `reconcile_interrupted_sends`, `ack_timeout_worker`, `api/api_chat.py` (via DI) |
| `nodes` | `dict[node_id, dict]` | `state_lock` | `update_node`, `process_nodeinfo`, `parse_nodes_from_info`, `apply_node_telemetry`, `ensure_known_nodes`, `normalize_unknown_nodes`, `api_toggle_ignore/favorite`, `api_nodes_import`, `meshsrv/schedule_engine.py` (via DI) |
| `chats` | `dict[chat_id, dict]` | `state_lock` | `ensure_chat`, `load_chats`, `update_chat_last_message`, `reset_unread`, `_handle_listener_line` (creates channel chats inline), `api_clear_chat`/`api_delete_chat`/`api_delete_all_dm`/`api_restore_deleted_dm` |
| `settings` | `dict` | `state_lock` | `load_settings`, `api/api_settings.py`'s `register_settings_routes` closure (DI — mutates the same dict object by reference) |
| `sensor_data` | `dict` | *not consistently* — `save_sensors`/`load_sensors_data` don't take `state_lock`; `apply_telemetry_values` does | `apply_telemetry_values`, `load_sensors_data` |
| `base_status` | `dict` | mixed — `update_base_status_from_info` takes `state_lock`, `apply_telemetry_values` does too, but the dict itself is reassigned (`global base_status`) in three places | `update_base_status_from_info`, `apply_telemetry_values`, `get_telemetry_from_info` |
| `radio_health` | `dict` | `state_lock` (mostly — `radio_event()` and `radio_health_worker()` both take it) | `radio_event`, `radio_health_worker` |
| `seen_ids` | `set` | `state_lock` | `_handle_listener_line`, `cleanup_seen_ids` |
| `seen_recent_texts` | `dict` | `state_lock` | `is_duplicate_text`, `cleanup_seen_ids` |
| `_nodeinfo_buffer` / `_collecting_nodeinfo` | `list` / `bool` | **not locked** — mutated directly inside `_handle_listener_line`, which only ever runs on the single listener thread, so this is safe by construction (single-writer) but fragile if that invariant ever changes | `_handle_listener_line` only |
| `telemetry_pending_values` / `telemetry_pending_time` | `dict` / `float` | `telemetry_buffer_lock` (a **different** lock from `state_lock`) | `queue_telemetry_values`, `telemetry_buffer_worker` |
| `RADIO_IDENTITY_RESULT` | `dict` | **not locked** — reassigned via `global` in `verify_radio_identity`, `_save_detected_radio_runtime`; read unlocked from many routes and `is_radio_available()`/background threads | `verify_radio_identity`, `_save_detected_radio_runtime` |
| `INSTANCE_IDENTITY` | `dict` | **not locked** — reassigned via `global` in 5 places (`verify_radio_identity`, `_save_detected_radio_runtime`, `api_accept_detected_radio`, `api_activate_radio_profile`, the profile-bootstrap block) | see above |
| `camera_manager_state` | `dict` (mutable container, `{"manager": ...}`) | none — deliberately a shared-by-reference container so `api_camera.py` and `api_camera_manager.py` see the same instance (see the code comment at line 369) | `start_runtime()`, `api/api_camera.py`'s lazy `_ensure_manager()` |
| `listener_recovery_state` | `dict` | **referenced but never defined in the range read** — declared elsewhere near line ~3997 (constants block); confirm exact init site before relying on this row | `process_listener_autorecovery` |
| `_runtime_started` / `_runtime_lock_handle` | `bool` / file handle | n/a (process-local, set once) | `start_runtime`, `_acquire_runtime_lock` |
| `radio_connection_manager` | object, `None` until line 3469 | n/a | assigned once |
| `ACTIVE_PROFILE_ID`, `PROFILE_DATA_DIR`, `PROFILE_CONTEXT`, `HISTORY_FILE`, `NODES_FILE`, `SENSORS_FILE`, `CHATS_FILE`, `DELETED_DM_FILE`, `WAYPOINTS_DB_FILE`, `NODE_DEBUG_LOG` | `str`/`dict` | n/a — resolved once at import time from `ProfileManager`, then effectively constant for the process lifetime (a profile switch restarts the whole process rather than reassigning these) | import-time only |

**Locking hazard already flagged in-repo** (not a new finding, confirming CLAUDE.md's own "Known trade-offs" section): `RADIO_IDENTITY_RESULT` and `INSTANCE_IDENTITY` are read/written without `state_lock` in several places. This is pre-existing, accepted behavior — noted here only because any extraction of radio-identity logic out of `server.py` needs to either preserve that same (lock-free) discipline or deliberately fix it as a named, separate change, not silently.

---

## 3. Function/class inventory, by section

Legend for **Deps**: `SL`=`state_lock`, `RL`=`radio_lock`, `TBL`=`telemetry_buffer_lock`, `G`=declares/mutates a `global`, `Sub`=`subprocess`, `FS`=file I/O, `SQL`=sqlite3, `Th`=starts a thread, `Flask`=Flask-request-scoped (uses `request`/`jsonify`), `HW`=touches radio/serial/camera hardware indirectly (via `meshtastic_transport`/`transport_router`/`camera`).

### 3.1 Bootstrap & config resolution — lines 1–369

| Lines | Name | Responsibility | Deps | Test coverage | Candidate module |
|---|---|---|---|---|---|
| 151–194 | `resolve_app_version()` | git-describe-based version string | Sub, FS | none found | stays in `server.py` (or a tiny `meshsrv/app_version.py`) — trivial, low risk |
| 389–456 | `verify_radio_identity()` | Probes the physical radio once, persists result into `INSTANCE_IDENTITY`/`RADIO_IDENTITY_RESULT`, logs + pushes a notification on `MISMATCH` | G, FS (via `instance_manager.save`), HW | **`tests/test_verify_radio_identity_logging.py`** (direct, via `server_module`) | `meshsrv/` — candidate to fold into `radio_identity.py` itself (it's currently a thin server.py wrapper around `detect_radio_identity`/`compare_radio_identity`, both already in `meshsrv/radio_identity.py`) |
| 472–490 | `_load_or_create_secret_key()` | Flask session secret persistence | FS | none found | `storage/` (generic pattern, same shape as `installation_identity.py`'s key generation) |
| 521–536 | `handle_errors` | Shared `@wraps` error-handling decorator used by *every* route module via DI | none | indirectly exercised by every route test | shared utility — candidate `utils/helpers.py` or a new `api/error_handling.py`; currently duplicated-by-reference into every `register_*_routes` call, a clean single-purpose extraction |
| 587–703 | Thirteen `_epaper_get_*`/`_epaper_build_*` closures | Adapter functions bridging `server.py` globals to `modules/display/service.py`'s callback-based API | SL (some), HW (reads `sensor_data`/`radio_health`/`nodes`) | **`tests/test_epaper_*.py`** cover `modules/display/` itself, not these specific closures — no direct test of the closures found | must stay in `server.py` (or a `server`-owned wiring module) as long as e-paper needs read access to `nodes`/`state_lock`/`settings`/`sensor_data` — a clean DI boundary already, just currently inline |
| 730–732 | `static_files()` | `/static/<path>` route | Flask | none found | trivial |
| 734–777 | `safe_read_json`, `safe_write_json`, `atomic_write_json` | JSON persistence primitives | FS | none found directly (exercised indirectly by everything that persists) | **duplicate of `storage/json_store.py`'s `safe_read_json`/`safe_write_json`** — same tmp-file+fsync+os.replace pattern, not the same function object. This is a concrete, low-risk first extraction: replace server.py's copy with an import from `storage/json_store.py` |
| 779–796 | `extract_json_block` | Brace-matching substring extractor used by the `--info` CLI-output parsers | none | none found directly | listener/CLI-output parsing group (§3.3) |

### 3.2 Identity/profile bootstrap — lines 269–369 (see §1, table rows 8–9) — no separate functions, straight-line module code.

### 3.3 Persistence primitives & chat/node/settings state — lines 1127–1512

| Lines | Name | Responsibility | Deps | Test coverage | Candidate module |
|---|---|---|---|---|---|
| 1130–1213 | `now`, `timestamp_iso`, `voltage_to_percent`, `node_num_to_id`, `normalize_node_id`, `normalize_node_id_with_aliases`, `is_valid_node_id`, `is_valid_chat_id`, `sanitize_text`, `friendly_unknown_node_name`, `get_node_name`, `get_node_info` | Pure(ish) string/ID/formatting helpers — no I/O, read `nodes`/`KNOWN_NODES` | none (a couple read `nodes` unlocked) | **`is_valid_node_id`**: `tests/test_node_id_validation.py`. **`sanitize_text`**: `tests/test_sanitize_text.py`. **`normalize_node_id`**: referenced in `tests/test_message_queue.py`. Rest: none found | prime extraction candidates — zero I/O, easily unit-testable in isolation; a `meshsrv/node_identity.py` or `utils/mesh_ids.py` |
| 1215–1263 | `save_messages`, `load_messages` | Messages persistence + one-time ID/owner backfill migration on load | SL, FS | none direct | pairs with `HISTORY_FILE`/profile storage — stays coupled to `messages` global until that global itself moves |
| 1265–1512 | `save_chats`, `load_chats`, `save_nodes`, `load_nodes`, `log_node_event`, `save_sensors`, `load_sensors_data`, `default_settings`, `save_settings`, `load_settings`, `ensure_chat`, `update_chat_last_message`, `reset_unread` | Same pattern for chats/nodes/sensors/settings; `log_node_event` writes a free-text audit log (`NODE_DEBUG_LOG`) for every node mutation | SL (most), FS | `load_settings`/`default_settings` shape overlaps `api/api_settings.py`'s `normalize_settings()`/`DEFAULT_SETTINGS` (**two independent defaults implementations** — worth flagging as a latent inconsistency risk, not just a decomposition concern) | `nodes`/`chats`/`settings` are each a natural "repository" module once the owning dict itself is extracted |

### 3.4 Listener line-parsing (regex/CLI-output parsers) — lines 1513–1893, 2564–2871

This is the largest cohesive, low-coupling group in the file: ~45 functions, all pure functions of `(line: str) -> parsed value`, all stateless except for reading a couple of module constants (`LOCAL_NODE_ID`). None of them touch `state_lock`, Flask, or hardware directly.

| Lines | Names | Responsibility | Test coverage |
|---|---|---|---|
| 1514–1596 | `_float_or_none`, `_regex_number`, `_telemetry_sender_node_id`, `_telemetry_from_local_node`, `_decode_waypoint_text`, `_waypoint_string`, `_waypoint_number` | Generic regex-number/string extraction helpers shared by the telemetry and waypoint parsers | none found directly; `parse_telemetry_from_listen_line` is referenced in `tests/test_listener_paused_recovery.py`/`test_radio_session_timeout.py` fixtures |
| 1597–1683 | `parse_waypoint_from_listen_line`, `process_waypoint_line` | Parses `WAYPOINT_APP` listener lines, upserts into `waypoint_store` (SQLite) | none found |
| 1684–1797 | `_parse_power_channels_from_line`, `parse_telemetry_from_listen_line` | Parses `TELEMETRY_APP`/power/environment/device metrics from a raw line | referenced by `server_module` fixture tests (see above) |
| 1893–2065 | `_normalize_nodeinfo_position`, `process_received_nodeinfo_line` | Parses the CLI's `ast.literal_eval`-able "Received nodeinfo:" dict form (a **different** code path from the `NODEINFO_APP` buffered-line path below) | none found — **notable gap**: this uses `ast.literal_eval` on untrusted-ish CLI output; no dedicated test |
| 2564–2871 | `extract_node_id`, `extract_nodeinfo_user_id`, `extract_sender`, `infer_node_id_from_sender`, `extract_field`, `extract_packet_id`, `extract_channel_index`, `channel_chat_id`, `extract_optional_channel_index`, `extract_reply_id`, `extract_request_id`, `extract_routing_error_reason`, `extract_text_message`, `extract_rssi`, `extract_snr`, `extract_hop_start`, `extract_relay_node` | Regex extraction of individual fields (sender, packet id, channel, reply id, RSSI/SNR, text) from a raw `--listen` line | none found directly — CLAUDE.md's "CLI-output parsing" test-suite claim appears to cover `meshtastic_transport.py`/adapter-side CLI parsing, not this specific family in server.py |

**Candidate module:** this whole group (~500 lines, ~45 functions) is the cleanest, lowest-risk extraction in the entire file — a `meshsrv/listener_line_parser.py` (or under a new `meshsrv/listen/` package) taking `LOCAL_NODE_ID`/`KNOWN_NODES` as parameters instead of module globals. It has almost no coupling to Flask, locks, or hardware. The one thing blocking a mechanical move is that several of these functions call `get_node_name()`/read `nodes` (e.g. `extract_sender`, `infer_node_id_from_sender`) — those specific ones need `nodes`/`get_node_name` passed in, same DI-by-parameter-list pattern `api/*.py` already uses.

### 3.5 Telemetry application & export — lines 2066–2417

| Lines | Name | Responsibility | Deps | Test coverage |
|---|---|---|---|---|
| 2066–2137 | `apply_telemetry_values` | Merges parsed values into `telemetry.telemetry_current`, `sensor_data`, `base_status`; calls `telemetry.add_telemetry_record` | G, SL (called-under, not itself locking — callers hold `state_lock`), FS (via `save_sensors`) | none direct |
| 2140–2181 | `queue_telemetry_values`, `telemetry_buffer_worker` | Debounces rapid telemetry updates (1.5s) before calling `apply_telemetry_values` | G, TBL, **Th** (started in `start_runtime`) | none direct |
| 2183–2199 | `process_telemetry_line` | Glue: parse → `apply_node_telemetry` (per-node) → queue local-node values | SL, HW | none direct |
| 2201–2270 | `get_telemetry_from_info` | Same telemetry extraction but from a full `--info` CLI dump instead of a listener line | G, SL, HW, Sub (indirectly via `meshtastic_transport.get_info`) | none direct |
| 2272–2417 | `get_telemetry_export_records`, `records_to_csv` | CSV/JSON export formatting for `/api/export/telemetry` | FS (reads `telemetry.TELEMETRY_FILE` directly, bypassing `telemetry.py`'s own accessor) | none direct |

**Candidate module:** `telemetry/telemetry.py` already exists and owns the underlying storage (`telemetry_current`, `telemetry_history`, `add_telemetry_record`) — these functions are the "apply a parsed sample" and "export" layers on top of it and belong there once `nodes`/`sensor_data`/`base_status` access is parameterized. `get_telemetry_export_records` reading `telemetry.TELEMETRY_FILE` directly (rather than through a `telemetry.py` accessor) is a minor existing layering crack worth noting for whoever does this extraction.

### 3.6 Node list building — lines 2418–2563, 3286–3424

| Lines | Name | Responsibility | Deps | Test coverage |
|---|---|---|---|---|
| 2418–2499 | `parse_nodes_from_info` | Parses `"Nodes in mesh: {...}"` from `--info` output into `nodes` | G, SL, Sub (indirect) | referenced by `server_module` fixture (`tests/test_...` files listed in §5) |
| 2500–2563 | `ensure_known_nodes`, `normalize_unknown_nodes` | Seeds/repairs `nodes` from `config.py`'s `KNOWN_NODES`; called once at startup | SL, FS | none direct |
| 3286–3379 | `node_status_icon`, `age_text`, `signal_quality`, `get_nodes_list` | Presentation-layer formatting for the node list (emoji status, human age string, RSSI bucket) + the full `/api/nodes`-style aggregate builder | SL | none direct |
| 3381–3424 | `get_chats_list`, `get_chat_messages` | Same for chats | SL | none direct |

**Candidate module:** `get_nodes_list`/`get_chats_list`/`get_chat_messages` are pure read-side view builders already passed by reference into `register_chat_routes(...)` — they're only in `server.py` because `nodes`/`chats`/`messages` are. A natural `storage/node_repository.py` / `storage/chat_repository.py` pairing (mirroring `storage/profile_manager.py`'s style) would hold both the dict and these accessors together, which is the real prerequisite for moving them, not the functions themselves.

### 3.7 Node/message mutation core — lines 2872–3286

| Lines | Name | Responsibility | Deps | Test coverage |
|---|---|---|---|---|
| 2872–2973 | `update_node` | Updates `nodes[node_id]` from a text-message listener line (never renames — only NODEINFO renames) | SL, FS (`log_node_event`) | none direct |
| 2975–3076 | `process_nodeinfo` | Updates `nodes[node_id]` from a buffered `NODEINFO_APP` block (**does** rename) | SL, FS | none direct |
| 3078–3178 | `add_message` | **The** canonical message-creation function — resolves chat routing (dm/channel), builds the message dict, appends, trims to `MAX_HISTORY_MESSAGES`, updates unread count, persists | SL, FS | **`tests/test_message_queue.py`** (direct, via `server_module`) |
| 3194–3224 | `update_message_status` | Marks a `pending` message `sent`/`delivered`/`failed`, stamps ACK deadline | SL, FS | **`tests/test_...` via `server_module`** (referenced) |
| 3226–3253 | `reconcile_interrupted_sends` | Startup-only: flips any leftover `pending` message to `failed` (no in-memory send queue survives a restart) | SL, FS | referenced by `server_module` fixture tests |
| 2737–2799 | `process_routing_ack_line`, `ack_timeout_worker` | Resolves/times-out delivery ACKs for DM sends (60s timeout) | SL, FS, **Th** | none direct |
| 2802–2839 | `find_message_by_packet_id`, `build_reply_reference` | Lookup helpers for reply-to threading | SL (caller-held) | none direct |
| 3255–3284 | `is_duplicate_text` | 15-second sliding-window dedup on (sender, node, text) | SL | none direct |

**Candidate module:** this is the most Flask/global-coupled group — `add_message` alone is called from `_handle_listener_line`, `start_runtime`, `api/api_chat.py` (via DI), `meshsrv/schedule_engine.py` (via DI), and waypoint processing. It's the highest-value but highest-risk extraction target (a "message service" module) precisely because of that fan-in — any PR touching it needs every one of those call sites in scope, not just server.py's own definition.

### 3.8 Radio session/command claiming — lines 3425–3620

| Lines | Name | Responsibility | Deps | Test coverage |
|---|---|---|---|---|
| 3425–3441 | `stop_listener` | Thin delegate to `listener_supervisor.stop_listener_process()` | HW | none direct |
| 3443–3449 | `wait_serial_release` | Delegate to `listener_supervisor.wait_serial_release()` | HW | none direct |
| 3452–3466 | `prepare_radio_command` | Pause listener + stop + wait-for-release, gated by `radio_connection_manager.commands_allowed()` | HW | none direct |
| 3478–3480 | `is_radio_available` | Combines identity status + `radio_connection_manager.commands_allowed()` | none | none direct |
| 3483–3484 | `RadioBusyError` | Exception type shared across `server.py`/`api/api_node_tools.py`/`api/api_waypoints.py` | n/a | n/a |
| 3487–3572 | `radio_session` (contextmanager) | THE exclusive-access primitive for CLI-based Node Tools operations — acquires `radio_lock` (bounded), then `prepare_radio_command`, yields, then cooldown + `pause_listen.clear()` in `finally` | RL, HW | **`tests/test_radio_session_timeout.py`** (direct, via `server_module`) |
| 3575–3618 | `_attempt_node_time_sync` | Best-effort node clock sync after a fresh listener connect, throttled | HW, **runs in its own daemon thread** (spawned from `radio_event`, not `start_runtime`) | none direct |

**This is exactly the "`radio_lock` bounded, but `prepare_radio_command()`'s own phase is not" trade-off CLAUDE.md already documents** — confirmed by reading; not a new finding.

**Candidate module:** `meshsrv/serial_port_supervisor.py` already owns the actual listener process; `radio_session`/`prepare_radio_command`/`is_radio_available`/`RadioBusyError` are Core-level orchestration one layer up (they know about `radio_connection_manager`, `pause_listen`, `radio_lock` — none of which are `SerialPortSupervisor`'s). A `meshsrv/radio_session.py` composing `listener_supervisor` + `radio_connection_manager` is the natural target, but every one of `api/api_node_tools.py`, `api/api_waypoints.py` (via `radio_session` passed by reference), and server.py's own `api_rescan_nodes` would need to import from the new location instead of getting it via DI — a signature-visible change, not a pure move.

### 3.9 Listener thread & line dispatch — lines 3656–3959

| Lines | Name | Responsibility | Deps | Test coverage |
|---|---|---|---|---|
| 3656–3658 | `read_sensors_from_meshtastic` | Trivial accessor, appears unused beyond its own definition — **candidate dead code, verify no caller before deleting** | none | none |
| 3659–3677 | `cleanup_seen_ids` | Background thread: trims `seen_ids`/`seen_recent_texts` every 5 min | SL, G, **Th** | none direct |
| 3678–3704 | `listen_meshtastic` | Thin wrapper: identity gate, then delegates to `listener_supervisor.run_listener()` | HW, **Th** (this IS the thread target) | none direct |
| 3707–3939 | `_handle_listener_line` | **The** per-line dispatcher — classifies each raw `--listen` stdout line by substring match and routes to the matching parser (nodeinfo / waypoint / routing-ack / telemetry / text message), then for text messages: dedup, node update, chat routing, `add_message`, and MCA dispatch (`mca_runtime.handle_incoming_meshtastic_text`) for `MCA1:`-prefixed DMs | G, SL, HW, FS | none direct found — **this is the single most load-bearing, untested function in the file**: it's the entire inbound-message pipeline, called once per line from `listener_supervisor.run_listener()`'s callback |
| 3941–3959 | `telemetry_worker` | Background thread, logs telemetry staleness every 60s — **listen-only, does no active polling** (comment confirms Stage B — active fetch — isn't implemented) | **Th** | none direct |

### 3.10 Listener auto-recovery & radio health — lines 3960–4459

| Lines | Name | Responsibility | Deps | Test coverage |
|---|---|---|---|---|
| 3997–4051 | `resolve_paused_recovery_status` | Escalates a stuck `PAUSED` state past `LISTENER_PAUSED_ESCALATE_THRESHOLD_S` (180s) into a synthetic `LISTENER_DOWN` for recovery purposes | SL (caller-held) | **`tests/test_listener_paused_recovery.py`** (direct, via `server_module`) |
| 4052–4293 | `process_listener_autorecovery` | State machine: enable/disable, attempt-window tracking (3 attempts / 30 min), restart-pending confirmation, safety limit — restarts the listener via `stop_listener()`+`pause_listen.clear()`+`radio_event("restart")` | SL, HW | **`tests/test_listener_paused_recovery.py`** (direct — confirmed via `process_listener_autorecovery` in grep hits) |
| 4295–4457 | `radio_health_worker` | Background thread (30s poll): classifies radio status (`RELEASED`/`PAUSED`/`LISTENER_DOWN`/`STARTING`/`OK`/`IDLE`/`NO_PACKETS`) from packet/telemetry/send ages, updates `radio_health`, calls `resolve_paused_recovery_status` + `process_listener_autorecovery` | SL, **Th** | indirectly via the two functions above; no direct test of the worker loop itself |

**Candidate module:** `meshsrv/radio_health.py` — this entire group (§3.8 partially, §3.10 fully) is already conceptually a self-contained state machine reading `radio_health`/`pause_listen`/`settings.listener_autorecovery` and calling back into `stop_listener`/`radio_event`. It has real test coverage already (better than most of the file), which lowers the risk of extracting it.

### 3.11 Route registration calls (DI wiring) — lines 541–552, 705–727, 4459–4763

Already covered structurally in §1; the exact parameter lists for each `register_*_routes()` call are the ground truth for "what does `api/X.py` depend on from `server.py`" — see `dependency-map.md` for the full parameter-list transcription per module (produced by a parallel pass over `api/`).

### 3.12 Native `@app.route` handlers defined directly in `server.py` — lines 4765–5891

Unlike `api/*.py`, these ~30 routes were never split out. Grouped by concern:

| Concern | Routes | Lines | Test coverage |
|---|---|---|---|
| Index/version/instance | `/`, `/api/sensors`, `/api/instance` | 4765–4814 | none found for these three specifically |
| **Radio profile / node-manager dashboard** | `/api/node-manager/dashboard`, `/api/devices/dashboard`, `/api/node-manager/radio/detect`, `/api/node-manager/radio/accept`, `/api/node-manager/profiles/<id>/activate`, `/api/devices` | 4816–5384 | **none found** — no direct test of any of these six routes, despite them being the most operationally risky in the file (radio detect/accept/activate each do a live radio probe + `instance_manager.save()` + spawn a `_restart_meshcenter_after_profile_switch()` thread that shells out to `sudo systemctl restart meshcenter.service`) |
| `/api/base_status`, `/api/node_status`, `/api/toggle_ignore`, `/api/toggle_favorite`, `/api/cleanup_nodes` | 5387–5441 | none found |
| Radio connection lifecycle | `/api/radio_connection/status`, `/release`, `/reconnect`, `/api/restart_listener`, `/api/rescan_nodes` | 5443–5561 | none found |
| Chat maintenance | `/api/clear_chat`, `/api/delete_chat` | 5563–5595 | none found |
| Telemetry API | `/api/telemetry`, `/api/telemetry/history`, `/api/export/telemetry`, `/api/telemetry/config` | 5597–5741 | none found |
| Node management | `/api/nodes_management`, `/api/nodes_export`, `/api/nodes_import` | 5743–5810 | none found |
| DM bulk ops | `/api/delete_all_dm`, `/api/restore_deleted_dm` | 5813–5857 | none found |
| `/api/radio_health` | 5859–5891 | none found |

**This is the single biggest, most surprising gap this audit turned up.** CLAUDE.md's own testing section says the suite covers "CLI-output parsing, `normalize_settings()`, node ID validation, `sanitize_text()`, the auth/message-queue/radio-identity/profile-manager logic, and the gunicorn runtime lock" — all true, and all confirmed above — but the ~30 routes still defined directly in `server.py` (as opposed to already-modularized `api/*.py` routes) have **no direct pytest coverage at all**, including the radio-profile-switch endpoints that restart the whole service via `sudo systemctl`. Any D1+ PR that touches these routes should budget for adding characterization tests first, not assume the existing suite is a safety net for them — it isn't.

### 3.13 `_restart_meshcenter_after_profile_switch`, `_save_detected_radio_runtime` — lines 4946–4998

Covered in §3.12's table above; called by name here because they're the concrete GPL/Docker-relevant hard dependency: `subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "meshcenter.service"], ...)` (line 4950–4956) is a **hard, unconditional systemd dependency** with no fallback path — see `docker-blockers.md`.

### 3.14 Storage-summary helpers — lines 4634–4704

`_format_bytes`, `_json_item_count`, `_waypoint_count` (SQL — opens `waypoints.db` directly with its own `sqlite3.connect`, bypassing `storage/waypoint_store.py`'s own `WaypointStore` class), `_profile_storage_summary` (`os.walk` over the profile directory) — all private helpers feeding `api_devices_dashboard`. No test coverage found. Minor layering crack: `_waypoint_count` re-implements a raw SQLite query instead of asking `waypoint_store` for a count.

### 3.15 Weather location resolution — lines 4490–4591

`_coordinate`, `resolve_weather_location` (resolves Weather's coordinates from `settings.reference_location`, mode `manual`/`node`/fallback-to-`config.py`), plus construction of the two `_weather_providers` singletons and `weather_manager`. No test coverage found for `resolve_weather_location` specifically (weather provider logic itself is tested — see `weather/` inventory in `dependency-map.md`).

### 3.16 Runtime startup — lines 5893–6229 (`_acquire_runtime_lock`, `start_runtime`)

Already covered in full in §1's framing and reproduced in detail below (§4) since it's the load-bearing sequencing contract for any bootstrap-decomposition work.

**Test coverage:** `_acquire_runtime_lock` → **`tests/test_runtime_lock.py`** (direct). `start_runtime` itself → **`tests/test_server_startup_degraded_serial.py`**, **`tests/test_smoke_import.py`** (both via `server_module`, exercising the "no serial radio" degraded path — see `[[mc_dev_esp32_serial_test_failure]]` memory for why this specific test is environment-sensitive on one dev box).

---

## 4. `start_runtime()` — the full startup sequence (ground truth, lines 5947–6229)

This is what `wsgi.py`'s `from server import app, start_runtime; start_runtime()` and `server.py`'s own `if __name__ == "__main__":` both call, exactly once per process (guarded by `_runtime_started` + the OS-level `flock()` in `_acquire_runtime_lock()`). In order:

1. `_acquire_runtime_lock()` — `flock()` on `data/runtime.lock` (`LOCK_EX | LOCK_NB`); `sys.exit(1)` if already held by another process (second gunicorn worker, etc.) — no-ops with a printed warning on non-POSIX (`fcntl is None`, i.e. Windows dev boxes).
2. `verify_radio_identity()` — one blocking radio probe (see §3.1).
3. `load_messages()`, `reconcile_interrupted_sends()`, `load_nodes()`, `load_sensors_data()`, `load_chats()`, `ensure_known_nodes()`, `normalize_unknown_nodes()` — in that order.
4. `parse_nodes_from_info(startup_info_output)` — **only if `identity_match`** (reuses the identity probe's own `--info` output rather than a second CLI call).
5. `load_settings()`.
6. `weather_manager.set_active(...)` / `.active().set_language(...)` — syncs the weather singletons to whatever was persisted in `settings.json` (they were constructed at import time from `config.py` defaults, before `settings.json` had loaded).
7. `load_cpu_history(CPU_HISTORY_FILE)`.
8. `update_base_status_from_info(startup_info_output)` — only if `identity_match`, best-effort.
9. `telemetry.load_telemetry()`, `camera.load_camera_settings()`.
10. Seeds chats for every `KNOWN_NODES` entry not already present.
11. `get_telemetry_from_info(startup_info_output)` — only if `identity_match`, best-effort.
12. **Camera manager construction** — only if `camera_power_enabled_at_startup` (persisted `camera_power.json` toggle); calls `build_camera_manager()`, which does real device I/O (`Picamera2`/`/dev/videoN` probing) — deliberately skipped entirely otherwise, per the in-code comment, "the camera must not be touched at all without being asked."
13. `mca_runtime.start_attachments_service(DATA_DIR, transport_router, MCA_CONTROL_CHANNEL_INDEX)` — **unconditional**, not gated on `identity_match` (deliberate — see the code comment: previously-queued attachment work must resume even if the current radio identity is unresolved). Must complete before the listener thread starts (step 14) — this ordering is load-bearing per an explicit PR #231 review comment in the code, not incidental.
14. **Only if `identity_match`:** starts six daemon threads — `listen_meshtastic`, `cleanup_seen_ids`, `telemetry_worker`, `telemetry_buffer_worker`, `radio_health_worker`, `ack_timeout_worker`. If identity does *not* match, `pause_listen.set()` instead and none of these start.
15. `cpu_history_worker` thread — **unconditional**, always starts regardless of identity match.
16. `update_service.check_worker` thread — unconditional.
17. `start_time_service()`, `start_installation_time_assignment(instance_manager)`, `start_schedule_engine(...)` — three more background workers/threads owned by their respective `meshsrv/` modules, unconditional.
18. **If `EPAPER_ENABLED`:** `display_manager.start()` (only if `epaper_config.get("enabled", True)`) and an `epaper_worker` thread (always started when `EPAPER_ENABLED`, even if the runtime toggle is off, so flipping it back on later doesn't need a restart).
19. Prints the startup banner.

**Total background threads possibly running after `start_runtime()`:** up to 12 (`listen_meshtastic`, `cleanup_seen_ids`, `telemetry_worker`, `telemetry_buffer_worker`, `radio_health_worker`, `ack_timeout_worker`, `cpu_history_worker`, `update_service.check_worker`, `time_service`'s own thread, `installation_time_assignment`'s own thread, `schedule_engine`'s own thread, `epaper_worker`) plus the `mca_runtime` attachments service's own internal worker(s) plus, transiently, the `node-time-sync` thread spawned from `radio_event()` and the `_restart_meshcenter_after_profile_switch` thread spawned from the two radio-profile-switch routes. None of this is configurable per-thread; a future "run listener in one container, UI in another" split has to account for all twelve, not just the listener.

**Shutdown path:** there isn't an explicit one. No `atexit`/signal handler was found registered anywhere in `server.py` for these threads — they're all `daemon=True`, so process exit just kills them. `_runtime_lock_handle` is released implicitly on process exit (fd closes). This matters for Docker: `docker stop`'s default SIGTERM has nothing graceful to catch in Core today; the listener subprocess itself (a separate OS process, not a thread) would be orphaned unless `PR_SET_PDEATHSIG`/`KillMode=control-group` (both already documented in CLAUDE.md for the systemd case) has an equivalent in whatever container runtime is chosen — flagged for `docker-blockers.md`.

---

## 5. Test coverage summary (server.py specifically)

Only **2 test files** import `server.py` directly (`from server import ...` / `import server`): `tests/conftest.py` (defines the `server_module` fixture) and `tests/test_server_startup_degraded_serial.py`. A further **13 files** consume `server.py` through that fixture:

`test_api_csrf.py`, `test_api_instance.py`, `test_cli_parsing.py`, `test_instance_manager.py`, `test_listener_paused_recovery.py`, `test_message_queue.py`, `test_node_id_validation.py`, `test_radio_event_listener_stop_classification.py`, `test_radio_session_timeout.py`, `test_runtime_lock.py`, `test_sanitize_text.py`, `test_smoke_import.py`, `test_verify_radio_identity_logging.py`.

Names/attributes directly referenced from `server.py` across those 15 files (ground truth from grep, not inference): `INSTANCE_IDENTITY`, `LISTENER_PAUSED_ESCALATE_THRESHOLD_S`, `LISTENER_PAUSED_WARNING_THRESHOLD_S`, `LISTENER_RECOVERY_MAX_ATTEMPTS`, `LISTENER_RECOVERY_RESULT_TIMEOUT`, `LOCAL_NODE_ID`, `MESHTASTIC_PORT`, `PROFILE_DATA_DIR`, `RADIO_IDENTITY_RESULT`, `RUNTIME_LOCK_FILE`, `RadioBusyError`, `SESSION_COOKIE_SECURE`, `_acquire_runtime_lock`, `_runtime_lock_handle`, `_runtime_started`, `add_message`, `api_instance_identity`, `app`, `extract_json_block`, `instance_manager`, `is_valid_chat_id`, `is_valid_node_id`, `listener_recovery_state`, `messages`, `nodes`, `normalize_node_id`, `parse_nodes_from_info`, `parse_telemetry_from_listen_line`, `pause_listen`, `prepare_radio_command`, `process_listener_autorecovery`, `process_received_nodeinfo_line`, `radio_health`, `radio_lock`, `radio_session`, `reconcile_interrupted_sends`, `resolve_paused_recovery_status`, `sanitize_text`, `settings`, `start_runtime`, `state_lock`, `update_message_status`, `verify_radio_identity`.

Everything else in the ~150-function inventory above — including the entire "native `@app.route`" block (§3.12, ~30 routes) and most of the listener-line-parsing group's individual `extract_*` functions (only reachable indirectly through `_handle_listener_line`, itself untested) — has **no direct test coverage found**. This matches CLAUDE.md's own disclaimer ("not full end-to-end coverage") but the audit makes the *specific* gap concrete: it's overwhelmingly the request-handling and listener-dispatch code, not the state-machine/validation logic, that's untested.

---

## 6. Cross-cutting observations feeding the PR sequence

1. **The listener-line-parsing group (§3.4, ~45 functions, ~500 lines) is the highest-value, lowest-risk extraction.** Pure functions, minimal global coupling, currently untested (so extraction is also the natural moment to add the characterization tests that don't exist today).
2. **The radio-health/auto-recovery group (§3.10) is the second-best candidate** — already has real test coverage, already reads/writes a self-contained `radio_health` dict, and its only external calls are `stop_listener()`/`pause_listen`/`radio_event()`, i.e. an already-narrow interface.
3. **`add_message`/`update_node`/`process_nodeinfo` (§3.7) are the highest-risk group** — heavy fan-in (called from listener dispatch, `api/api_chat.py` DI, `meshsrv/schedule_engine.py` DI, waypoint processing) and zero direct tests. Any move here should land *after* the listener-parsing and radio-health extractions establish the DI-by-parameter-list pattern more thoroughly at smaller scale.
4. **The "native `@app.route` in server.py" block (§3.12) is not really a decomposition problem, it's a modularization backlog item** — the same `register_*_routes(app, ...)` DI pattern `api/*.py` already uses would apply directly; the work is mechanical (move + parameterize + wire in) but every single one of these ~30 routes needs a new test written first, since none currently exist.
5. **`safe_read_json`/`safe_write_json` duplicating `storage/json_store.py`** (§3.1) is a trivial, contained fix — replace the definitions with an import. Not a "decomposition" so much as dead-weight removal; worth doing early since it removes a footgun (two independent atomic-write implementations that could drift).
6. **Two independent settings-defaults implementations** (`server.py`'s `default_settings()`/`load_settings()` vs. `api/api_settings.py`'s `DEFAULT_SETTINGS`/`normalize_settings()`) — flagged in §3.3, worth a follow-up issue regardless of the Docker work.
7. **Module import side effects (§1) are the actual D1 bootstrap-decomposition scope**, not `start_runtime()` — `start_runtime()` is already fairly well isolated (that's the whole point of the `wsgi.py` split from PR #69). The real coupling problem for "can Core start in a container with no radio/no `config.py` file on disk" is steps 4, 8, 9 of §1's table, all of which run at `import server` time, before any test or WSGI server gets a chance to configure anything.
