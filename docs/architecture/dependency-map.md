# Dependency map (D0)

**Status:** read-only audit, no behavior changed. Built against `origin/main` @ `2adf32a`. Covers `server.py` (summarized; full detail in `server-decomposition-audit.md`), `api/`, `meshsrv/`, `storage/`, `camera/`, `hardware/`, `telemetry/`, `weather/`, `modules/display/`, `adapters/meshtastic/`, `devices/`, `system/`, `utils/`.

**Method:** a full-repo grep sweep for the specific risk categories the D0 brief named (circular imports, direct `server`/`adapters` imports, subprocess/`sudo`/`nmcli`/`systemctl`/`bluetoothctl`, `/dev/` access, GPIO/I2C/SPI, `flock`/`fcntl`, hardcoded paths, venv/`--system-site-packages` assumptions, `libcamera`/`picamera2` imports, config-loading mechanics), cross-checked against direct reads of the flagged files, plus a structural per-file inventory of every module outside `server.py`/`api/*.py` (which have their own detailed sections below and in `server-decomposition-audit.md`).

---

## 1. Headline findings (read this first)

1. **Zero circular imports found.** Nothing under `api/`, `meshsrv/`, `storage/`, `camera/`, `hardware/`, `telemetry/`, `weather/`, `modules/`, `devices/`, `system/`, `utils/` imports `server`. `server.py` is a true composition root — every dependency edge points into it, none point back out. The only `import server`/`from server import` hits anywhere in the repo are the WSGI entrypoint (`wsgi.py`, expected) and test/tooling harnesses that construct a synthetic environment first (`tests/conftest.py`, `tests/test_server_startup_degraded_serial.py`, `scripts/_smoke_test_core_harness.py`, `docs/theme-registry/tools/_visual_server.py`).
2. **Zero violations of the GPLv3 adapter boundary.** Nothing outside `adapters/meshtastic/` imports `adapters`/`adapters.meshtastic` — confirmed by grep across the whole non-test codebase. The only "hits" are: internal `adapters/meshtastic/*.py` files importing each other (expected), test files exercising the adapter package directly for unit testing (`tests/test_ipc_server.py`, `tests/test_ble_transport.py`, etc. — never happens in the shipped runtime), and a CI smoke test (`scripts/_smoke_test_core_harness.py`) that *asserts* `import adapters` fails from a Core-only extracted artifact — i.e. the enforcement mechanism for the boundary, not a violation of it. `server.py` itself only imports `meshsrv.adapter_ipc_client.{AdapterIPCTransport, AdapterSupervisor}` and `meshsrv.serial_port_supervisor.SerialPortSupervisor` (MIT, stdlib-only) — confirmed via direct read.
3. **The `meshtastic` Python package is imported lazily, inside functions, in exactly three places, all inside `adapters/meshtastic/`**: `serial_transport.py::_open_interface()` (`from meshtastic.serial_interface import SerialInterface`), `ble_transport.py::connect()`'s inner `_do_connect()` and `ble_transport.py::scan()`'s inner `_do_scan()` (both `from meshtastic.ble_interface import BLEInterface`). None are top-level imports. `adapters/meshtastic/ipc_server.py` itself only imports its own siblings and `meshsrv.*` at top level — even the subprocess entrypoint doesn't touch the `meshtastic` package before a request actually needs it.
4. **`libcamera`/`Picamera2` import risk is confined to `camera/`,** and even there it's handled defensively: `camera/camera.py` does `from libcamera import Transform, controls` at top level but wrapped in `try/except ImportError` (sets `LIBCAMERA_AVAILABLE = False`, stubs the names to `None`); `from picamera2 import Picamera2` is fully deferred, inside `init_camera()` only. `camera/csi_driver.py`'s own Picamera2 touch is also deferred (inside `_detect_usb_ids()`). No `smbus`/`smbus2`/`RPi.GPIO` import exists anywhere in the repo — all I2C/RTC access goes through CLI tools (`i2cdetect`/`i2cget`/`i2ctransfer`/`hwclock`) via `subprocess`, not a Python bus library. `tests/conftest.py` installs a shallow auto-vivifying `libcamera` stub specifically because `server.py` imports `camera.camera` unconditionally at its own module-import time (see `server-decomposition-audit.md` §1) — first-party confirmation that this import-time coupling is real, not hypothetical, and was a genuine breakage on non-Pi machines before the guard existed.
5. **Config loading (`config.py`) has no absolute-path or env-var resolution mechanism at all.** `server.py:90-98`'s `from config import *` and `gunicorn.conf.py`'s `from config import APP_HOST, APP_PORT` are both bare Python imports resolved via `sys.path`/CWD — meaning `config.py` (gitignored, holds real secrets per `INSTALL.md`) must sit in whatever directory the process's working directory resolves to, with no `CONFIG_PATH`/`MESHCENTER_CONFIG` env-var override anywhere in the codebase. This is a genuine Twelve-Factor gap or a container-image gap: it forces "bind-mount `config.py` at a fixed `WORKDIR`" rather than "point at an external secret via env var." Two internal tools (`scripts/_smoke_test_core_harness.py`, `docs/theme-registry/tools/_visual_server.py`) work around this today by synthesizing a throwaway `config.py` on disk and manipulating CWD — not by using an env var, because none exists.

