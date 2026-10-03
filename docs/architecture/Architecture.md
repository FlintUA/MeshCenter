# MeshCenter Architecture

**Status:** Living document — kept in sync with the code by H2-A's documentation-accuracy pass; please fix this file in the same PR as any change to the systems it describes (see the project's own `CLAUDE.md`, which this document summarizes for a general audience — `CLAUDE.md` is the more detailed, implementation-level reference).

---

## Overview

MeshCenter is a Flask web control center for a Meshtastic LoRa radio attached to a Raspberry Pi over USB serial, TCP, or Bluetooth LE. It combines messaging, an interactive map, telemetry, camera streaming, file transfer (MCAttach), system monitoring, and several optional hardware integrations (e-Paper display, I2C sensors/RTC) into one process, running continuously alongside the radio.

There is no database — all persistence is local JSON files (with two SQLite exceptions, see [Data Storage](#data-storage)) under `data/`.

## License boundary: why there's a subprocess at all

MeshCenter's own code (`server.py`, `api/`, `meshsrv/`, `storage/`, `telemetry/`, `camera/`, everything except `adapters/`) is MIT-licensed. The official [`meshtastic`](https://github.com/meshtastic/python) Python package — the only way to actually open a serial/TCP/BLE connection to a Meshtastic radio from Python — is GPLv3-licensed. To keep GPLv3 code from linking into MeshCenter's own MIT-licensed process, `meshtastic` is used exclusively from `adapters/meshtastic/`: its own Python package, its own virtual environment (separate from Core's), imported only inside a **separate OS process** that Core talks to over a local IPC boundary. Core itself never imports `meshtastic`.

`adapters/meshtastic/` is licensed under GPLv3 **as a whole** (not just the dependency it wraps) — see [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md) and [`adapters/meshtastic/README.md`](../../adapters/meshtastic/README.md) for the full licensing detail. This same arm's-length-subprocess pattern is reused for two other GPL-licensed external *programs* MeshCenter drives but never imports as libraries: the `meshtastic` CLI binary (for the serial listener, see below) and `ffmpeg`/`v4l2-ctl` (for USB camera support, see the Camera entry under [Subsystems](#subsystems) below).

## The radio link

Everything that talks to the radio goes through one of two paths:

**The listener** (`listen_meshtastic()` in `server.py`) runs `meshtastic --listen` as a subprocess and parses its human-readable stdout line by line, classifying each line by substring (`NODEINFO_APP`, `TELEMETRY_APP`, `TEXT_MESSAGE_APP`, etc.) into message/telemetry/node-info/waypoint events. This is fragile by nature (it depends on the CLI's log format) and is deliberately the one piece that still runs inside Core's own process — it only ever shells out to the `meshtastic` CLI *binary*, never imports the Python package, so it doesn't cross the license boundary above. The listener only exists for the **serial** transport; TCP and Bluetooth have no equivalent and are handled entirely by the adapter process.

**Everything else** — connecting, sending, reading node/channel/telemetry state, switching transports — goes through `RadioTransport`, a neutral interface (`meshsrv/radio_transport.py`) implemented three ways (`SerialTransport`, `TCPTransport`, `BLETransport`) inside the adapter subprocess, and reached from Core via `meshsrv/adapter_ipc_client.py`'s `AdapterSupervisor` (newline-delimited JSON on stdin/stdout, protocol in `meshsrv/ipc_protocol.py`). `meshsrv/transport_router.py`'s `TransportRouter` is the one stable object every caller in Core (chat sending, waypoints, the schedule engine) actually calls — it wraps whichever transport is currently active, so callers never need to know which one is live. Switching between Serial/TCP/Bluetooth in Settings asks `TransportRouter` to switch; a separate, short-lived "probe" adapter process (not the production one) is used to verify a TCP radio's identity during Discovery/Accept without touching the live session.

Three independent timeout layers bound every adapter call, from innermost to outermost: the adapter's own internal watchdog (reports its own timeout before anything else can), `AdapterSupervisor`'s own deadline (kills the adapter subprocess if that watchdog doesn't respond in time — the adapter respawns automatically on the next call, no data loss), and the caller's own declared budget. A killed/respawned adapter is a `TransportError`, not a crash.

**TCP's own, narrower scope**: text, waypoints, NodeInfo, positions and telemetry only — no delivery/routing acknowledgements, and no remote waypoint deletion. Everything else about a TCP-connected radio comes from periodic snapshots or manual actions. While **Bluetooth** is the active transport, MeshCenter can send but not receive anything at all — not degraded, fully absent (there's no BLE equivalent of the serial listener yet).

## Multi-radio profiles

Switching between physical radios keeps each one's data isolated: a detected radio's identity (`!xxxxxxxx` node ID) is compared against what's configured, and the listener/adapter refuses to start until that identity matches — this avoids silently mixing one radio's message history with another's. Each known radio gets its own directory under `data/profiles/<8-hex-node-id>/` (messages, nodes, chats, telemetry, waypoints, node icons — see [Data Storage](#data-storage)); switching profiles restarts the whole MeshCenter process so every module rebinds its file paths rather than trying to hot-swap in-memory state.

## Subsystems

Beyond the radio link itself, the active Flask process also runs (as background threads, for its whole lifetime, alongside the radio listener/adapter):

- **MCAttach** (`meshsrv/attachments/`) — file transfer directly between Meshtastic nodes (not channel-wide), with its own end-to-end encryption, a pluggable "Relay" for larger transfers (defaults to a project-hosted instance, self-hostable — see `relay-server/`), and its own SQLite database (`data/mca/<principal-id>/attachments.db` — see [ADR-0003](ADR-0003-attachments-sqlite-exception.md)).
- **Camera** (`camera/`) — a shared driver framework (`camera_manager.py`) behind one live-stream/photo-capture interface, with two real implementations: `csi_driver.py` for a Raspberry Pi Camera (via Picamera2/libcamera) and `usb_driver.py` for a USB/UVC webcam (driving `ffmpeg`/`v4l2-ctl` as external subprocesses — the GPL-isolation pattern described above — rather than importing a GPL-licensed Python library). A USB camera's stream process stops itself automatically once nothing is viewing it. MJPEG-capable USB cameras are supported; the one YUYV-only camera this was verified against cannot exercise the MJPEG path, so that specific path is implemented and unit-tested but not live-verified against a real MJPEG camera.
- **Telemetry** (`telemetry/telemetry.py`) — device/environment/power history, bounded (10,000 records for the local radio, 1,000 per remote node, 40,000 total — oldest remote records evicted first once the total is reached) and flushed to disk on a 60-second debounce rather than on every reading, so a crash (not a clean restart) can lose at most that last ~60 seconds of *history* specifically; live values are unaffected either way.
- **e-Paper display** (`modules/display/`) — optional, for Waveshare/WeAct HATs; a background worker turns state changes into debounced, hash-deduplicated panel refreshes.
- **Hardware** (`hardware/`) — optional I2C bus scanning, a DS3231 real-time clock, and a BME280 environmental sensor, surfaced in the Devices tab without needing SSH.
- **Schedule engine** (`meshsrv/schedule_engine.py`) and **timers** (`meshsrv/timer_service.py`) — scheduled/one-off mesh actions (sends, data reports) and countdown/stopwatch timers, both feeding a shared **notification center** (`meshsrv/notification_service.py`).
- **Weather** (`weather/`) — a pluggable provider registry (OpenWeather, WeatherAPI); requires an API key and outbound internet access when enabled.
- **Update check** (`meshsrv/update_service.py`) — polls GitHub Releases for a newer version (the browser never calls GitHub directly); applying an update is a manual, user-confirmed `git merge --ff-only`, never automatic.
- **Action Engine** (`meshsrv/action_engine.py`) — a lightweight, synchronous action registry/runner that interactive UI operations (Node Tools and similar) dispatch through, so adding a new one doesn't mean adding a new bespoke endpoint/handler shape each time. Deliberately synchronous — Node Tools already serialize access to the radio themselves.
- **Runtime identity** (`meshsrv/runtime_identity.py`) — the filesystem/PATH-search logic behind the serial radio link: resolves the Meshtastic CLI binary (checking the adapter venv, then a few fallback locations, in order), discovers/resolves the serial port, and builds the exact pinned CLI command every serial-side call uses.

## Security model

A setup wizard requires a password (minimum 12 characters) before any page or `/api/` route is usable; every subsequent request is session-authenticated. Changing the password bumps an internal version number that immediately invalidates every *other* open session. Every state-changing `/api/` request requires a CSRF token (issued at login, compared in constant time). Repeated failed logins are throttled with an increasing delay. None of this encrypts the connection itself — MeshCenter is served over plain HTTP by default and is meant for a trusted local network, with a VPN or TLS-terminating reverse proxy for genuine remote access. See [docs/User_Guide.md](../User_Guide.md#18-security-notes) for the user-facing version of this.

## Data Storage

Nearly everything is a JSON file, written atomically (a uniquely-named temp file, `fsync`, then an atomic rename — never a shared fixed temp-file name, which could let a concurrent read delete an in-flight writer's own temp file). A JSON file that fails to parse on read is quarantined (renamed aside with a timestamp, never silently overwritten) rather than replaced with an empty default and re-saved. A route that mutates shared state before saving snapshots that state first and restores it if the save fails, so a failed write never leaves memory and disk permanently disagreeing (reported to the caller as a `storage_write_failed` error).

Two exceptions use SQLite instead of JSON, both because their data needs query/transaction patterns a flat file doesn't fit well: `storage/waypoint_store.py` (`waypoints.db`, per radio profile) and MCAttach's `attachments.db` (per MCA principal, see [ADR-0003](ADR-0003-attachments-sqlite-exception.md)).

Only `data/instance.json` (this installation's identity and active-profile pointer), `data/settings.json`, and `data/screenshots/` are instance-scoped (shared across whichever radio is active). Everything else lives under `data/profiles/<node-id>/` — its own `messages.json`, `nodes.json`, `chats.json`, `sensors.json`, `telemetry_history.json` (the one file written in compact, not pretty-printed, JSON — it's rewritten whole on every flush and can grow large), `waypoints.db`, `node_icons/`, and a few smaller files. Outside the profile system: `data/devices.json` (auxiliary device/sensor metadata), `data/epaper_config.json` and `data/hardware_pending.json` (e-Paper/I2C hardware config), `data/update_check.json` (cached GitHub release info), `data/system_events.jsonl` (the persistent System Log), `data/secret_key.txt` (Flask session signing key), and `data/mca/` (MCAttach's own workspace root, keyed by MCA principal rather than radio profile, since the two aren't the same thing).

## REST API

See [docs/BACKEND_API.md](../BACKEND_API.md) for the hand-written explanations of the important endpoints, and [docs/API_ROUTES.md](../API_ROUTES.md) for the complete, auto-generated list of all 167 routes (regenerated from the code by `scripts/gen_api_inventory.py`; CI fails if it's out of date).

## Deployment

Production runs under [Gunicorn](https://gunicorn.org/) (`gunicorn.conf.py`, `wsgi.py`), not Flask's own dev server. `workers = 1` is a correctness requirement, not a performance choice: the radio listener owns the serial port exclusively, and all runtime state lives in one process's memory — a second worker would race the first for the same port and the same in-memory state. An OS-level file lock is a second, independent guard against that. `worker_class = "gthread"` is what lets the live camera stream coexist with ordinary API traffic instead of blocking the whole process for whoever's watching it.

## Project layout

```
server.py              # Flask app, shared state, most route handlers
api/                    # Newer route modules (NOT Flask Blueprints — plain
                        # functions taking the shared app/state as parameters)
meshsrv/                # Radio transport, IPC, schedule engine, MCAttach, ...
storage/                # JSON/SQLite persistence helpers
telemetry/              # Telemetry history storage/aggregation
camera/                 # Camera driver framework (CSI + USB)
weather/                # Pluggable weather provider registry
hardware/               # I2C/RTC/BME280 support
modules/display/        # Optional e-Paper display subsystem
adapters/meshtastic/    # GPLv3-isolated Meshtastic transport adapter
static/, templates/     # No-build-step frontend (vanilla JS/CSS)
docs/                   # This document, the API reference, ADRs, ...
```

## Future direction

The project's own stated direction is to keep shrinking `server.py` by moving logic into the modules above as each area is genuinely a separate concern — not a wholesale rewrite. Beyond that, no specific architectural changes are currently planned; see [docs/development/Roadmap.md](../development/Roadmap.md) for feature-level direction.
