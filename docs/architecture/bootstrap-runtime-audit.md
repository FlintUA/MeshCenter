# Bootstrap/Runtime audit (D1.0)

**Status:** read-only audit, no behavior changed. Built on top of `docs/architecture/server-decomposition-audit.md` and `dependency-map.md` (D0, produced against `2adf32a`) — this document does not re-derive what D0 already established; it re-verifies it against the current tree and documents everything that changed since. Produced against `origin/main` @ `cc29897` ("fix(radio): make radio_health_worker() transport-aware...", #286), i.e. immediately after the full Radio Profiles & Connections Model track (#277–#286) landed.

**Method:** `git diff 2adf32a..HEAD -- server.py wsgi.py gunicorn.conf.py meshsrv/ api/ adapters/` read in full (997 insertions / 82 deletions in `server.py` alone, +915 net lines, 6,233 → 7,148), every new/changed function implicated in bootstrap or runtime lifecycle read directly in the current file (not sampled), cross-checked against the D0 tables row-by-row for staleness. Line numbers below are current-tree; they will drift on the next merge — re-grep before relying on them, same caution D0 already gives.

**Scope reminder (from the task brief):** analysis and plan only. No code changed by this document. No Dockerfile work, no env-var migration — those stay explicitly out of scope for D1.0.

---

## 0. What actually changed since D0 (read this first)

D0 was accurate at `2adf32a`. Five things changed since, all from the Radio TCP Transport + Radio Profiles & Connections Model track:

1. **A third transport object at module scope.** `tcp_ipc_transport = AdapterIPCTransport(ConnectionType.TCP, adapter_supervisor)` (server.py:~995, alongside the pre-existing `serial_ipc_transport`/`ble_ipc_transport`) — D0's table 17 (§1) listed two; there are now three, all constructed at import time (object construction only, no connection attempt — this part of D0's finding still holds).
2. **`verify_radio_identity()` is now transport-branching**, not serial-only (server.py:403–453) — see §3 below, this is the biggest single change to the bootstrap sequence's *meaning*, not just its size.
3. **A new function, `restore_active_transport()`** (server.py:6713–6839), called unconditionally near the end of `start_runtime()` — did not exist in D0's tree at all. This is where **invariant #3** (§2.3) lives.
4. **A new function, `seed_nodes_from_transport()`** (server.py:2583–~2650) — the TCP-native equivalent of `parse_nodes_from_info()`, called once from `restore_active_transport()`.
5. **The serial-specific background-thread group is now conditionally gated on `active_transport == "serial"`, not just `identity_match`** (server.py:7025). D0's "up to 12 threads" figure (§4 of that document) is still the right upper bound, but D0 could not have known the true count is now **transport-dependent**: a TCP- or Bluetooth-active radio starts 6 fewer threads than a serial-active one (§2.4 below).

