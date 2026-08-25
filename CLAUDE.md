# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MeshCenter is a Flask web control center for a Meshtastic LoRa radio attached to a Raspberry Pi over USB serial or Bluetooth LE. The MIT-licensed Core (`server.py`, `api/`, `meshsrv/`, everything except `adapters/`) never imports the `meshtastic` Python package directly — all real radio I/O goes through a neutral `RadioTransport` interface (`meshsrv/radio_transport.py`, "Backend Protocol v1") implemented by `SerialTransport`/`BLETransport` in `adapters/meshtastic/`, which run in a separate subprocess with its own Python venv specifically to keep the GPLv3 `meshtastic` dependency isolated from Core's own process (see "GPLv3 process isolation" below and `THIRD_PARTY_NOTICES.md`). Core's own listener (`meshtastic --listen`, still a subprocess, still parsed line-by-line — "Stage A" of the isolation work, see below) is the one exception: it shells out to the CLI binary as an arm's-length subprocess, the same license-safe reasoning as any other external tool invocation, never importing the Python package. There is no database — all persistence is local JSON files (one SQLite file for waypoints) under `data/`.

## Running it

There is no build step, package.json, or linter config in this repo — don't invent `npm run` or lint commands that don't exist here. There **is** a pytest test suite (`tests/`, `pytest.ini`) and a GitHub Actions CI workflow (`.github/workflows/ci.yml`) — see "Testing" below.

```bash
python3 -m venv --system-site-packages venv   # --system-site-packages required for Picamera2 on the Pi
source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py                # edit MESHTASTIC_PORT / LOCAL_NODE_ID / LOCAL_NODE_NAME
mkdir -p data

python server.py                               # dev run, http://<host>:5000
# or, in production:
sudo systemctl restart meshcenter.service       # runs gunicorn - see "Deployment" below
```

`config.py` and `weather_secrets.py` are gitignored local files; `server.py` exits at import time if `config.py` is missing or missing required variables (see the `required_vars` check near the top of `server.py`). When changing code that reads config, check `config.example.py` for the authoritative variable list.

### Testing

```bash
pip install -r requirements-dev.txt   # adds pytest on top of requirements.txt - never installed on a production Pi
pytest                                 # run from the repo root; picks up pytest.ini automatically
```

`tests/conftest.py` makes `server.py` importable without a Pi or a radio attached (a synthetic `config.py`, a fake Meshtastic CLI/serial port, a stub for the Pi-only `libcamera` import) - it does **not** start `start_runtime()`'s background threads/radio listener, so the suite is safe to run anywhere, including CI. As of PR #69 this covers CLI-output parsing, `normalize_settings()`, node ID validation, `sanitize_text()`, the auth/message-queue/radio-identity/profile-manager logic, and the gunicorn runtime lock - not full end-to-end coverage, but a real regression net. Run `pytest` locally before opening a PR.

`.github/workflows/ci.yml` runs on every PR and push to `main`: `python -m compileall` (all Python), `bash -n` on every installer/deploy shell script, `node --check` on the non-vendored `static/*.js` files, then `pytest`. It does not run linters (none configured) or touch real hardware.

Beyond that, manual verification against a real or simulated radio (`meshtastic --port <dev> --info`) and exercising the REST endpoints/UI in a browser is still how anything touching the actual radio listener, camera, or e-Paper hardware gets validated - the test suite deliberately doesn't attempt to fake real hardware I/O for those.

## Architecture

### `server.py` is the core, not a thin entrypoint

`server.py` (~5,200 lines) owns the Flask `app`, nearly all shared mutable state (`nodes`, `messages`, `chats`, `settings`, locks, background threads), and most route handlers that haven't yet been split out. Newer feature areas live in `api/*.py`, but they are **not Flask Blueprints** — each is a plain function `register_<area>_routes(app, state_lock, ..., <30+ shared globals/functions>)` called from `server.py`, closing over the objects/functions passed in. When adding a new route module, follow this dependency-injection-by-parameter-list pattern rather than introducing Blueprints, and wire the new `register_*_routes(...)` call into `server.py`.

