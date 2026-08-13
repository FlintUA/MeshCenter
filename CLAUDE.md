# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MeshCenter is a Flask web control center for a Meshtastic LoRa radio attached to a Raspberry Pi over USB serial. It talks to the radio exclusively through the official `meshtastic` CLI (never the Meshtastic Python API directly): one long-lived `meshtastic --listen` subprocess whose stdout is parsed line-by-line with string/regex matching, plus short-lived `meshtastic --info` / `--sendtext` invocations for one-off commands. There is no database — all persistence is local JSON files (one SQLite file for waypoints) under `data/`.

## Running it

There is no build step, package.json, linter config, or test suite in this repo — don't invent `npm run`, `pytest`, or lint commands that don't exist here.

```bash
python3 -m venv --system-site-packages venv   # --system-site-packages required for Picamera2 on the Pi
source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py                # edit MESHTASTIC_PORT / LOCAL_NODE_ID / LOCAL_NODE_NAME
mkdir -p data

python server.py                               # dev run, http://<host>:5000
# or, in production:
sudo systemctl restart meshcenter.service       # see deploy/meshcenter.service
```

`config.py` and `weather_secrets.py` are gitignored local files; `server.py` exits at import time if `config.py` is missing or missing required variables (see the `required_vars` check near the top of `server.py`). When changing code that reads config, check `config.example.py` for the authoritative variable list.

Manual verification is normally done against a real or simulated radio (`meshtastic --port <dev> --info`) and by exercising the REST endpoints/UI in a browser — there's no automated test harness to run instead.

## Architecture

### `server.py` is the core, not a thin entrypoint

`server.py` (~5,200 lines) owns the Flask `app`, nearly all shared mutable state (`nodes`, `messages`, `chats`, `settings`, locks, background threads), and most route handlers that haven't yet been split out. Newer feature areas live in `api/*.py`, but they are **not Flask Blueprints** — each is a plain function `register_<area>_routes(app, state_lock, ..., <30+ shared globals/functions>)` called from `server.py`, closing over the objects/functions passed in. When adding a new route module, follow this dependency-injection-by-parameter-list pattern rather than introducing Blueprints, and wire the new `register_*_routes(...)` call into `server.py`.

The project's own stated direction (see README "Modular Architecture") is to keep shrinking `server.py` by moving logic into `api/`, `meshsrv/`, `storage/`, `telemetry/`, `camera/` — prefer extending those modules over growing `server.py` further when the code is genuinely a separate concern.

### The radio link: subprocess + text parsing, not an SDK

`listen_meshtastic()` in `server.py` runs `meshtastic --listen` as a subprocess and classifies each stdout line by substring checks (`"NODEINFO_APP"`, `"TELEMETRY_APP"`, `"WAYPOINT_APP"`, `"TEXT_MESSAGE_APP"`, etc.), then hands multi-line blocks to parsers like `process_nodeinfo`, `parse_telemetry_from_listen_line`, `parse_waypoint_from_listen_line`. This is fragile by nature (it depends on the CLI's human-readable log format) — when the CLI output format changes across `meshtastic` package versions, these parsers are what breaks. Sending uses separate short-lived CLI calls (`meshsrv/meshsrv.py: send_text`, and the send worker in `api/api_chat.py`) that must not run concurrently with the listener on the same serial port — that's what `radio_lock`, `pause_listen` (a `threading.Event`), and `RadioConnectionManager` (`meshsrv/radio_manager.py`) coordinate. `stop_listener()` / `wait_serial_release()` / `prepare_radio_command()` in `server.py` implement the "stop listener, wait for the OS to actually free the serial device, run the one-off command, resume listener" dance — reuse them rather than calling the CLI directly from new code.

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

- `camera/camera.py` — Picamera2-based MJPEG live stream + photo capture; needs the venv built with `--system-site-packages` to see system Picamera2/libcamera bindings.
- `telemetry/telemetry.py` — telemetry history storage/aggregation (`configure_storage()` is pointed at the active profile's `telemetry_history.json` at startup and after a profile switch).
- `weather/` — pluggable weather backend. `weather/providers/base.py` defines the `WeatherProvider` interface (each provider normalizes its own condition codes into a shared `CONDITION_KEYS` vocabulary so `static/weather.js` never has to know which provider is active); `weather/providers/openweather.py` and `weather/providers/weatherapi.py` implement it for OpenWeather and WeatherAPI; `weather/weather_manager.py` is the registry that tracks which provider is active (`settings.weather.provider`, mirrors `settings.maps.provider`) and switches on save. Both providers share one cache-per-request-window shape (`WEATHER_CACHE_SECONDS`); API keys come from gitignored `weather_secrets.py` (one variable per provider, e.g. `OPENWEATHER_API_KEY` / `WEATHERAPI_API_KEY`), never from the browser.
- `system_log.py` — persistent event log surfaced in the System workspace (`log_system_event`, used e.g. by `RadioConnectionManager`).
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

`deploy/meshcenter.service` is a template (`__MESH_USER__` / `__MESH_HOME__` placeholders filled via `sed` at install time, see README step 8) for running under systemd. Its `ExecStart` runs `python server.py` directly — that means production actually serves requests through Flask's built-in Werkzeug dev server (`app.run(..., debug=False, threaded=True)` at the bottom of `server.py`), not through a real WSGI server. `wsgi.py` (`from server import app`) exists and is correctly importable, but nothing in this deployment path invokes it via Gunicorn/uWSGI/Waitress — it is currently dead code, not the production entrypoint its own docstring claims. `debug=False` keeps the interactive debugger (the actual RCE-risk part of the dev server) off, and this is a LAN-only single-user deployment, so the practical exposure is limited — but don't describe `wsgi.py` as "the" production entrypoint elsewhere in this repo until something in `deploy/` actually runs it that way. `deploy/meshcenter.sudoers` and `deploy/meshcenter-wifi.sudoers` grant the narrowly-scoped sudo rules needed for the in-app system actions (restart/reboot/shutdown) and Wi-Fi management (NetworkManager) respectively — extend these rather than widening sudo access when adding new privileged actions.