---

## 2. Per-directory dependency summary

### `server.py` → everything

Full inventory in `server-decomposition-audit.md`. Summary for this document's purposes: imports from `camera`, `telemetry`, `meshsrv.*` (11 names), `storage.*` (3 classes), `api.*` (13 `register_*_routes` functions), `system.cpu_history`, `hardware.hardware_config`, `weather.*`, `modules.display.*`. No file anywhere imports `server` back (see §1.1). `server.py` is the only place all these dependency edges converge — which is exactly what makes it large, and exactly why splitting it is safe from a *dependency-direction* standpoint (nothing downstream needs to change its own imports) but risky from a *shared-mutable-global* standpoint (see `server-decomposition-audit.md` §2/§6).

### `api/*.py` (16 modules, all DI-by-parameter-list, none are Flask Blueprints)

Every module exports `register_<area>_routes(app, ...)`, called once from `server.py` at import time. None import `server`. None import `meshtastic`/`adapters`. Signature length is the clearest signal of coupling depth — from 2 parameters (`api_attachments.py`, talking only to an external singleton facade) to 27 (`api_chat.py`, deeply coupled to `server.py`'s in-memory `nodes`/`chats`/`messages`).

| Module | Routes | `register_*` signature size | Shared state touched | Registration-time side effect | Direct test coverage |
|---|---|---|---|---|---|
| `api_attachments.py` | 31 | 2 params | None of server.py's — talks only to `meshsrv.attachments.mca_runtime`'s external facade singleton | none | **Best in the codebase**: `tests/test_mca_api_attachments.py`, 181 tests |
| `api_auth.py` | 5 routes + 2 `before_request` hooks + 1 `context_processor`, applied to **every** route in the app | 6 params | `auth_state` dict (own), `state_lock` | none | `tests/test_api_auth.py` (41), `tests/test_api_csrf.py` (19) |
| `api_camera.py` | 15 | 5 params, **returns a bool** (`power_state["enabled"]`) consumed by `server.py`'s `__main__` block | `camera_manager_state` dict (shared by reference with `api_camera_manager.py`) | `load_power_state()` + possibly `close_camera_device()` (real hardware stop) run **during registration**, i.e. at `server.py` import time | `tests/test_api_camera.py` (4) |
| `api_camera_manager.py` | 3 | 4 params | same `camera_manager_state` | none | **none found** |
| `api_chat.py` | 5 | **27 params** (longest in the codebase) | `chats`, `nodes`, `messages` (all server.py globals, by reference) | **spawns a live daemon thread** (`send_worker`) unconditionally on every registration/import | injected primitives only tested (`tests/test_message_queue.py`); **routes/worker themselves: none found** |
| `api_hardware_bme280.py` | 1 | 2 params | none | none | `tests/test_api_hardware_bme280.py` (2) |
| `api_hardware_display.py` | 8 | 10 params | `config`/`ui_state`/`gpio_registry`/`display_manager` (all shared) | none | `tests/test_api_hardware_display_rotation.py` (12) |
| `api_hardware_i2c.py` | 5 | 3 params | none (delegates to `hardware.hardware_config`) | none | `tests/test_api_hardware_i2c.py` (14) |
| `api_meshtastic.py` | 5 | 11 params | `settings["meshtastic"]` section, `transport_router` | none | `tests/test_api_meshtastic.py` (4) |
| `api_node_icons.py` | 3 | 4 params, **returns `icon_path` closure** (test-only seam) | none | creates `data/profiles/<id>/node_icons/` at registration time | `tests/test_api_node_icons.py` (5) |
| `api_node_tools.py` | 1 (dispatches by `action` via `meshsrv.action_engine`) | 11 params | `nodes` | **raises `RuntimeError` at registration time** if the CLI path/serial port are invalid — a hard startup-time validation | **none found** |
| `api_settings.py` | 3 | 7 params, exports `normalize_settings`/`DEFAULT_SETTINGS` (imported directly by `server.py` too) | `settings` dict | none | `normalize_settings()` only: `tests/test_normalize_settings.py` (15). **Route handlers: none found** |
| `api_system.py` | 23 | 3 params (small signature, large scope — notifications/schedules/timers all delegate via **function-local** imports of `meshsrv.*`) | none of server.py's — self-contained, delegates everywhere | none | `tests/test_api_system.py` (8) — covers Wi-Fi/system-action/system-info only; notifications/schedules/timers/network/time routes not covered by this file |
| `api_updates.py` | 4 | 4 params | none | none | `tests/test_api_updates_apply.py` (3) |
| `api_waypoints.py` | 7 | 11 params | `waypoint_store` (SQLite) | none | `tests/test_api_waypoints.py` (20) |
| `api_weather.py` | 3 | 4 params, **no `handle_errors` param at all** (only module that skips the shared error decorator) | `weather_manager` | none | **none found** |

**Cross-cutting `api/` findings:**
- The `radio_transport` parameter name is misleading in both `api_chat.py` and `api_waypoints.py` — `server.py` actually binds a `TransportRouter` (`transport_router`) into that slot, not a raw `RadioTransport` implementation.
- Only `api_waypoints.py` touches SQLite directly (via the injected `WaypointStore`); `api_attachments.py`'s own docstring explicitly forbids itself from ever touching SQLite.
- `storage/json_store.py`'s `safe_read_json`/`safe_write_json` is used by exactly one `api/*.py` file (`api_auth.py`) — every other module either delegates to an injected `save_*` closure owned by `server.py`, or writes raw files itself (`api_camera.py`, `api_node_icons.py`, `api_weather.py`, `api_hardware_display.py` via `modules/display/config_store.py`).
- Modules with **no direct route-level test coverage at all**: `api_camera_manager.py`, `api_node_tools.py`, `api_settings.py` (routes, not `normalize_settings`), `api_weather.py`, and effectively `api_chat.py` (only its injected primitives are tested).

### `meshsrv/*.py` (21 modules besides the IPC/radio-transport cluster covered in `ipc-carrier-audit.md`)

None import `server`. None import `meshtastic`. The DI-by-parameter-list pattern extends here too: `meshsrv/schedule_actions.py` and `meshsrv/schedule_engine.py` both take an explicit `configure()`/`start()` call carrying `nodes`/`state_lock`/`radio_transport`/etc. by reference specifically to avoid `from server import ...` (documented at length in `schedule_actions.py`'s own docstring, citing an explicit prior review finding).

| Module | Owns | Shared state | I/O | Background thread | Test coverage |
|---|---|---|---|---|---|
| `action_engine.py` | Generic action registry/runner (`ActionRegistry`, `ActionRunner`) | None — instantiated fresh per `register_node_tools_routes()` call | None | No | **none found** |
| `connectivity_monitor.py` | MCAttach relay/internet health probing | Instance state on `ConnectivityMonitor`, one long-lived instance owned by `mca_runtime` | HTTPS probes (`requests`), reads via `ProviderRegistry` (SQLite, out of scope) | No (uses a scoped `ThreadPoolExecutor` per `refresh()` call, not persistent) | `tests/test_connectivity_monitor.py` + 4 more |
| `installation_identity.py` | `MC1-XXXX-...` ID generation/validation | None | None | No | `tests/test_installation_identity.py` + 2 more |
| `installation_time_assignment.py` | One-time `installation.assigned_at` stamping once NTP is confirmed | None (closures only) | Writes via `instance_manager.save()` | **Yes** — daemon thread, started explicitly from `server.py`'s `start_runtime()` | `tests/test_installation_time_assignment.py` + 1 more |
| `instance_manager.py` | `data/instance.json` (`InstanceManager`) | `self._data` under `self._lock`; **server.py keeps its own `INSTANCE_IDENTITY` global mirror** rather than always calling `.get()` — a duplication worth simplifying | `storage.json_store` (reused correctly) | No | `tests/test_instance_manager.py` + 4 more |
| `meshtastic_transport.py` | `get_info()` — the one `meshtastic --info` CLI wrapper server.py always calls through | None | `subprocess.run([cli, "--port", port?, "--info"])` | No | `tests/test_meshtastic_transport.py` + 2 more |
| `network_config.py` | **Sole** privileged Wi-Fi chokepoint | None | `sudo -n <helper> list-connections\|scan\|connect\|forget`, password via stdin never argv | No | `tests/test_network_config.py` + `test_api_system.py` |
| `node_time_sync.py` | `try_sync()` — node clock sync | Module-level `_sync_lock`/`_last_sync_ts` throttle, shared process-wide | None directly (radio I/O via caller-supplied `interface`) | No | **none found**. Also: `evaluate_drift()`/`_get_node_time()` are confirmed **dead code** (documented as forward-compat scaffolding, unreachable from `try_sync()`), with no test guarding that "kept intentionally" claim. **Imported by the adapter subprocess itself** (`serial_transport.py`/`ble_transport.py`) as well as Core — must stay `meshtastic`-import-free, and does |
| `notification_service.py` | In-memory notification queue (cap 50, no persistence) | Module-level `_lock`/`_queue` | None | No | **none found** (incidental match only) |
| `radio_identity.py` | Identity parsing/comparison/detection | None (stateless) | `meshtastic_transport.get_info()`, `os.path.exists`/`realpath` against `runtime_identity.discover_serial_ports()` | No | `tests/test_radio_identity.py` + 5 more. `detect_connected_radio()` has **no confirmed live caller** in `server.py`/`api/*.py` — worth confirming it isn't dead |
| `radio_manager.py` | `RadioConnectionManager` (5-state machine: connected/releasing/released/reconnecting/error) | Instance state under `self._lock`; single instance held by `server.py` | None directly (delegates to injected callbacks) | No | **none found**, despite ~20 call sites across `server.py` and a nontrivial state machine — see §3 finding below |
| `runtime_identity.py` | CLI/venv/serial-port resolution | None (pure functions) | `glob`, `Path.is_file`/`os.access`, `shutil.which` | No | Indirect via `conftest.py`, `test_radio_identity.py`, `test_server_startup_degraded_serial.py` |
| `schedule_actions.py` | Executes one schedule action (`log_entry`/`mesh_send`/`send_data_report`) | Module-level globals set once via `configure(nodes, state_lock, radio_transport, ...)` (DI, not owned) | Sends via injected `radio_transport.send_text()`; `_FIELD_GROUP` hard-mirrors `server.py`'s `apply_node_telemetry()` schema by line-number cross-reference | No | **none found** |
| `schedule_engine.py` | Schedule CRUD + minute-granularity ticker | Module-level `_lock`/`_running`; **no in-memory cache** — every call re-reads the whole file | `data/schedules.json` via **raw, non-atomic** `Path.read_text()`/`write_text()` (not `storage.json_store`) | **Yes** — `start()` spawns the ticker thread from `server.py`'s `start_runtime()` | **none found**, despite firing unattended, arbitrary mesh sends on a timer — flagged as one of the highest-risk untested modules in this audit |
| `time_service.py` | Single source of truth for NTP/timezone/RTC status | Two independent caches (`_cache` 20s TTL, `_rtc_cache` 300s TTL) under one `_lock` | `timedatectl` (systemd), `/etc/timezone`, `/etc/localtime`, delegates RTC to `hardware.rtc_service` | **Yes** — background thread from `server.py`'s `start_runtime()` | `tests/test_time_service.py` + 1 more |
| `timer_service.py` | In-memory stopwatch/countdown bookkeeping (session-scoped) | Module-level `_lock`/`_timers` dict, resets on restart | None | No | **none found**. `get_timer()`/`get_elapsed()` have no confirmed live caller — possibly dead |
| `update_service.py` | Self-update: check (network+cache) / preflight (git read-only) / apply (git merge) | `_lock`/`_cache_path` (set via `configure()`) | `urlopen(GITHUB_API_URL)`, `git rev-parse`/`status`/`fetch`/`rev-list`/`diff`/`merge --ff-only` (all `cwd=project_dir`) | check_worker() runs inside a `server.py`-owned thread, not one it starts itself | `tests/test_api_updates_apply.py`, `tests/test_update_service_requirements_changed.py` |

**Cross-cutting `meshsrv/` findings (feeding §3 below):**
- `meshsrv/radio_manager.py`'s domain logic is already cleanly isolated, but every one of its ~20 call sites and the actual `/api/radio_connection/*` HTTP routes live directly in `server.py` — unlike almost every other subsystem, which has a dedicated `api/api_*.py` wrapper. Strongest "extract a thin `api/api_radio.py`" candidate found anywhere in this audit.
- Privileged `sudo -n` calls are **not** fully centralized the way `network_config.py`'s own docstring claims for Wi-Fi: `server.py:4950-4951` independently shells out to `sudo -n /usr/bin/systemctl restart meshcenter.service`, with no dedicated module — `update_service.py` only *tells the user* to run that command manually, it never runs it itself.
- `meshsrv/node_time_sync.py` is confirmed as a genuine dual-environment module (imported by both Core and the isolated adapter subprocess) — any future refactor must preserve its `meshtastic`-import-free status.

### `storage/*.py`

| Module | Owns | Reuses `json_store`? | Test coverage |
|---|---|---|---|
| `json_store.py` | `safe_read_json`/`safe_write_json`/`atomic_write_json` (alias, no confirmed caller) | n/a — this is the primitive | No dedicated test file; only incidental through callers |
| `device_manager.py` | `data/profiles/<id>/devices.json` (camera/sensor assignment) | **No — reimplements the same tempfile+`os.replace` pattern independently**, a second parallel implementation | Indirect only, via `test_api_camera.py` |
| `profile_manager.py` | `data/profiles/<id>/profile.json` + the legacy-flat-file migration | **Yes** — correct reuse | `tests/test_profile_manager.py` |
| `waypoint_store.py` | `data/profiles/<id>/waypoints.db` (SQLite) | n/a (SQLite) | Indirect only, via `test_api_waypoints.py` — no dedicated test of the composite-key schema migration inside `_initialize()` |

**The single highest-leverage, lowest-risk consolidation finding in this whole audit:** `storage/json_store.py`'s atomic-write pattern is independently reimplemented **three separate times** elsewhere in the codebase, with at least one confirmed behavioral divergence:
1. **`server.py:734-777`** defines its own near-duplicate `safe_read_json`/`safe_write_json`/`atomic_write_json`, used by 7+ call sites (`messages.json`, `chats.json`, `nodes.json`, `sensors.json`, `settings.json`, `telemetry.telemetry_history`, etc.) — and it is **not a byte-for-byte copy**: server.py's version does **not** call `os.makedirs(directory, exist_ok=True)` before writing the temp file, unlike `storage/json_store.py`'s version (usually harmless today only because `DATA_DIR` is created earlier at import time, but a real, confirmed divergence between two same-named functions in the same codebase).
2. **`storage/device_manager.py`** reimplements the same atomic-write pattern via `tempfile.mkstemp` independently, in the same `storage/` package that already has the canonical version.
3. **`meshsrv/schedule_engine.py`** skips atomicity entirely (`Path.write_text()`, no temp file, no `os.replace`) — the only JSON-backed module found in this entire audit that does not use *some* atomic-write pattern.
4. **`camera/camera.py`** also reimplements the pattern independently (noted in the camera/hardware inventory below).

Only `api/api_auth.py`, `meshsrv/instance_manager.py`, `meshsrv/update_service.py`, and `storage/profile_manager.py` currently import and reuse `storage.json_store` correctly.

### `camera/`, `hardware/`, `telemetry/`, `weather/`

Full detail already captured by the parallel audit; condensed here for the dependency-graph view:

- **`camera/camera.py`** (1299 lines): the architectural odd-one-out — **all state is bare module globals** (`CAMERA_AVAILABLE`, `picam2`, `VIDEO_CONFIG`, etc.), not instance-scoped, meaning there can only ever be one camera per process. `camera/csi_driver.py`'s `CsiCameraDriver` is only a thin ABC-conformance wrapper over these same globals, not a genuinely independent instance. Creates `data/` and `data/screenshots/` **unconditionally at import time** (`os.makedirs`, lines 47-48) — a real, confirmed side effect of merely `import camera.camera`. Reimplements `safe_read_json`/`safe_write_json` independently (a fourth parallel copy, see above).
- **`camera/camera_manager.py`**/**`camera/camera_driver.py`**: the intended pluggable-driver registry (`CameraManager`, `CameraDriver(DeviceDriver)`), instance-scoped and clean, but only fully realized for **`camera/usb_driver.py`**'s `UsbCameraDriver` (fully self-contained, `linuxpy`/`PIL` imports all deferred, genuinely supports multiple concurrent USB cameras) — CSI support is still a facade over `camera.py`'s legacy global-singleton design.
- **`hardware/{bme280,i2c,rtc}_service.py`**: no Python I2C library dependency at all — every one uses CLI tools (`i2cdetect`, `i2cget`/`i2ctransfer`, `hwclock`) via `subprocess`, which is why the hardware/ package has zero top-level hardware imports and is fully import-safe off-Pi. `hardware/hardware_config.py` is the sole `sudo -n meshcenter-hw-config` chokepoint (mirrors `network_config.py`'s pattern) and reuses `storage.json_store` correctly for `data/hardware_pending.json`.
- **`telemetry/telemetry.py`**: **two-way stateful coupling with `server.py`** — `server.py` directly mutates `telemetry.telemetry_current`/reads `telemetry.telemetry_history` as attribute access rather than through an API, in both directions. `configure_storage()` rebinds the module's own `TELEMETRY_FILE` global per active profile (confirming this file is **per-profile**, not instance-scoped, despite living in a package with no "profile" in its name). **No dedicated test file exists for `telemetry/telemetry.py`** — a meaningful gap given how central it is and how much direct-global-mutation coupling exists.
- **`weather/`**: the cleanest pluggable-driver pattern in the repo — `WeatherProvider` ABC + `CONDITION_KEYS` shared vocabulary, both providers (`OpenWeatherProvider`, `WeatherApiProvider`) fully instance-scoped with zero module globals, explicitly cross-referenced in both `weather_manager.py`'s and `camera_manager.py`'s own docstrings as the pattern camera's driver registry is *supposed* to mirror. Worth citing as the reference implementation for any future pluggable-driver work. **No test coverage found anywhere in `weather/`.**

### `modules/display/`, `adapters/meshtastic/`, `devices/`, `system/`, `utils/`

- **Import-time hardware touch: confirmed zero**, across every file in `modules/display/`. SPI/GPIO access (`gpiozero`, `spidev`, the vendored Waveshare `epdconfig`) is deferred to each driver's `start()`/`render()` method, never module import. The one exception is `waveshare_213g.py`'s `sys.path.insert()` (adding a vendor directory) — a path mutation, not a hardware touch.
- **`modules/display/service.py::epaper_worker()` is confirmed pull-only** — polls a fixed set of `get_*` callables injected from `server.py` (all reads of already-collected in-memory state), never calling `meshtastic --info` or any radio operation itself. This is the one place in the repo explicitly designed for eventual out-of-process operation, but today it relies on direct-callable injection, not an RPC/shared-state channel — a future split would need to replace that injection mechanism.
- **`adapters/meshtastic/ipc_server.py`** is never imported in-process by anything — it's launched as `<adapter venv>/bin/python -m adapters.meshtastic.ipc_server`, a genuinely separate subprocess. Full IPC-boundary detail in `ipc-carrier-audit.md`.
- **`devices/device_driver.py`** is real, minimal shared infrastructure, not scaffolding — exactly two hierarchies depend on it (`CameraDriver`, `DisplayDriver`), both audited above.
- **`system/cpu_history.py`**: reads `/proc/stat`, `/proc/meminfo`, `/proc/uptime`, `/sys/class/thermal/thermal_zone0/temp` directly; owns the one background thread (`cpu_history_worker`) that's unconditional in `start_runtime()` regardless of radio identity match.
- **`utils/helpers.py`**: `get_device_model()` reads `/proc/device-tree/model`, memoized — same source `api/api_system.py`'s System Information card uses, kept in sync deliberately.
- **Test-coverage gaps confirmed absent from the pytest suite** (hardware-adjacent standalone `tools/test_*.py` scripts exist for some of these but are not part of `tests/` / CI): `modules/display/gpio_registry.py`, `modules/display/manager.py`, `modules/display/drivers/waveshare_213g.py`, `modules/display/drivers/weact_154.py`, `modules/display/drivers/_weact_ssd1681.py`, `modules/display/pages/test_pattern.py`, `devices/device_driver.py`.

---

## 3. Flagged dependency categories (the D0 brief's explicit checklist)

### 3.1 Circular imports — none found (§1.1)

### 3.2 Direct imports of `server` — none outside composition-root/tooling (§1.1)

### 3.3 Global singleton dependencies

The recurring pattern across the codebase is **"a manager class owns the real state, `server.py` additionally caches a snapshot in its own global"**:
- `InstanceManager` (owns `data/instance.json`) ↔ `server.py`'s `INSTANCE_IDENTITY` global, refreshed after every `.save()`/`.load_or_create()` call rather than always calling `.get()`.
- `ProfileManager` ↔ `server.py`'s `PROFILE_CONTEXT` dict cache.
- `weather_manager`, `radio_connection_manager`, `transport_router`, `display_manager`, `camera_manager_state["manager"]` — each a single module-level instance in `server.py`, shared by reference into the `api/*.py` modules that need it (constructor injection, not a second competing instance).
- `telemetry.telemetry_current`/`telemetry.telemetry_history` are the one place this pattern inverts: `server.py` doesn't cache a snapshot, it **directly mutates the `telemetry` module's own globals** as if they were its own — the tightest, least API-mediated coupling found anywhere in this audit (see §2's `telemetry/` entry).

### 3.4 Direct `/dev/...` access

| Path pattern | Files | Purpose |
|---|---|---|
| `/dev/ttyACM*`, `/dev/ttyUSB*`, `/dev/serial/by-id/*` | `meshsrv/runtime_identity.py::discover_serial_ports()`, `api/api_node_tools.py::_resolve_serial_port()` (independent glob fallback), `config.example.py`/`server.py` (`MESHTASTIC_PORT` default) | Meshtastic radio serial port discovery |
| `/dev/video*` | `camera/usb_driver.py` (enumeration + regex filter, excludes `bcm2835-isp`/metadata nodes) | USB/UVC camera discovery |
| `/dev/i2c-{bus}` | `hardware/i2c_service.py`, `hardware/hardware_config.py` (comment re: `i2c-dev` kernel module) | I2C bus existence check before shelling to `i2cdetect` |
| `/dev/rtc0`, `/sys/class/rtc` | `hardware/rtc_service.py`, `meshsrv/time_service.py` | RTC hardware detection |
| `/dev/spidev{bus}.{device}` | `modules/display/drivers/weact_154.py`, `modules/display/drivers/waveshare_213g.py` | e-Paper SPI device existence probe (not opened directly — actual I/O via `spidev`/`gpiozero`) |

Every one of these needs explicit `--device=` passthrough (or a privileged/full-`/dev` mount) in any container split; device paths (especially `ttyACM*`/`video*`) are non-deterministic across reconnects, which is exactly why `runtime_identity.py` already prefers `/dev/serial/by-id/*` — the same preference should extend to any container device-mapping scheme.

### 3.5 `os.uname`, `platform.*`

`server.py:283` (`os.uname().nodename`, platform-guarded), `api/api_system.py` (`platform.release()`, `platform.node()`), `utils/helpers.py::get_device_model()` (`platform.node()` fallback). All of these report **container-internal** values (container hostname, not the physical Pi's) once containerized — `platform.release()` is the one exception, since containers share the host kernel. Worth a UI-facing note wherever "System Info" is surfaced.

### 3.6 `systemctl`/`systemd` — pervasive; see `docker-blockers.md` §2 for the full breakdown (deployment model, orphan-protection mechanism, self-restart triggers).

### 3.7 `sudo` — three narrowly-scoped chokepoints (`systemctl restart|reboot|poweroff`, `meshcenter-network-helper`, `meshcenter-hw-config`); see `docker-blockers.md` §3.

### 3.8 `nmcli`/`iw`/`bluetoothctl`

- `nmcli`/`iw` (privileged): confined entirely to `scripts/meshcenter-network-helper` (outside this audit's Python-file scope, but the sole chokepoint by design).
- `iw`/`iwgetid`/`hostname -I`/`ip route` (unprivileged reads): `api/api_system.py`, deliberately **not** routed through the privileged helper (documented split in `meshsrv/network_config.py`'s own docstring, confirmed correct by grep — no accidental duplication).
- `bluetoothctl disconnect <address>`: three call sites — `adapters/meshtastic/ble_transport.py` (from inside the adapter process), `meshsrv/adapter_ipc_client.py` (from **Core's own process**, after Core kills a misbehaving adapter). This dual-ownership is the one place a Core/adapter container split creates a genuine new problem: only whichever container still has BlueZ/D-Bus access can actually run this cleanup — see `ipc-carrier-audit.md` §4.

### 3.9 GPIO/I2C/SPI assumptions — see §2's `hardware/`/`modules/display/` summaries above; full detail in `docker-blockers.md` §4.

### 3.10 Subprocess calls — full itemized table in `docker-blockers.md` §1 (git/self-update, systemd/sudo, privileged hardware/network helpers, the adapter IPC `Popen`, `bluetoothctl`).

### 3.11 Filesystem/systemd assumptions — see `docker-blockers.md` in full; the single-process design (`gunicorn.conf.py`'s hardcoded `workers = 1`, `server.py`'s `fcntl.flock()`-based `_acquire_runtime_lock()`) is the one finding here worth repeating in this document specifically because it's a **dependency-graph-shaped** constraint, not just a deployment detail: it means Core's replica count can never exceed 1 regardless of how the adapter/Core boundary is drawn, because all shared mutable state (`nodes`/`chats`/`messages`/`settings`, every global in `server-decomposition-audit.md` §2) lives in one process's memory with no cross-process synchronization mechanism at all. `fcntl.flock()` only catches a second replica on the *same host filesystem* — it provides zero protection against two replicas on two different hosts/containers.

---

## 4. What this means for the proposed PR sequence

Cross-referencing this dependency map against `server-decomposition-audit.md` §6, the safest-to-riskiest extraction ordering is:

1. **Zero-risk consolidation, no architecture change**: collapse the four parallel `safe_read_json`/`safe_write_json` reimplementations (`server.py`, `storage/device_manager.py`, `camera/camera.py`, and fix `meshsrv/schedule_engine.py`'s missing atomicity) onto `storage/json_store.py`. No behavior change for the common case; removes a confirmed divergence (missing `os.makedirs` in server.py's copy) and closes the one non-atomic JSON writer in the codebase.
2. **Listener-line-parsing extraction** (`server-decomposition-audit.md` §3.4) — pure functions, minimal coupling, already identified as the best `server.py` extraction target independent of this document's findings.
3. **`meshsrv/radio_manager.py`'s HTTP-route layer** — the domain logic is already isolated; only needs a thin `api/api_radio.py` wrapper, matching the DI pattern every other `api/*.py` module already uses. Would also be a natural place to centralize the orphaned `sudo -n systemctl restart` call (§2's `meshsrv/` cross-cutting finding #2) into a proper `service_control.py`, mirroring `network_config.py`'s existing chokepoint pattern.
4. **Test-coverage backfill for untested-but-load-bearing modules** before any further extraction touches them: `meshsrv/schedule_engine.py`/`schedule_actions.py` (unattended, fires real mesh sends, zero tests), `meshsrv/radio_manager.py` (5-state machine, zero tests), `telemetry/telemetry.py` (zero tests despite heavy two-way global coupling with `server.py`).
5. **Camera global-singleton rework** (`camera/camera.py`'s process-global design) is the one item in this audit that's a prerequisite for container decomposition specifically, not just a general code-quality item — it's what makes "run Core in a container with no camera hardware, camera support in a separate container" structurally impossible today without first giving CSI camera support the same instance-scoped design `UsbCameraDriver` already has.

See `docker-blockers.md` for the full Docker/GPL blocker list and `server-decomposition-audit.md` §6 for the complete proposed PR sequence.