The project's own stated direction (see README "Modular Architecture") is to keep shrinking `server.py` by moving logic into `api/`, `meshsrv/`, `storage/`, `telemetry/`, `camera/` — prefer extending those modules over growing `server.py` further when the code is genuinely a separate concern.

### The radio link: listener stays in Core (Stage A), everything else crosses a process boundary

`listen_meshtastic()`/`run_listener()` in `server.py`/`adapters/meshtastic/serial_transport.py` still runs `meshtastic --listen` as a subprocess and classifies each stdout line by substring checks (`"NODEINFO_APP"`, `"TELEMETRY_APP"`, `"WAYPOINT_APP"`, `"TEXT_MESSAGE_APP"`, etc.), handing multi-line blocks to parsers like `process_nodeinfo`, `parse_telemetry_from_listen_line`, `parse_waypoint_from_listen_line`. This is fragile by nature (depends on the CLI's human-readable log format) and, as of the process-isolation work, deliberately still lives in Core ("Stage A" — a full move to the adapter process is "Stage B", not yet done). It only ever shells out to the `meshtastic` CLI *binary* (an arm's-length subprocess invocation, not a Python import), so it doesn't break the GPLv3 isolation below.

Everything else — connect/disconnect/reconnect, sending, `get_nodes`/`get_channels`/`get_local_node`/`get_metadata`, `set_device_time`, BLE scan — goes through `meshsrv/radio_transport.py`'s `RadioTransport` ABC ("Backend Protocol v1"), implemented by `SerialTransport`/`BLETransport` in `adapters/meshtastic/` (the only modules anywhere in the repo that import the `meshtastic` Python package — lazily, inside the method that needs it). `meshsrv/transport_router.py`'s `TransportRouter` is the single, stable object every DI consumer (`api/api_chat.py`, `api/api_waypoints.py`, `meshsrv/schedule_engine.py`) actually calls — it wraps whichever concrete transport is currently active and handles switching between them (`api/api_meshtastic.py`), so callers never need to know or care which transport is live.

### GPLv3 process isolation — why, and how it actually works

The `meshtastic` Python package is GPLv3-licensed; MIT-licensed Core code must never import it directly (see `THIRD_PARTY_NOTICES.md` and `adapters/meshtastic/LICENSE`). `SerialTransport`/`BLETransport` run in a **separate OS process**, with their own **separate Python venv** (`adapters/meshtastic/venv`, created by `install.sh`'s `step_adapter_venv()`/`meshcenter-firstboot.sh`, installing `adapters/meshtastic/requirements.txt`) — Core's own venv/`requirements.txt` has no `meshtastic` dependency at all. Process isolation (not merely code-directory separation) is the architecture chosen so GPLv3 code never links into Core's MIT-licensed process — see README's licensing section for the reasoning.

- `meshsrv/adapter_ipc_client.py`'s `AdapterSupervisor` spawns/supervises the adapter subprocess (`python -m adapters.meshtastic.ipc_server`, one persistent process, multiplexing both serial and BLE per-request via a `transport_type` field) and talks to it over newline-delimited JSON on stdin/stdout (`meshsrv/ipc_protocol.py`, `docs/BACKEND_API.md`'s wire shape). `AdapterIPCTransport` is the Core-side `RadioTransport` implementation that does the actual request/response round-trip — `server.py` constructs one instance per transport type (`serial_ipc_transport`, `ble_ipc_transport`) and wires them into `TransportRouter`.
- **Three-tier timeout contract** (see `meshsrv/adapter_ipc_client.py`'s module docstring for the full detail): (a) the adapter's own internal `TimeoutEnforced` watchdog (`adapters/meshtastic/_timeout_support.py`) uses the caller's timeout minus a fixed margin, so it can report its own graceful timeout before (b) `AdapterSupervisor.call()`'s own deadline fires and SIGKILLs the adapter subprocess, which is itself bounded by (c) `TransportRouter`'s caller-declared budget, split dynamically (`time.monotonic()`-measured, not a fixed proportion) between lock-wait and the delegated call. A killed adapter respawns automatically on the next call — no data loss, no stuck state, just a `TransportError(TIMEOUT)` to the caller.
- **`claim_for_external_command()`** (`SerialTransport`, called on Core's own listener-management-only instance before any serial-type IPC call) pauses Core's `--listen` subprocess and confirms the port is genuinely free before letting the adapter open its own `SerialInterface` — a live-caught gap, not part of the original design, closed after Core's listener and the adapter's own operations raced for `/dev/ttyACM0` with nothing coordinating them.
- Orphaned-adapter protection if Core itself dies: `deploy/meshcenter.service`'s `KillMode=control-group` (systemd's default, live-confirmed via `systemctl show`) plus `PR_SET_PDEATHSIG` (`preexec_fn`, Linux-only) for the `python server.py` direct-run case outside systemd.
- BLE cleanup on a killed adapter: `bluetoothctl disconnect <address>` from Core's own process (arm's-length CLI call, not a library import) when the kill happened mid-BLE-operation — a BLE GATT session can outlive the process that opened it.