Two new `meshsrv/` modules (`radio_connections.py`, `radio_endpoint.py`, 185 + 246 lines, both pure/stateless/no-I/O, same architectural style D0 already praised for `weather/`) plus `connection_status.py` (58 lines, already covered in this session's earlier PR-2 review) plus a new 960-line `adapters/meshtastic/tcp_transport.py`. None of these introduce new import-time side effects in `server.py` beyond item 1 above — confirmed by reading the diff hunks touching server.py's top-level (lines 1–1030), which show only the `tcp_ipc_transport` construction and the `TransportRouter` callback wiring (`_transport_router_on_log`), nothing else new at module-import scope.

**Net effect on D0's own load-bearing claim** ("`import server` is not side-effect-free... none of steps 1–20 start a background thread or open the serial port"): **still true.** `restore_active_transport()` and the identity-transport-branching both run inside `start_runtime()`, not at import time. `tcp_ipc_transport`'s construction is exactly like the two transports it joins — object construction only, no I/O. D0's Docker/import-time findings (§1 of that document) do not need updating on substance, only on the count of transport objects built.

---

## 1. Dependency map delta (per D1.0's requested module list)

D1.0 asks for a per-directory table covering import side effects / runtime-start side effects / background workers / OS-radio-hardware resources / persistent storage init / route registration deps / mutable globals / shutdown requirements, for `server.py`, `wsgi.py`, `gunicorn.conf.py`, `api/*`, `meshsrv/*`, `camera/*`, `hardware/*`, `storage/*`, `system/*`, `telemetry/*`, `weather/*`, `modules/display/*`.

This table already exists, correctly, in D0 (`server-decomposition-audit.md` §1–§3 for `server.py`; `dependency-map.md` §2 for everything else). Reproducing it here would duplicate ~450 lines of already-accurate content the task brief explicitly says not to redo. What follows is **only the delta**:

| Module | What D0 said | What's true now | Action needed |
|---|---|---|---|
| `server.py` module scope | 20-step side-effect table, steps 1–20 (§1) | Same 20 steps, same ordering, same fatality profile — **plus** the third transport object (item 1 above). No new step, no new failure mode. | None — D0's table stands, add one footnote row for `tcp_ipc_transport`. |
| `server.py::verify_radio_identity()` | "Probes the physical radio once" (D0 §3.1, treated as a single serial-CLI-shaped operation) | **Now three branches**: serial (CLI `--info`, unchanged), TCP (live `tcp_ipc_transport.connect()` + `get_local_node()`, no CLI involved at all), Bluetooth (always `NOT_CHECKED`, no probe at all) — server.py:403–453. | D0's own candidate-module suggestion ("fold into `meshsrv/radio_identity.py`") is **more true now, not less** — the function's real logic (`detect_radio_identity`/`detect_tcp_radio_identity`) already lives there; `server.py`'s copy is a 3-way dispatcher over configured transport plus `INSTANCE_IDENTITY`/`RADIO_IDENTITY_RESULT` global writes. |
| `server.py` background threads (`start_runtime()` step 14, D0 §4) | "identity_match gates six daemon threads" | **Now gated on `identity_match AND active_transport == "serial"`** (server.py:7025) — six threads (`listen_meshtastic`, `cleanup_seen_ids`, `telemetry_worker`, `telemetry_buffer_worker`, `radio_health_worker`, `ack_timeout_worker`) are serial-only. A TCP- or Bluetooth-active MeshCenter process today runs **6 fewer background threads** than a serial-active one. | See §2.4 — this is new runtime-shape variability D0's "up to 12" ceiling didn't anticipate as *conditional on configuration*, only on identity match. |
| `server.py` — new function `restore_active_transport()` | did not exist | server.py:6713–6839, called unconditionally at the end of `start_runtime()`, after the identity-gated thread-start block. Owns invariant #3. | New row for D0's §3 function inventory — candidate module: alongside `verify_radio_identity()`, both belong with `meshsrv/radio_identity.py`/a new bootstrap-owned transport-restore module (§5). |
| `server.py` — new function `seed_nodes_from_transport()` | did not exist | server.py:2583–~2650, same shape/risk profile as `parse_nodes_from_info()` (D0 §3.6) — writes `nodes` under `state_lock`, called once, non-atomic w.r.t. the listener's own writes only by virtue of being called before any listener thread starts for a non-serial transport. | Same candidate module as `parse_nodes_from_info()` (D0's "storage/node_repository.py" suggestion, §3.6). |
| `meshsrv/radio_connections.py`, `radio_endpoint.py` | did not exist | Both pure functions, `Mapping in → dict out`, zero I/O, zero `meshtastic` import — same clean shape D0 already flagged `weather/` and the listener-line-parsers as the reference pattern for. | None — these are already what D0 was asking future modules to look like. |
| `adapters/meshtastic/tcp_transport.py` | did not exist | 960 lines, isolated to `adapters/meshtastic/` (GPLv3 boundary respected — confirmed by the same grep sweep D0's dependency-map.md §1.2 already ran, re-run for this audit: zero new hits of `import adapters` outside `adapters/meshtastic/` or test files). | None — boundary intact. |
| `wsgi.py` | not separately detailed in D0 (folded into "start_runtime() sequencing") | Read in full for this audit (44 lines) — unchanged since D0: `logging.basicConfig()` then `from server import app, start_runtime` then `start_runtime()` at import time, single-worker rationale documented inline. | None — still accurate, still the entire "how does the WSGI process actually boot" story in one file. |
| `gunicorn.conf.py` | same | Read in full (76 lines) — unchanged: `workers = 1` (hard architectural requirement, not tunable), `worker_class = "gthread"`, `threads = 8`, `timeout = 120`, deliberately no `max_requests` recycling. | None. |
| Everything else (`api/*`, `hardware/*`, `storage/*`, `system/*`, `telemetry/*`, `weather/*`, `modules/display/*`, `camera/*`) | full tables in `dependency-map.md` §2 | **No changes found** in this track's diff outside `api/api_meshtastic.py` (+514/-… lines — transport-switch routes, already covered by this session's own PR 1/2/3a-c reviews, not a bootstrap concern) and `api/api_settings.py` (+27 lines — `meshtastic` settings section, same). Neither touches import-time side effects or route-registration ordering. | None — D0's tables for these directories stand unmodified. |

**Confirms D0's headline finding #1 still holds:** re-ran the "does anything import `server`" grep sweep for this audit — still zero, across the whole new TCP-transport code too. `adapters/meshtastic/tcp_transport.py`/`fake_radio_server.py` (new) import only `meshsrv.*`/stdlib, never `server`.

---

## 2. Three invariants that must not break in D1.1+

D1.0 asks these to be explicitly flagged. All three are real, all three are already load-bearing in the current `start_runtime()`, and all three have their own in-code comments citing the specific incident/review that produced them — not invented for this document.

### 2.1 AttachmentsService init before the serial listener starts

**Where:** server.py:6986–7013 (comment) / 7010–7013 (the call) / 7025–7042 (the listener thread group, which comes *after*).

**What:** `mca_runtime.start_attachments_service(DATA_DIR, transport_router, MCA_CONTROL_CHANNEL_INDEX)` must complete (or at least be attempted — it's wrapped in its own best-effort `try/except`) **before** `threading.Thread(target=listen_meshtastic, ...).start()` ever runs. The in-code comment cites this explicitly: *"PR #231 review (2nd pass), requirement 1... handle_incoming_meshtastic_text() (called from the listener thread) is only safe to be lock-free/init-free... because by the time it can ever run, mca_runtime's singleton state and the queue it enqueues onto already exist."*

**Why it's real, not decorative:** if the ordering were ever flipped (listener starts first), an inbound `MCA1:`-prefixed DM could arrive and reach `_handle_listener_line()` → `mca_runtime.handle_incoming_meshtastic_text()` before the AttachmentsService's bounded inbound queue exists — the exact race the PR #231 review caught.

**Deliberately NOT gated on `identity_match`** (unlike the listener group right after it) — a mismatched/unresolved radio identity blocks the listener from ever starting at all, but AttachmentsService's own job (resuming previously-drafted SENT-side work, reconciling interrupted RECEIVED-side work) has nothing to do with whether today's radio identity question is resolved.

**For D1.1+:** any bootstrap-decomposition PR that reorders `start_runtime()`'s steps must preserve `start_attachments_service(...)` strictly before every one of the six serial-conditional thread starts — not just `listen_meshtastic` specifically, since `cleanup_seen_ids`/`telemetry_worker`/etc. don't feed the listener callback but keeping the whole group atomic relative to AttachmentsService init avoids re-deriving this ordering constraint per-thread later.

### 2.2 Identity mismatch gate — never write into the active profile

**Where:** two independent code paths, both gated the same way, confirmed to have grown to cover TCP as well as serial in this track:

- **Serial:** server.py:6888–6894 — `if identity_match and active_transport == "serial": parse_nodes_from_info(...)` else prints `"[PROFILE] Radio writes blocked for profile {ACTIVE_PROFILE_ID}: identity={identity_status}"` and does nothing.
- **TCP:** server.py:6764–6771, inside `restore_active_transport()` — `if active_transport == "tcp" and not identity_match: ...return` (refuses to connect/seed at all), with an explicit comment: *"a TCP identity probe that just found MISMATCH/NOT_FOUND at this exact endpoint must not be immediately followed by this function connecting to it anyway and seeding its nodes into the accepted profile — that would silently contaminate profile data with an unverified radio's NodeDB."*
- **Bluetooth is the one deliberate exception**, not an oversight: `verify_radio_identity()` always returns `NOT_CHECKED` for Bluetooth (no verification mechanism exists for it at all — server.py:432–448), so `identity_match` is always `False` for a Bluetooth-configured radio. Gating Bluetooth restore on `identity_match` the same way as TCP would mean **Bluetooth could never restore on startup at all** — so `restore_active_transport()` restores Bluetooth unconditionally, matching Bluetooth's pre-existing accepted lack-of-verification everywhere else in the codebase (radio-identity's own `NOT_CHECKED` state has always meant "proceed cautiously, not blocked" for BLE — this is consistent with that, not a new carve-out invented for TCP).

