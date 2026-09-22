# Persistent storage inventory (D0)

**Status:** read-only audit, no behavior changed. Built against `origin/main` @ `2adf32a` by grepping `server.py`, `storage/`, `meshsrv/` (incl. `meshsrv/attachments/`), `api/`, `camera/`, `hardware/`, `telemetry/`, `weather/`, `modules/display/`, `system/`, `system_log.py` for every `data/`-relative path, cross-checked against `.gitignore` and the install scripts.

**Purpose:** every file/directory under `data/` that must survive container recreation, with format, ownership, sensitivity, and durability classified for each.

**Root cause of the whole layout:** `server.py` defines `DATA_DIR` (from `config.py`, default `PROJECT_DIR/data`) once at import; every instance-scoped path below is `os.path.join(DATA_DIR, ...)`. `storage/profile_manager.py`'s `ProfileManager` additionally resolves `data/profiles/<8-hex-node-id>/` and hands out per-profile paths (`PROFILE_DATA_DIR`) that `server.py` rebinds its own module-level path variables to once the active radio profile is known.

---

## A. Instance-scoped (survives radio-profile switches, lives directly under `data/`)

| Path | Format | Owner | Sensitive? | Durability |
|---|---|---|---|---|
| `data/instance.json` | JSON, atomic (`safe_read_json`/`safe_write_json`) | `meshsrv/instance_manager.py`:`InstanceManager` | **Yes** — holds the `MC1-XXXX-...` installation identity plus configured radio identity and `active_profile_id`. Not a credential, but a stable install fingerprint — handle like a hostname/serial number. | **Durable, must survive recreation** — losing it mints a new installation ID and forgets which radio profile was active. |
| `data/settings.json` | JSON, atomic | `server.py`:`save_settings()`/`load_settings()` | No secrets (units, listener autorecovery, battery capacity). Weather API keys are **not** stored here — see `weather_secrets.py` below. | Durable user preferences, fully regenerable to defaults if lost. |
| `data/auth.json` | JSON, atomic | `api/api_auth.py`:`load_auth_state()`/`register_auth_routes()` | **Yes — critical.** `{"enabled": bool, "password_hash": str}` (salted `werkzeug.generate_password_hash`). Compromise = full app takeover. Deliberately kept out of `settings.json` so a generic settings save can never wipe it. | Durable — losing it forces every user back through `/setup`; disruptive but not catastrophic. |
| `data/secret_key.txt` | Plain text, 64 hex chars (`secrets.token_hex(32)`), `chmod 0600` | `server.py`:`_load_or_create_secret_key()` | **Yes — critical.** Flask's `app.secret_key`, signs the session cookie carrying `authenticated` state and the CSRF token. Compromise lets an attacker forge authenticated sessions and CSRF tokens. **Must be handled as a credential in any Docker secrets scheme** — never baked into an image layer, never logged. | Durable but silently regenerable — a lost key auto-mints a new one and just invalidates existing sessions (forces re-login, not fatal). |
| `data/runtime.lock` | Plain text (PID), via `flock()` | `server.py`:`_acquire_runtime_lock()` | No. | **Ephemeral** — an OS-level advisory lock for the process's own lifetime; meaningless across a container restart, safe to exclude from a durable volume. |
| `data/cpu_history.json` | JSON, atomic | `system/cpu_history.py` (path passed as an explicit parameter, no module constant) | No. | Regenerable cache (rolling CPU/RAM history graph). |
| `data/update_check.json` | JSON, atomic | `meshsrv/update_service.py`:`configure()` | No. | Regenerable cache of the last update-check result. |
| `data/epaper_config.json` | JSON, custom loader | `modules/display/config_store.py`:`load_epaper_config()` — module docstring explicitly states instance-scoped since the physical e-Paper HAT is wired to this Pi, not to a radio | No. | Durable user config (pins, SPI, rotation), fully regenerable to defaults. |
| `data/hardware_pending.json` | JSON, atomic | `hardware/hardware_config.py`:`_save_pending()`/`_load_pending()`/`_clear_pending()`, reconciled at startup | No. | **Ephemeral / arguably meaningless in a container** — a "reboot required" marker for I2C/RTC Device-Tree-overlay changes; a container recreation isn't a Pi reboot, so this file's whole purpose is worth a design note if this feature is ever containerized. |
| `data/camera_config.json` | JSON, atomic (own reimplementation — see `dependency-map.md`'s consolidation finding) | `camera/camera.py`:`save_camera_config()`/`load_camera_config()` | No. | Regenerable camera video/photo/control settings; instance-scoped because the camera is Pi hardware, not radio-specific. |
| `data/screenshots/` (`YYYY/MM/DD/` subdirs) | Binary JPEG | `camera/camera.py`:`capture_screenshot()`, `cleanup_old_screenshots()` | No (user-generated content — treat like any user photo). | **Durable user content** — actual photos the user took; no other copy exists. Has its own retention policy (`max_mb=500`, `keep_days=30`). |
| `data/system_events.jsonl` | Plain text, JSON-Lines, append-only, manual 1 MiB rotation | `system_log.py`:`log_system_event()` | No (diagnostic detail only). | Regenerable operational log. |
| `data/schedules.json` | JSON — **raw `Path.read_text()`/`write_text()`, NOT atomic, bypasses `storage/json_store.py` entirely** | `meshsrv/schedule_engine.py`:`_load()`/`_save()` | No. | Durable user config (scheduled automation), but see the architecture bug flagged below. |
| `data/mca/attachments.db` | **SQLite** (documented 2nd exception to the JSON convention, ADR-0003) | `meshsrv/attachments/mca_runtime.py` (opens `workspace_manager.mca_dir / "attachments.db"` directly) | **Yes, partially.** Ten tables covering attachments/contacts/deliveries/jobs. `mca_principal` holds **public** Ed25519/X25519 keys and only a `private_key_file` **filename** (never key bytes); `mca_provider_profiles.upload_token_file` similarly stores only a filename pointer. | **Durable** — the MCAttach subsystem's entire transactional state; losing it breaks delivery/history and forces re-establishing key exchange with every contact. |
| `data/mca/<principal-id>/keys/` (dir, mode `0700`) | — | `meshsrv/attachments/workspace.py`:`MCAWorkspaceManager.ensure_workspace()` | Container for the two secret files below. | Durable. |
| `data/mca/<principal-id>/keys/identity_ed25519.seed` | Binary, 32 raw bytes, mode `0600` | `meshsrv/attachments/identity.py`:`create_principal()`/`load_signing_key()` | **Yes — critical secret.** The MCAttach principal's private Ed25519 signing key (X25519 derived at runtime, never stored separately). Module docstring: "never stored in the database, logged, or included in any export." Compromise lets an attacker impersonate this installation's MCA identity. **Handle exactly like a private key in any Docker secrets scheme.** | **Durable and irreplaceable** — no recovery path; losing it breaks attachment crypto for this identity permanently, and regenerating it changes an identity contacts have already trusted. |
| `data/mca/<principal-id>/keys/relay_upload_token_<provider_id>.secret` | Plain text, mode `0600` | `meshsrv/attachments/provider_registry.py`:`set_upload_token()`/`get_upload_token()` | **Yes — secret.** A long-lived bearer token for the configured Relay server (ADR-0008: "never a plaintext DB column"). | Durable but re-obtainable (user can re-enter it) — lower blast radius than the identity seed, but still a live credential. |
| `data/mca/<principal-id>/files/` | Binary (arbitrary attachment content) | `meshsrv/attachments/workspace.py`, `receiver.py`, `service.py` | Potentially sensitive user content (whatever was sent/received), not credentials. | **Durable user content** — must survive recreation. |
| `data/mca/<principal-id>/spool/outgoing/` | Binary (queued outbound attachments) | `meshsrv/attachments/facade.py`, `service.py` | Same as `files/`. | Semi-durable — in-flight queue; losing it mid-transfer drops an outgoing attachment (sender can resend). |
| `data/mca/<principal-id>/cache/incoming/` | Binary (partial-reassembly cache) | `meshsrv/attachments/receiver.py` | Same as above. | **Regenerable/ephemeral** — safe to exclude from a durable volume. |
| `data/mca/<principal-id>/quarantine/` | Binary (failed-validation holding area) | `meshsrv/attachments/receiver.py` | Potentially sensitive quarantined content, no credentials. | Regenerable diagnostic holding area. |

## B. Per-profile-scoped (`data/profiles/<8-hex-node-id>/`)

Resolved exclusively through `storage/profile_manager.py`'s `PROFILE_FILES`/`PROFILE_DIRECTORIES` maps; `server.py` rebinds its own module-level constants (`HISTORY_FILE`, `NODES_FILE`, etc.) to these once the active profile is known.

| Path | Format | Owner | Sensitive? | Durability |
|---|---|---|---|---|
| `data/profiles/<id>/profile.json` | JSON, atomic | `storage/profile_manager.py`:`ensure_profile()`/`get_profile()` | No — radio metadata + migration bookkeeping. | Durable — the profile's own identity record; deleting it orphans the rest of the directory. |
| `data/profiles/<id>/messages.json` | JSON, atomic | `server.py`:`save_messages()`/`load_messages()` | No (message content — no credentials, but arguably sensitive comms data depending on threat model). | **Durable user data** — chat history. |
| `data/profiles/<id>/nodes.json` | JSON, atomic | `server.py`:`save_nodes()`/`load_nodes()` | No. | Durable — known-nodes table (names, positions, last-heard). |
| `data/profiles/<id>/sensors.json` | JSON, atomic | `server.py`:`save_sensors()`/`load_sensors_data()` | No. | Effectively a live cache of the last sensor reading — low loss impact. |
| `data/profiles/<id>/chats.json` | JSON, atomic | `server.py`:`save_chats()`/`load_chats()` | No. | Durable user data (chat list/thread metadata). |
| `data/profiles/<id>/deleted_dm.json` | JSON — **raw `open()`/`json.load`/`json.dump`, NOT atomic** | `server.py`:`ensure_chat()` (reads), `api_delete_all_dm()` (writes), `api_restore_deleted_dm()` (deletes) | No. | Durable but low-value; **flag: inconsistent with the rest of the codebase** — a crash mid-write can corrupt/truncate it, unlike every sibling file in the same directory. |
| `data/profiles/<id>/telemetry_history.json` | JSON, atomic | `telemetry/telemetry.py` — default path set at import, **repointed to the profile path** via `server.py` calling `telemetry.configure_storage(profile_paths["telemetry_history"])` at startup | No. | Durable environment/power telemetry history; loss only degrades historical charts. |
| `data/profiles/<id>/waypoints.db` | **SQLite** (the original, oldest documented exception to the JSON convention, predates MCAttach) | `storage/waypoint_store.py`:`WaypointStore` — created lazily on first radio start | No. | Durable user data (received map waypoints). |
| `data/profiles/<id>/nodes_debug.log` | Plain text, append-only | `server.py` (opened directly around line 1355) | No. | Ephemeral diagnostic log — safe to exclude from a durable volume or truncate freely. |
| `data/profiles/<id>/node_icons/` | Binary PNG (256×256, Pillow-normalized) | `api/api_node_icons.py` | No — user-uploaded custom icons. | **Durable user content** — no other copy exists. |
| `data/profiles/<id>/devices.json` | JSON — own atomic implementation (tempfile + `os.replace`), not `storage/json_store.py` but equivalent in effect | `storage/device_manager.py`:`DeviceManager` | No. | Durable (active camera/sensor assignment); regenerable to defaults if lost, but active-camera selection needs re-picking. |
| `data/profiles/<id>/*.pre_profiles_backup` | Same format as the file it backs up | `storage/profile_manager.py`:`_migrate_legacy()` | No. | One-time migration backup, created only when upgrading from a pre-profile install — irrelevant for a Docker-native install that never had legacy flat files to migrate. |

## C. `.gitignore` cross-check

```
data/
!data/.gitkeep
data/**/*.json
data/**/*.db
data/**/*.txt
```

The entire `data/` tree is excluded except a `.gitkeep` placeholder — a blunt, recursive exclusion, which is why it silently covers `data/mca/**` and `data/profiles/**` without needing per-feature updates, but also means git offers no independent signal about *which* files exist under `data/`; the inventory above came entirely from reading the code, not from `.gitignore` contents.

## D. Install-time seeding

- `install.sh:451` — `mkdir -p "${INSTALL_DIR}/data"` (creates the directory only, seeds nothing).
- `meshcenter-firstboot.sh:693-697` — defensively `rm -f data/instance.json` (clears any incomplete bootstrap identity), `mkdir -p data`, `chown -R` to the target user.
- `meshcenter-firstboot.sh:822-824` — post-start verification that `data/instance.json` was actually created by the running service (fails the install otherwise). No other file is seeded/verified by the installer — everything else is created lazily by the app on first use.

---

## Key findings for the D1+ Docker work

1. **CLAUDE.md's own "Storage conventions" summary is stale.** It names only `data/instance.json`, `data/settings.json`, `data/screenshots/` as instance-scoped, but `auth.json`, `secret_key.txt`, `runtime.lock`, `cpu_history.json`, `update_check.json`, `epaper_config.json`, `hardware_pending.json`, `camera_config.json`, `system_events.jsonl`, `schedules.json`, and the entire `data/mca/` tree are also instance-scoped and are missing from that summary. A Docker volume-mount design built from that doc line alone would miss most of the sensitive/durable files.
2. **Three files need secrets-grade handling**, not just "durable volume": `data/secret_key.txt` (session signing key), `data/auth.json` (password hash), and `data/mca/<principal-id>/keys/identity_ed25519.seed` (MCAttach private key) — plus the lower-tier `relay_upload_token_*.secret` files. None should ever be baked into an image or exposed in a log/support bundle.
3. **Two deviations from the codebase's own conventions**, worth fixing regardless of Docker: `meshsrv/schedule_engine.py`'s `SCHEDULES_FILE = Path('data/schedules.json')` is **CWD-relative, not `DATA_DIR`-derived** — it only lands in the right place today because the default `DATA_DIR` happens to coincide with the process's working directory; a different Docker `WORKDIR`, or a customized `DATA_DIR` in `config.py`, would silently misplace it. And `data/profiles/<id>/deleted_dm.json` is the one JSON file in the whole codebase that bypasses atomic writes entirely.
4. **SQLite is a deliberate, twice-documented exception** to the JSON convention (`waypoints.db`, `attachments.db`) — both need to live on the same durable volume as everything else, but a bind-mount scheme should account for SQLite's WAL/journal sidecar files rather than assuming every file under `data/` is safe to copy while the app is live.
5. **Some files are deliberately ephemeral or lose their meaning outside their original host context**: `runtime.lock` (process-lifetime `flock`), `hardware_pending.json` (a physical-reboot-pending marker that's largely moot once running under Docker — a container recreation isn't a Pi reboot), and `data/mca/<id>/cache/incoming/` (attachment-reassembly cache) can reasonably be excluded from a durable volume or reset on every container start without real data loss.