### Known, accepted trade-offs (not bugs — explicitly documented, some deferred on purpose)

- **BLE receive-blindness**: while Bluetooth is the active transport, MeshCenter can send but cannot receive — no incoming messages/telemetry/nodeinfo at all, not degraded, fully absent. Core's listener only ever manages the *serial* `--listen` subprocess; there is no BLE equivalent yet ("Stage B" work). Surfaced explicitly in the UI (`meshtastic_ble_receive_warning` in the i18n catalogs) and in the README's Bluetooth section — don't let this regress silently if BLE receive is ever implemented.
- **No hot-reconnect after a physical serial cable swap**: if the USB cable is unplugged and replugged (or the radio power-cycled) while MeshCenter is running, recovery needs a full service restart, not just a Settings click — the listener's own subprocess doesn't currently detect and recover from an underlying device disappearing/reappearing.
- **`radio_lock` bounded, but `prepare_radio_command()`'s own phase is not** (`server.py`'s `radio_session()`): the lock-acquire step is bounded and raises `RadioBusyError`/HTTP 503 on contention, but `prepare_radio_command()` (pause+stop+wait-for-port-release) still runs *before* the bounded acquire, unlike `_claim_radio()`'s deliberate whole-span lock hold — two concurrent callers can still race during that phase. Flagged, not fixed — a separate, narrower follow-up.

Background threads (`listen_meshtastic`, `telemetry_worker`, `radio_health_worker`, `cpu_history_worker`, the chat send-queue worker in `api/api_chat.py`) all run for the lifetime of the process. `pause_listen.is_set()` must be respected by anything that wants exclusive serial access, and `state_lock` guards the in-memory JSON-backed state (`nodes`, `messages`, `chats`) during concurrent reads/writes.

### Multi-radio profiles

MeshCenter supports switching between physical Meshtastic radios and keeps each one's data isolated:

- `meshsrv/instance_manager.py` (`instance.json`) tracks this MeshCenter installation's identity and which profile is currently active.
- `meshsrv/radio_identity.py` detects the connected radio and compares it against the configured/accepted one (`MATCH` / `MISMATCH` / `NOT_FOUND`) — the listener refuses to start (`RADIO_IDENTITY_RESULT.status != "MATCH"`) until identity is verified, to avoid silently mixing one radio's data with another's.
- `storage/profile_manager.py` (`ProfileManager`) owns `data/profiles/<8-hex-node-id>/`, one directory per radio, each with its own `messages.json`, `nodes.json`, `sensors.json`, `chats.json`, `deleted_dm.json`, `telemetry_history.json`, `waypoints.db`, `node_icons/`. It also migrates pre-profile legacy flat files in `data/` into the first profile on upgrade.
- Switching profiles (`/api/node-manager/radio/accept`, `/api/node-manager/profiles/<id>/activate` in `server.py`) ends with `_restart_meshcenter_after_profile_switch()` — the process restarts itself so every module rebinds its file paths to the newly active profile rather than trying to hot-swap in-memory state.