**For D1.1+:** this is now a **three-way gate**, not a single `if identity_match:` check reusable verbatim across transports — a decomposition that extracts identity verification into its own module must preserve the transport-specific difference (serial and TCP both hard-gate on match, Bluetooth structurally cannot and doesn't), not collapse it into one shared boolean.

### 2.3 TCP startup reuse — no second connect

**Where:** server.py:6775–6821, inside `restore_active_transport()`, extensively commented in-place as *"TCP DOUBLE-CONNECT FIX (follow-up investigation after PR #279)"*.

**What:** `verify_radio_identity()`'s TCP branch (§0 item 2) doesn't just *check* identity — `detect_tcp_radio_identity()` performs a real `transport.connect(descriptor, timeout=25)` and **deliberately does not close the connection on success** (`meshsrv/radio_identity.py:361–365`, its own docstring: *"identity detection for TCP unavoidably *is* the same connect a caller may want to keep using afterward... the caller is responsible for deciding whether to disconnect()"*). `restore_active_transport()` is that caller: it reads `tcp_ipc_transport.get_connection_info()` (a free, local cache read — no IPC round-trip), and if the cached state is already `CONNECTED` with a descriptor address matching the endpoint being restored, it reuses that exact connection (`restore_connect_new = lambda: tcp_ipc_transport`) instead of calling `build_transport_connect_new(...)`'s unconditional `force=True` reconnect path.

**Why it's real:** the in-code comment is explicit that the earlier (pre-fix) behavior "would tear down a perfectly good, seconds-old link and redo the entire handshake for no reason — doubling the number of TCP connect operations on every single boot, back-to-back, each one carrying the same real risk of hitting the underlying library's own handshake instability (confirmed live — see PR #279 and its own follow-up investigation)." This is a **live, previously-observed regression**, not a theoretical concern.

**For D1.1+:** any refactor that separates "verify identity" from "restore the active transport" into two independently-callable units (a natural-looking decomposition) must preserve this specific coupling — the TCP identity check and the TCP startup connect are **the same operation observed twice**, not two operations that happen to agree. A naive split (call identity-check as a pure function, then unconditionally call connect separately) reintroduces the exact bug PR #279's follow-up fixed. Whatever `AppContext`/bootstrap module ends up owning this needs to pass the *already-connected transport handle* from the identity step into the restore step, not just a boolean/status.

### 2.4 (New observation, not a fourth invariant, but adjacent) — background-thread count is now transport-dependent

Not explicitly requested as an invariant by the brief, but directly relevant to D1.4 (§8) and worth stating precisely since D0 couldn't have known it: **the shape of "what's running after `start_runtime()` returns" now depends on `accepted_radio["transport"]`, not just on identity match.** A serial-active, identity-matched MeshCenter runs all 12 of D0's threads. A TCP-active, identity-matched MeshCenter runs 6 (`cpu_history_worker`, `update_service.check_worker`, `time_service`'s own, `installation_time_assignment`'s own, `schedule_engine`'s own, plus `epaper_worker` if `EPAPER_ENABLED`) — the six serial-conditional ones (`listen_meshtastic` and its five listener-dependent siblings) never start at all, by design (§0 item 5). Any D1.4 stop()-path design (§8) needs to be correct for **both** shapes, not assume all 12 are always present.

---

## 3. What actually requires the Meshtastic CLI now

D1.0 explicitly asks this to be documented, not acted on (the fatal check at server.py:217–223 stays as-is either way — this section is analysis for a future decision, not a recommendation to remove it now).

**Genuinely CLI-dependent (serial-listener/info-path), confirmed by reading every call site of `MESHTASTIC_CMD`/`meshtastic_transport.get_info()`:**
- `verify_radio_identity()`'s serial branch: `detect_radio_identity(MESHTASTIC_CMD, MESHTASTIC_PORT, timeout=25)` (server.py:431) — shells out to `meshtastic --port <port> --info`.
- `listen_meshtastic()`/`run_listener()` (`meshsrv/serial_port_supervisor.py`) — the long-lived `meshtastic --listen` subprocess, Core's own (per CLAUDE.md's "Stage A" framing) — unconditionally serial-only, no TCP/BLE equivalent exists or is planned short-term.
- `adapters/meshtastic/serial_transport.py::get_info()` (line 558) — the **adapter's own** CLI shell-out, used by `SerialTransport` for `--info` re-probes; this is why `ipc_server.py`'s `--meshtastic-cli` argument exists at all (`argparse` `required=True`, `adapters/meshtastic/ipc_server.py:317`).

**Genuinely CLI-independent (TCP adapter path), confirmed the same way:**
- `verify_radio_identity()`'s TCP branch — `detect_tcp_radio_identity()` never touches `MESHTASTIC_CMD`; it's a pure `RadioTransport.connect()` + `get_local_node()` round-trip through `tcp_ipc_transport` (§2.3).
- `restore_active_transport()`'s TCP path, `seed_nodes_from_transport()` — both operate purely through `RadioTransport`'s structured API (`get_nodes()`, `get_connection_info()`), no CLI text-parsing anywhere.
- `adapters/meshtastic/tcp_transport.py` — 960 lines, grepped for `cli_path`/`subprocess`: zero hits. TCPTransport talks to the radio via the `meshtastic` **Python library's** `TCPInterface`, not the CLI binary, at all.

**The concrete finding (new, not in D0):** `resolve_meshtastic_cli()`'s fatal check (server.py:217–223, `raise SystemExit(1)` if no CLI binary is resolvable anywhere — comment: *"nothing radio-related can ever work without it, not just serial"*) is now **stricter than what's structurally required for a TCP-only-configured deployment.** For a radio whose accepted `transport` is `"tcp"`:
- Core's own identity check never calls the CLI (confirmed above).
- The listener never starts (gated on `active_transport == "serial"`, §0 item 5) — so `MESHTASTIC_CMD` is never used by Core directly at runtime.
- The **adapter subprocess** still requires *a* string for `--meshtastic-cli` to launch at all (`argparse required=True`) — but `SerialTransport.get_info()` (the only consumer of that value, `adapters/meshtastic/serial_transport.py:558`) is never called unless a `SerialTransport.connect()`/re-probe actually happens, which a TCP-only deployment never triggers.

So the comment's claim ("not just serial") is **no longer accurate** for the TCP path specifically — it was true when D0/pre-TCP-track server.py was written (the CLI genuinely was the only path to anything radio-related), and is stale now that a second, CLI-free path exists. This is exactly the kind of comment-vs-code drift this audit is meant to surface. **Per the task brief, this fatal check is not being removed or loosened here** — that's a real decision with real consequences (a Docker Core image not bundling the adapter's CLI venv at all becomes structurally possible only for TCP-only deployments, never for serial ones, which is a meaningfully different Docker story than "just relax the check") that belongs to whichever phase actually tackles the Docker/GPL blocker work D0's `docker-blockers.md` already scoped, not to D1.0.

---

## 4. `scripts/check_startup_calls.py` — canonical target evaluation

Read in full (77 lines, unchanged since D0's audit — no commits touched it in the TCP track). Its `REQUIRED_CALLS` list currently checks for these literal substrings existing anywhere in `server.py`'s text: `start_time_service()`, `start_schedule_engine(`, `register_camera_routes(`, `register_camera_manager_routes(`, `register_chat_routes(`, `register_settings_routes(`, `listen_meshtastic`.

**Two concrete gaps found, both directly relevant to this track and to D1.1+ decomposition risk, neither previously flagged:**

1. **It does not check for `start_attachments_service(` or `restore_active_transport(` at all.** Given §2.1's invariant is exactly the class of regression this script exists to catch ("a whole-file deploy... silently deleted several startup calls... nothing raised an exception"), the AttachmentsService-before-listener ordering and the TCP-restore call are two of the three invariants this very audit was asked to flag, and **the one automated safeguard that exists for exactly this failure mode doesn't cover either of them.** A decomposition PR that accidentally drops or misorders either call would pass `check_startup_calls.py` cleanly.
2. **`'listen_meshtastic'` as a bare substring match doesn't verify it's actually reachable.** The check only confirms the string exists somewhere in the file — it already couldn't distinguish "called unconditionally" from "called behind a condition that's never true" even before this track (this is inherent to a static-text check, not a new gap), but the ADD of the `active_transport == "serial"` condition (§0 item 5) makes the gap concretely relevant now: a hypothetical bug that flipped the condition to `active_transport != "serial"` (listener starts for TCP, never for serial) would still pass this check, since the substring `listen_meshtastic` is still present in the file.

**One more thing found while checking this, not a gap but directly relevant:** `tests/test_server_mca_startup_wiring.py` already exists and already enforces invariant §2.1 far more rigorously than `check_startup_calls.py` could — it's an `ast`-based test (`test_start_attachments_service_completes_before_listen_meshtastic_starts()`) that parses `start_runtime`'s actual source statements and asserts the `start_attachments_service(...)` call's top-level statement index is strictly less than the `threading.Thread(target=listen_meshtastic, ...)` call's index. This is real, load-bearing, source-order-verifying coverage for exactly the ordering §2.1 describes — **already present, not something D1.1+ needs to add.** It does, however, sharpen this section's structural constraint rather than replace it: this AST test walks `start_runtime.body` as *one function's top-level statements* — exactly the same "must stay one function, one file, visible call sequence" shape `check_startup_calls.py`'s canonical-target concern already requires (below). If D1.5 moves this body into `Runtime.start()`, **both** this AST test and `check_startup_calls.py` need updating in the same PR to point at the new function/file — and if the sequence is ever split across multiple functions (violating that shape), this AST test would either need a rewrite to still mean anything, or silently stop testing the real invariant if not updated in lockstep. Two independent safeguards depending on the same structural property makes that property more important to preserve deliberately, not less.

**Recommendation for D1.1+ (not applied by this audit — analysis only):**
- Add two entries to `REQUIRED_CALLS`: `'start_attachments_service('` (breaks: inbound MCA attachments silently stop being processed, in-flight transfers never resume after a restart) and `'restore_active_transport('` (breaks: TCP/Bluetooth radios silently stop reconnecting after every restart, reverting to the pre-#278 manual-reselect-in-Settings behavior).
- **Canonical target after decomposition:** whatever single function ends up being "the thing `wsgi.py` calls to bring the runtime up" — today that's `start_runtime()` in `server.py`; if D1's bootstrap work produces a `meshsrv/runtime.py::start()` or an `AppContext.start()` (§6), **that new function's source file becomes the new `SERVER_PY` target**, not `server.py`. The check's whole design (grep a known-good set of substrings out of one file) only works if that one file is still where every startup call textually appears — a decomposition that splits `start_runtime()`'s body across multiple functions in multiple files (rather than one function that still calls out to helpers, all from one file) would silently defeat this safeguard **even with the entries updated**, since the calls would no longer all be textually present in the one file the script reads. This is a **structural constraint on how D1's runtime-startup decomposition should be shaped**, not just a script-maintenance footnote: keep `start()`'s own body as one function making a visible sequence of calls (which is exactly `start_runtime()`'s current shape), even if the callees move to other modules.

---

## 5. Proposed module boundaries

D1.0 asks for candidate module boundaries, explicitly not for creating a directory "for prettiness." Building on D0's own candidate-module suggestions (`server-decomposition-audit.md` §3, §6) plus what this audit found:

- **`meshsrv/bootstrap.py`** (new) — owns exactly what D0 called "the actual D1 bootstrap-decomposition scope" (its own §1 conclusion, reconfirmed accurate in §0 of this document): the import-time steps that mutate disk / can abort the process (`resolve_meshtastic_cli`, `resolve_serial_port`, `InstanceManager.load_or_create`, `ProfileManager.ensure_profile`, secret-key load/create, auth-state load/create). This is D0's steps 4/8/9/13/14 (§1 table), the ones explicitly flagged as "a serious concern for any test or container context that imports `server` without meaning to touch real profile data." Candidate signature: a `BootstrapResult` dataclass (paths, `INSTANCE_IDENTITY`, `PROFILE_CONTEXT`, `MESHTASTIC_CMD`) rather than the current pattern of ~15 separate module globals.
- **`meshsrv/runtime.py`** (new) — owns `start_runtime()`'s *sequencing* (§1 of D0, §0/§2 of this document) as one visible top-level function calling out to already-existing owners (`verify_radio_identity`, `restore_active_transport`, the six-thread group, the always-on thread group, `mca_runtime.start_attachments_service`) — **not** a rewrite of any of those owners, just the composition. This is the file `check_startup_calls.py` should point at after decomposition (§4).
- **`meshsrv/radio_identity.py`** (existing, extend) — `verify_radio_identity()`'s server.py wrapper (§0 item 2, §2.2, §2.3) is now a 3-way dispatcher whose actual logic (`detect_radio_identity`/`detect_tcp_radio_identity`/`compare_radio_identity`) already lives here. Moving the dispatcher itself here (parameterized on `INSTANCE_IDENTITY`/`RADIO_IDENTITY_RESULT` rather than reading/writing server.py globals directly) is a more natural fit now than it was at D0 time, not a new idea — D0 already suggested this fold (§3.1 table), this audit just confirms the code grew in exactly the direction that makes the fold more valuable, not less.
- **`meshsrv/transport_restore.py`** (new, or fold into the above) — `restore_active_transport()`/`seed_nodes_from_transport()` as a pair, since §2.3's invariant means they can't be usefully separated from each other or from the identity-check step that feeds them (the connection-reuse coupling is the whole point).
- **`app_context.py` — deliberately NOT proposed as `meshsrv/app_context.py`.** Given `server.py`'s ~150-function inventory (D0 §3) and the ~50+ shared mutable globals (D0 §2) that dozens of `api/*.py` modules already receive via DI-by-parameter-list, an `AppContext` object is better placed at the repo root (`app_context.py`, sibling to `server.py`/`wsgi.py`) or as `meshsrv/app_context.py` if the team wants it under the existing package — **this is a naming/location bikeshed the audit is flagging as a decision to make explicitly in the D1.1 kickoff, not resolving here**, since it doesn't change the actual technical content of §6.

**Not proposed:** a `meshsrv/listen/` package for the listener-line-parsers (D0's own top pick, §3.4/§6) — still the single best extraction target in the codebase by D0's own analysis, but it's a **state/parsing decomposition**, not a **bootstrap/runtime lifecycle** one — out of D1.0's actual scope (bootstrap/runtime, per the task brief's own framing) even though it would make an excellent D-series phase on its own.

---

## 6. Proposed target structure — `AppConfig` / `AppContext` / `create_app()` / runtime lifecycle

**Explicitly approximate, not final**, per the task brief. Sketched against what `server.py`/`wsgi.py` actually do today (§0, §1), not a from-scratch redesign.

```python
# app_config.py (new) — replaces server.py's `from config import *` +
# resolve_app_version()/resolve_meshtastic_cli()/resolve_serial_port()
# (D0 §1 rows 2-5). Pure data + pure resolution functions, no I/O beyond
# reading config.py/shelling to `git describe`/probing for the CLI binary
# - i.e. exactly what those functions already do, just returning a
# dataclass instead of setting module globals.
@dataclass
class AppConfig:
    app_host: str
    app_port: int
    data_dir: str
    meshtastic_cmd: str          # resolve_meshtastic_cli() result
    meshtastic_port: str          # resolve_serial_port() result
    app_version: str              # resolve_app_version() result
    # ... every other `from config import *` name server.py currently
    # relies on as a bare module global (see config.example.py for the
    # full authoritative list CLAUDE.md already points to)

def load_app_config() -> AppConfig: ...


# app_context.py (new) — replaces server.py's ~803-1017 block (D0 §1 row
# 17-19): state_lock/radio_lock/messages/nodes/chats/settings/
# pause_listen, listener_supervisor, adapter_supervisor, the three
# *_ipc_transport objects, transport_router, radio_connection_manager.
# Object CONSTRUCTION only - no I/O, no thread starts, matching what
# D0/this audit confirmed is already true of this exact code today.
@dataclass
class AppContext:
    config: AppConfig
    state_lock: threading.Lock
    radio_lock: threading.Lock
    messages: list
    nodes: dict
    chats: dict
    settings: dict
    pause_listen: threading.Event
    listener_supervisor: SerialPortSupervisor
    adapter_supervisor: AdapterSupervisor
    serial_ipc_transport: AdapterIPCTransport
    ble_ipc_transport: AdapterIPCTransport
    tcp_ipc_transport: AdapterIPCTransport
    transport_router: TransportRouter
    radio_connection_manager: RadioConnectionManager
    instance_manager: InstanceManager
    # ... the rest of D0 §2's global inventory, grouped the same way

def build_app_context(config: AppConfig) -> AppContext: ...


# server.py (shrinks) — create_app() replaces the current "construct app,
# call every register_*_routes(...)" block (D0 §1 rows 11, 15, 19-20;
# ~19 register_*_routes calls total across both locations). Still the
# DI-by-parameter-list pattern every api/*.py module already expects -
# create_app() is just what CALLS them, in one place, instead of that
# being interleaved with bootstrap.
def create_app(ctx: AppContext) -> Flask:
    app = Flask(__name__)
    register_camera_routes(app, ctx.state_lock, ...)
    register_chat_routes(app, ctx.state_lock, ctx.chats, ctx.nodes, ...)
    # ... every existing register_*_routes call, unchanged signatures
    return app


# meshsrv/runtime.py (new) — start_runtime()'s CURRENT body, moved
# essentially as-is (see §5's explicit "composition, not rewrite" framing
# and §4's canonical-check-startup-calls-target constraint: this needs to
# stay ONE function with a visible top-to-bottom call sequence).
class Runtime:
    def __init__(self, ctx: AppContext):
        self._ctx = ctx
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        # _acquire_runtime_lock(), verify_radio_identity(), load_*(),
        # mca_runtime.start_attachments_service() BEFORE the serial
        # thread group (invariant §2.1), the identity/transport-gated
        # thread-start block (invariants §2.2/§2.4), restore_active_
        # transport() (invariant §2.3), the always-on thread group,
        # e-paper - same order as today's start_runtime(), same gating
        # conditions, just tracking what it started in self._threads
        # instead of firing daemon threads into the void.
        ...

    def stop(self) -> None:
        # D1.4's own concern - see §8. NOT sketched further here per
        # the task brief's explicit framing of D1.4 as a qualitatively
        # different, non-behavior-preserving risk from the rest of D1.


# wsgi.py (shrinks to)
config = load_app_config()
ctx = build_app_context(config)
app = create_app(ctx)
runtime = Runtime(ctx)
runtime.start()
```

**What this sketch deliberately does NOT change:** the DI-by-parameter-list pattern (`register_*_routes(app, state_lock, ...)`) every `api/*.py` module already uses stays exactly as-is — `AppContext` is a *container* those calls unpack into the same parameter lists, not a replacement for the pattern itself (an `AppContext`-as-single-mega-parameter refactor of all 16 `api/*.py` modules' signatures would be its own, much larger, much riskier phase — explicitly not proposed here).

---

## 7. Does D1.1–D1.5 need re-splitting, given the real scope found?

**Yes — the same pattern that made PR 3 split into 3a/3b/3c is already visible here, for the same underlying reason: `server.py` grew 915 lines / 15% in the time between D0 and this audit, entirely from one feature track, and D1's own scope (bootstrap + runtime lifecycle) touches a *wider* cross-section of that file than PR 3 did.**

Originally-implied D1.1–D1.5 (inferred from this task's own framing — "AppConfig/AppContext/create_app()/Runtime lifecycle... D1.4 (runtime.stop())... D1.5" — five phases) is too coarse for at least two reasons found by this audit specifically:

1. **§2's three invariants are not evenly distributed across one clean "bootstrap" vs. "runtime" split.** `restore_active_transport()` (invariant §2.3) sits structurally between "load persisted state" and "start background threads" — it's neither pure bootstrap (it does live I/O, a real connect) nor pure runtime-lifecycle (it's not a long-running worker, it runs once and returns). A single D1.2 ("AppContext") or D1.3 ("create_app()") phase would have to make an arbitrary call about which one owns it, and get it wrong in exactly the way that produces a second PR-3-style mid-flight split.
2. **§4's `check_startup_calls.py` finding is a hard structural constraint** on how the runtime-sequencing phase can be shaped (one function, one file, visible call sequence) — this needs to be a named, explicit sub-goal of whichever phase does `meshsrv/runtime.py`, not an incidental detail discovered mid-PR.

**Proposed revised split** (mirrors the PR-3a/3b/3c experience: narrower phases, each independently reviewable, each behavior-preserving except the explicitly-flagged one):

| Phase | Scope | Behavior-preserving? | Depends on |
|---|---|---|---|
| **D1.1** | `AppConfig` only — `app_config.py`, `load_app_config()`, replaces `from config import *` + the three `resolve_*()` calls (D0 §1 rows 2-5). Smallest possible first slice; establishes the dataclass-over-module-globals pattern this whole track relies on. | Yes | none |
| **D1.2** | `AppContext` construction only — `app_context.py`, `build_app_context()`, replaces D0 §1 rows 8-9, 17-19 (identity/profile load, state_lock/globals, three transports, `transport_router`). Explicitly does NOT touch `verify_radio_identity()`/`restore_active_transport()` — those move in D1.3. | Yes | D1.1 |
| **D1.3** | Identity + transport-restore as one unit — `verify_radio_identity()` + `restore_active_transport()` + `seed_nodes_from_transport()` move together into `meshsrv/radio_identity.py`/a new sibling, preserving invariants §2.2/§2.3 as one atomic change (per this section's point 1). This is the phase most likely to need its own a/b split if it turns out larger in practice than this audit's read-through suggests — flag for re-estimation once D1.2 is actually merged and the real diff size is known, same "re-split if PR3-sized" caution this whole section is arguing for. | Yes | D1.2 |
| **D1.4** | `create_app()` — the ~19 `register_*_routes(...)` calls, moved as pure relocation (no signature changes to any `api/*.py` module). | Yes | D1.3 |
| **D1.5** | `meshsrv/runtime.py::Runtime.start()` — `start_runtime()`'s body, moved as one function preserving its exact call sequence (§2.1/§2.4, §4's structural constraint). Includes updating `check_startup_calls.py`'s target + adding the two missing entries (§4). | Yes | D1.4 |
| **D1.6** (renumbered from the original D1.4) | `Runtime.stop()` — genuinely new behavior, not a move. See §8 in full; this is correctly flagged by the task brief as qualitatively different and should stay its own phase, now sixth instead of fourth given the split above. | **No — new behavior** | D1.5 |

This adds one phase (six instead of five) but keeps every phase except D1.3/D1.6 to a single, narrow, mechanical concern — the same trade researching PR 3a/3b/3c already validated works well for this codebase's review cadence. D1.3 is flagged in the table itself as the one most likely to need a further split once its real size is known.

---

## 8. D1.4/D1.6 — `Runtime.stop()` is a different kind of risk, not a refactor

Per the task brief's own explicit instruction, this needs to be called out separately: **every phase in §7's table except this one is a behavior-preserving move.** This one is not — nothing in the current codebase calls `stop()` on anything runtime-owned today (D0 already found this: *"Shutdown path: there isn't an explicit one. No `atexit`/signal handler was found registered anywhere in `server.py`"*). Building `Runtime.stop()` means **designing new behavior**, not extracting existing behavior, and needs to be scoped/reviewed accordingly — not folded into a "just move the code" phase.

**Inventory: which services already have a stop/close path, and which would need one invented, confirmed by direct grep + read for this audit:**

| Service | Owns a thread/subprocess? | Has `stop()`/`close()` today? | What D1.6 needs to do |
|---|---|---|---|
| `AttachmentsService` (`meshsrv/attachments/service.py`) | Yes (its own worker) | **Yes** — `stop(self, *, timeout: Optional[float] = 5.0) -> bool` (line 503), already graceful with a timeout | Call it. Nothing to invent. |
| `TransportRouter` (`meshsrv/transport_router.py`) | No thread of its own, wraps the active transport | **Yes** — `close(self, *args, timeout: float = 15.0, **kwargs)` (line 230) | Call it. |
| `AdapterSupervisor` (`meshsrv/adapter_ipc_client.py`) | Yes (the adapter subprocess) | **Yes** — `close(self) -> None` (line 805) | Call it — and call it **after** `TransportRouter.close()`, not before (the router's close needs the adapter alive to send a clean disconnect). |
| `SerialPortSupervisor` / `listen_meshtastic` (the `--listen` subprocess) | Yes (a real OS subprocess, Core's own, per CLAUDE.md's "Stage A") | **Yes** — `stop_listener_process()` (line 295), already what `stop_listener()` in server.py delegates to | Call it. |
| `DisplayManager` (`modules/display/manager.py`) | Yes (its own worker) | **Yes** — `stop(self) -> None` (line 153) | Call it, only if `EPAPER_ENABLED` and it was actually started. |
| `schedule_engine.py`'s ticker thread | Yes | **No** — no stop function found anywhere in the module | **Needs inventing**, or explicitly documented as process-lifetime-scoped (see below). |
| `time_service.py`'s background thread | Yes | **No** | Same. |
| `update_service.py`'s `check_worker` | Yes | **No** | Same. |
| `installation_time_assignment.py`'s thread | Yes, but **self-terminating** — per CLAUDE.md, it "polls... until NTP is confirmed... assigns... exactly once and exits." | N/A — it's not a persistent worker in the first place | Not a gap — this one is correctly self-terminating already, no stop() needed by design. |
| `cpu_history_worker`, `cleanup_seen_ids`, `telemetry_worker`, `telemetry_buffer_worker`, `radio_health_worker`, `ack_timeout_worker`, `epaper_worker` (all plain `while True: time.sleep(N)` daemon threads defined directly in `server.py`/`modules/display/service.py`) | Yes, all seven | **No** — none of these are methods on any class; they're bare functions passed to `threading.Thread(target=...)` | **The real design problem D1.6 has to solve**, not a checklist item — see below. |

**The actual decision D1.6 needs to make, stated plainly (not solved here — D1.0 is analysis only):** the seven bare-function daemon threads have no natural `.stop()` to call because they were never built as objects with lifecycle in the first place — they're `while True: time.sleep(N): <do work>` loops. There are exactly two honest options, and the task brief already names the one to avoid:

1. **Convert each to check a `threading.Event` (a `_shutdown` flag) at the top of its loop and exit cleanly when set**, with `Runtime.stop()` setting the event and `join()`-ing each thread with a bounded timeout. This is real, additive work per-thread (seven small, mechanical, but non-zero changes, each needing its own review for "does this loop have a blocking call inside it that `Event.is_set()` won't interrupt mid-sleep" — several of these `time.sleep(30)`-style loops would need `Event.wait(30)` instead of `time.sleep(30)` specifically so a shutdown doesn't have to wait out a stale sleep).
2. **Explicitly document them as process-lifetime-scoped** — `Runtime.stop()` does not attempt to stop them at all; process exit (SIGTERM → gunicorn worker exit → process death) kills them as daemon threads, exactly as happens today. This is the **do not invent unsafe thread termination** option the task brief is steering toward, and matches D0's own finding that this is already, today, how the process actually behaves (`KillMode=control-group` in `deploy/meshcenter.service`, `PR_SET_PDEATHSIG` for the direct-run case — both already documented in CLAUDE.md as the existing orphan-protection mechanism, at the OS/process level, not the application level).

**This audit's recommendation for D1.6 to evaluate (not decide — that's D1.6's own review, not D1.0's):** option 2 for all seven, **explicitly documented as such** (a docstring/comment on `Runtime.stop()` naming exactly which threads it does not attempt to join and why), reserving option 1 only for threads that hold an external resource needing clean release beyond "the process died" — and by the inventory above, **none of the seven bare-function threads hold such a resource**: they read already-shared in-memory state (`nodes`/`radio_health`/etc.) or make already-idempotent I/O calls (CPU history append, a GitHub API check), none of them own a subprocess, socket, or file handle that needs explicit closing the way `AdapterSupervisor`/`SerialPortSupervisor`/`TransportRouter` (all already covered by real `stop()`/`close()` methods) do. This makes option 2 a defensible, not just expedient, choice for these seven specifically — worth stating as the working hypothesis for D1.6 to confirm or refute, not as this document's own decision.

---

## 9. Migration plan + test plan (draft — detail belongs to each phase)

**Migration plan (per §7's revised phase table):**
- Each phase D1.1–D1.5 is a pure code-motion + signature-preserving change: old module-level globals become attributes read off `AppConfig`/`AppContext` instances threaded through the same functions that used to read them as globals. `tests/conftest.py`'s `server_module` fixture (which imports `server.py` wholesale against a synthetic config/data dir — D0 §5) is the thing every phase's tests run against; it should need **zero changes** through D1.5 as long as `server.py` keeps re-exporting the same names (`app`, `start_runtime`, `INSTANCE_IDENTITY`, etc. — D0 §5's own list of 15 test files' cross-references) even after their real definition moves elsewhere — a thin `from meshsrv.runtime import Runtime; ...` re-export shim in `server.py`, kept deliberately through D1.5, avoids a 15-file test-fixture rewrite happening in the same PR as the actual code move.
- D1.6 (`Runtime.stop()`) is the one phase that needs a **new** fixture/harness, not a preserved one — testing "does the runtime actually stop" needs a way to start a real `Runtime` instance in-process (not the module-import-time pattern `server_module` uses) and assert on thread/subprocess state after `stop()` returns, which is a materially different test shape than anything in the current suite.
- Rollback per phase: since D1.1–D1.5 are pure moves, each is independently revertable via a plain `git revert` without touching later phases' code (they only ever consume the new module's public function/dataclass, never its internals) — the same "safe to revert PR-by-PR" property D0's own proposed sequencing already relied on for its consolidation-first ordering.

**Test plan (draft):**
- **D1.1 (`AppConfig`):** characterization tests first (none exist today for `resolve_app_version`/`resolve_meshtastic_cli`/`resolve_serial_port` per D0 §3.1's own coverage column) — write these against the *current* `server.py` functions before moving them, so the move itself has a safety net from day one, not day five.
- **D1.2 (`AppContext`):** no new test logic needed beyond confirming `server_module`'s existing 15-file cross-reference list (D0 §5) still resolves identically — a mechanical "does everything that used to be `server.foo` still equal `server.foo`" parity check, run once per merged phase.
- **D1.3 (identity/transport-restore):** **lower net-new-test burden than initially expected, confirmed by actually reading the existing suite for this audit** — all three invariants already have dedicated, passing regression tests today, not just informal code comments: `tests/test_server_startup_tcp_transport.py` directly exercises `restore_active_transport()`, including `test_restore_active_transport_skips_tcp_entirely_on_identity_mismatch` (invariant §2.2) and `test_restore_active_transport_reuses_an_already_connected_tcp_link_without_reconnecting` (invariant §2.3, the exact PR #279 regression) plus reconnect-when-stale/reconnect-when-mismatched-endpoint counterparts; `tests/test_server_mca_startup_wiring.py`'s AST-based test covers invariant §2.1 (see §4's finding above). D1.3's actual job is **moving these three functions without breaking any of that existing coverage** — every one of those tests currently reaches into `server_module.restore_active_transport`/`server_module.verify_radio_identity`/`start_runtime`'s own source by name, so each needs its import path (and the AST test's source-parsing target) updated in the same commit as the code move, not left pointing at the old location. Net new test needed for D1.3 specifically: none identified beyond that import/target update — flag during implementation if the move surfaces a gap this audit's read-through missed.
- **D1.4 (`create_app()`):** parity test — every route currently reachable stays reachable at the same path with the same registered view function, comparable via `app.url_map` before/after.
- **D1.5 (`Runtime.start()`):** re-run D0's own existing coverage (`test_server_startup_degraded_serial.py`, `test_smoke_import.py`, `test_runtime_lock.py`) unchanged against the new entry point — if these still pass without modification, the move preserved behavior; add one new test asserting `check_startup_calls.py`'s updated target/entries (§4) actually still catches a deliberately-reintroduced ordering bug (a "the safeguard still works" meta-test, not just updating the script itself).
- **D1.6 (`Runtime.stop()`):** new test harness (see migration plan above) covering: every service with a real `stop()`/`close()` today (§8's table) is called in the correct order (`AttachmentsService` → `TransportRouter` → `AdapterSupervisor` → `SerialPortSupervisor`, matching the dependency ordering §8 already identifies) and returns before some bounded timeout; explicit assertion that the seven bare-function threads are **not** joined (if option 2 from §8 is what D1.6 actually decides) with a comment pointing back at this document's §8 reasoning, so a future reader doesn't mistake the omission for an oversight.

---

## Appendix: cross-reference to existing docs

This document assumes familiarity with and defers to, rather than duplicates:
- `server-decomposition-audit.md` — full `server.py` function/global inventory, PR sequence reasoning.
- `dependency-map.md` — full per-directory dependency tables, GPL/circular-import sweep results.
- `docker-blockers.md` — the systemd/sudo/GPIO/config-path blocker list this audit's §3 CLI finding feeds into, for whoever picks that work up.
- `ipc-carrier-audit.md` — the adapter subprocess IPC wire-protocol detail underlying `tcp_ipc_transport`/`AdapterSupervisor`, not re-derived here.
- `persistent-storage-inventory.md` — the `data/` file classification already used correctly (and re-confirmed live, on pixel-111's production migration, in this same working session) — unaffected by this track.

**Stop for review**, per the task brief — D1.1+'s final split is §7's proposal, not a decision; confirm or adjust before any implementation phase begins.