Only `data/instance.json`, `data/settings.json`, and `data/screenshots/` are instance-scoped (not per-radio); everything else profile-scoped lives under `data/profiles/<id>/`.

### Storage conventions

- `storage/json_store.py` (`safe_read_json` / `safe_write_json`) is the standard atomic JSON read/write (write to `.tmp`, `fsync`, `os.replace`) — use it for any new JSON-backed state instead of open()/json.dump directly.
- Waypoints are the one exception to "everything is JSON": `storage/waypoint_store.py` uses SQLite (`waypoints.db`), per profile.
- `storage/device_manager.py` tracks per-profile auxiliary device/sensor metadata separate from the node list.

### Subsystem modules

- `camera/camera_manager.py` is the live dispatch point for `/video_feed` and `api/api_camera.py` (cutover completed 2026-08-17) — it holds the active `CameraDriver` (`camera/csi_driver.py` for Picamera2-based CSI cameras, `camera/usb_driver.py` for USB/UVC via a persistent reader thread) and handles discovery/dedup/switching across multiple simultaneously-connected cameras. `camera/camera.py` itself still provides the CSI capture internals and the driver-agnostic screenshot helpers (`get_screenshot_day_dir`/`make_screenshot_filename`/`cleanup_old_screenshots`) shared by both drivers, but is no longer called directly for the live stream. Needs the venv built with `--system-site-packages` to see system Picamera2/libcamera bindings.
- `telemetry/telemetry.py` — telemetry history storage/aggregation (`configure_storage()` is pointed at the active profile's `telemetry_history.json` at startup and after a profile switch).
- `weather/` — pluggable weather backend. `weather/providers/base.py` defines the `WeatherProvider` interface (each provider normalizes its own condition codes into a shared `CONDITION_KEYS` vocabulary so `static/weather.js` never has to know which provider is active); `weather/providers/openweather.py` and `weather/providers/weatherapi.py` implement it for OpenWeather and WeatherAPI; `weather/weather_manager.py` is the registry that tracks which provider is active (`settings.weather.provider`, mirrors `settings.maps.provider`) and switches on save. Both providers share one cache-per-request-window shape (`WEATHER_CACHE_SECONDS`); API keys come from gitignored `weather_secrets.py` (one variable per provider, e.g. `OPENWEATHER_API_KEY` / `WEATHERAPI_API_KEY`), never from the browser.
- `system_log.py` — persistent event log surfaced in the System workspace (`log_system_event`, used e.g. by `RadioConnectionManager`).
- `meshsrv/update_service.py` — checks GitHub Releases for a newer version, caching the result in instance-scoped `data/update_check.json` so the browser never calls the GitHub API directly (`api/api_updates.py` serves the cache). `git_preflight()`/`apply_update()` only ever do a plain `git merge --ff-only` after confirming a clean tree and a real, non-diverged upstream — never auto-stash/auto-resolve, only report why an update isn't safe. No auto-rollback on a failed restart; `apply_update()` just records the pre-update SHA for a manual `git checkout <sha>` (a same-process update can't reliably recover itself if the new code fails to start — see the Updates card's own 60s-timeout UI, which surfaces that SHA to the user instead of a real rollback).
- `utils/helpers.py` — small shared utilities.

### Frontend

No build step: `templates/index.html` is a single server-rendered page pulling in `static/chat.js`, `static/media.js`, `static/weather.js`, a vendored `static/chart.umd.min.js`, and Leaflet from a CDN (`unpkg.com/leaflet@1.9.4`). Cache-busting on the local scripts is done manually via `?v=` query strings in the `<script>` tags in `index.html` — bump those when shipping JS changes that must not be served stale from browser cache.

### Form preferences

Reusable, non-content preferences in composer-style forms (checkboxes, notify/mesh toggles, target type, selected channel/node) are persisted server-side, not to `localStorage`: `appSettings.<section>` round-trips through `GET`/`POST /api/settings`, with `normalize_settings()` in `api/api_settings.py` as the source of truth for each section's shape and defaults (`DEFAULT_SETTINGS`). Where a preference should differ per connected radio, the section carries a `profile_defaults` map keyed by the active radio profile ID (lowercased, `[a-z0-9_-]{1,64}`) fetched from `/api/base_status`, with the section's top-level fields as the global/legacy fallback. Content fields (name, description, message text, coordinates, duration) are never remembered — only structural/preference choices are. Reference implementation: the Waypoint creation form's `getWaypointComposerDefaults()` / `saveWaypointComposerDefaults()` / `loadWaypointComposerContext()` (`static/chat.js`, around `openCreateWaypointDialog()`), backed by the `waypoints` settings section. The Timer form (`static/chat.js`, `openTimerForm()`) follows the same pattern via a `timers` settings section and `getTimerComposerDefaults()` / `saveTimerComposerDefaults()` / `loadTimerComposerContext()`, restoring on form open and persisting on each relevant field's `change` event rather than only at submit (Stage 8 addendum, `feature/time-system`).

### Internationalization (i18n)

UI strings go through a hand-rolled runtime, `static/i18n.js` (`window.I18N.t()` / `.plural()` / `.applyStaticDom()`), backed by one JSON catalog per locale under `static/i18n/{en,de,ru,uk}.json`. New user-facing strings must use `I18N.t()` (or `data-i18n*` attributes in static markup) instead of hardcoded English, added to all four catalogs in the same commit. See `static/i18n/CONVENTIONS.md` for the short rulebook (tone per locale, key-naming, plural handling) and `static/i18n/README.md` for the do-not-translate glossary (Meshtastic, Waypoint, GPS, etc.) and the reasoning behind ambiguous cases. Translation coverage is uneven — check the README's "Localization" section before claiming a UI area is fully translated.

### REST API

Routes are split between `server.py` (nodes, chats, waypoints, telemetry, system, radio profile/connection management) and `api/api_*.py` (camera, chat send worker, settings, system actions, node tools, node icons, weather). See the README's "REST API" section for the representative endpoint list; the JS in `static/*.js` is the actual client.

## Deployment

`deploy/meshcenter.service` is a template (`__MESH_USER__` / `__MESH_HOME__` placeholders filled via `sed` at install time, see INSTALL.md) for running under systemd. As of PR #69, its `ExecStart` runs `gunicorn -c gunicorn.conf.py wsgi:app`, not `python server.py` directly - production is a real WSGI server now, not Flask's Werkzeug dev server. `wsgi.py` does `from server import app, start_runtime` and calls `start_runtime()` explicitly at import time, since a WSGI server never executes `server.py`'s own `if __name__ == "__main__":` block.

`gunicorn.conf.py` (repo root, loaded automatically by `-c` + `WorkingDirectory`) hardcodes `workers = 1` - not a performance choice, a correctness requirement. The radio listener owns the serial port exclusively and all runtime state (`nodes`/`chats`/`messages`/`settings`) lives in one process's memory behind `state_lock`; a second worker process would run its own `start_runtime()` and race the first for the same serial port. `start_runtime()` also takes an OS-level `flock()` on `data/runtime.lock` (`_acquire_runtime_lock()` in `server.py`) as a second, independent guard in case `workers` is ever overridden - a second process fails loudly (`[FATAL] ...`) instead of silently corrupting state. `worker_class = "gthread"` (not the `sync` default) is what lets `/video_feed`'s long-lived MJPEG stream coexist with ordinary API traffic instead of blocking the whole process for whoever's watching the camera.

Rollback to the old direct-run command is a one-line `ExecStart` edit, documented in `deploy/meshcenter.service` itself; `python server.py` still works exactly as before for local/manual runs, `start_runtime()`/`app.run()` unchanged.

`deploy/meshcenter.sudoers` and `deploy/meshcenter-wifi.sudoers` grant the narrowly-scoped sudo rules needed for the in-app system actions (restart/reboot/shutdown) and Wi-Fi management (NetworkManager) respectively — extend these rather than widening sudo access when adding new privileged actions.
