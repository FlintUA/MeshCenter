# D0 audit summary and proposed PR sequence

**Status:** read-only audit complete, no behavior changed, no code moved. This document ties together the four detailed audit reports and proposes a PR sequence for review. **No Dockerfiles, Compose files, or code changes were produced in D0**, per the brief.

Companion documents (all in `docs/architecture/`):
- [`server-decomposition-audit.md`](server-decomposition-audit.md) — full `server.py` function/class/global inventory, module-import side effects, `start_runtime()` sequencing, test-coverage gaps.
- [`dependency-map.md`](dependency-map.md) — dependency graph across every package, the grep-sweep findings (circular imports, GPL boundary, subprocess/sudo/systemd/GPIO/etc.), per-directory summaries.
- [`ipc-carrier-audit.md`](ipc-carrier-audit.md) — how Core detects adapter-process death today, and a recommendation (Unix domain socket over a shared volume) for the future cross-container carrier.
- [`persistent-storage-inventory.md`](persistent-storage-inventory.md) — every file under `data/`, format/owner/sensitivity/durability.
- [`docker-blockers.md`](docker-blockers.md) — what currently prevents starting without a serial device/systemd/Pi hardware/as two containers, plus the GPL-compliant-distribution question.

**Note on "the existing plan":** this worktree/audit found no prior written D1–D9 plan checked into the repository (searched `docs/`, `README.md`, `CLAUDE.md`) to diff against or refine in place. The sequence below is a fresh proposal derived entirely from this audit's findings — if a D1–D9 plan already exists elsewhere (a prior conversation, an external doc), treat this as input to reconcile against it, not a replacement for it.

---

## What actually blocks Docker/Server Mode (the short version)

1. **`import server` itself is not side-effect-free** — it can `SystemExit(1)` if the Meshtastic CLI binary isn't resolvable, and it unconditionally writes to `data/instance.json`/`data/profiles/<id>/` at import time, before any WSGI server or test framework gets a say (`server-decomposition-audit.md` §1).
2. **`config.py` resolution has no env-var override** — bind-mount-at-a-fixed-path is the only option today (`dependency-map.md` §1.5).
3. **Three JSON persistence implementations disagree with each other** in small but real ways, and one JSON file (`schedules.json`) isn't atomic at all — low-risk to fix, but worth doing before reasoning hardens around "the storage layer" for volume design (`dependency-map.md` §2's `storage/json_store.py` finding).
4. **The Core↔adapter IPC today is a literal parent-child OS pipe relationship** — the wire protocol (`protocol_version`/request/response/error) is carrier-agnostic and doesn't need to change, but the process-ownership model (`proc.kill()`, `PR_SET_PDEATHSIG`, `KillMode=control-group`) is entirely local-parent-child/cgroup-based and has zero meaning across a container boundary (`ipc-carrier-audit.md` §4, `docker-blockers.md` §4).
5. **Three features are host-privileged in a way containers can't cleanly reach**: e-Paper GPIO/SPI, `hardware_config.py`'s `/boot/firmware/config.txt` editor (needs a physical reboot — arguably meaningless in a container context at all), and `network_config.py`'s NetworkManager/Wi-Fi management (`--net=host` or a D-Bus-proxy sidecar). All three are already optional/gateable features, which makes "exclude from the base Core image, decide per-feature later" a reasonable default rather than a blocker to solve up front.
6. **GPL redistribution posture is undocumented for any shipped image.** The current three-legged isolation model (venv separation, process separation, CI-verified non-importability) is architecturally sound and — per this audit's reading of the project's own stated reasoning — would remain sound for a combined single-image deployment, but nothing in the repo today addresses the different obligations that come from *distributing* GPLv3 code in a binary artifact versus today's "the installer `pip install`s it on the user's own machine" model (`docker-blockers.md` §5).
7. **`workers = 1` / `fcntl.flock()` single-instance design is permanent**, independent of container topology — Core can never be horizontally scaled while all shared state lives in one process's memory with no cross-process synchronization. Worth stating explicitly so no later PR accidentally designs toward multi-replica Core.

---

## Proposed PR sequence

### D1 — Bootstrap decomposition (the actual scope, refined by this audit)

The brief for D1 is "boundaries of bootstrap decomposition." This audit's finding is that the real bootstrap-decomposition surface is **`server.py`'s module-import-time side effects** (`server-decomposition-audit.md` §1, table rows 4/8/9 specifically — CLI resolution, `InstanceManager.load_or_create()`, `ProfileManager.ensure_profile()`), **not** `start_runtime()` — `start_runtime()` is already reasonably well isolated (that was the point of the `wsgi.py` split in PR #69) and already has test coverage for its degraded-serial path.

Proposed scope: make every import-time side effect in server.py's top-level code either (a) deferred into an explicit, callable bootstrap function distinct from `start_runtime()`, or (b) tolerant of running in an environment where `config.py`/the CLI binary/the data directory aren't yet in their final form — without changing production behavior when run the normal way. This is what actually unblocks "can a container import `server.py` to run tests/tooling against it without full production preconditions," which is the real Docker prerequisite, not `start_runtime()`'s own sequencing.

Small, low-risk companion fix worth bundling into D1 or landing just before it: add a `CONFIG_PATH`/`MESHCENTER_CONFIG` env-var override to how `config.py` is resolved (`server.py:90-98`, `gunicorn.conf.py`), since D1's bootstrap work will need to reason about config resolution anyway and this is the concrete gap blocking clean secret injection later.

*(This PR needs its own separate, focused pass per the user's own stated plan — this section is scoping input for that pass, not a substitute for it.)*

### D2 — Platform capabilities (already scoped, per the brief)

Not deep-dived here (explicitly out of scope for D0 per the brief), but this audit's findings are directly relevant input: the three host-privileged features named in §6 above (e-Paper, hardware-config `/boot` editor, Wi-Fi NetworkManager) plus camera's `--system-site-packages`/Picamera2 coupling are the concrete list of "capabilities" D2 needs a decision framework for — each is independently already gateable (`EPAPER_ENABLED`, camera power persisted off, etc.), which is a good sign D2 has real existing seams to build on rather than needing new ones.

### D3 — Storage-layer consolidation (zero-risk, high-leverage, do early)

Collapse the four independent JSON atomic-write implementations (`server.py`, `storage/device_manager.py`, `camera/camera.py`, plus fixing `meshsrv/schedule_engine.py`'s non-atomic writer) onto `storage/json_store.py`. No behavior change for the common case; removes a confirmed divergence (server.py's copy is missing `os.makedirs`) and the one non-atomic JSON writer found in the codebase. Doing this before D4/D5 means the storage layer's actual behavior matches what `persistent-storage-inventory.md` documents, rather than having three slightly-different implementations to reason about when designing volume/secrets handling.

### D4 — Config and secrets externalization

Building on D1's `CONFIG_PATH` env var: formalize how the three secrets-grade files (`data/secret_key.txt`, `data/auth.json`, `data/mca/<id>/keys/identity_ed25519.seed`) are meant to be handled in a Docker deployment — Docker secrets, a mounted read-only secret volume, or documented equivalent. This is a documentation-and-convention PR primarily, informed directly by `persistent-storage-inventory.md`'s sensitivity classification; no code change is strictly required unless the current file-permission-based handling (`chmod 0600`) needs strengthening for a shared-volume scenario.

### D5 — IPC carrier + adapter process-ownership redesign

Per `ipc-carrier-audit.md`: implement the Unix-domain-socket carrier behind the existing, untouched `protocol_version`/request/response/error contract, **and** separately design what replaces `AdapterSupervisor.call()`'s direct `proc.kill()`/`proc.wait()` ownership plus the two-legged orphan protection (`KillMode=control-group` + `PR_SET_PDEATHSIG`) — most plausibly an explicit shutdown/health-check IPC operation combined with the container orchestrator's own restart policy for the adapter side. These are two distinct pieces of design work that happen to land in the same PR/milestone because they're both prerequisites for a working Core/adapter container split; they are not the same decision (see `ipc-carrier-audit.md` §4's explicit note that carrier choice doesn't resolve the ownership question either way).

### D6 — GPL redistribution documentation + combined/split-image CI enforcement

Per `docker-blockers.md` §5: close the documentation gap around image-redistribution obligations (distinct from today's "installer fetches `meshtastic` onto the user's own machine" posture) before any image containing the adapter venv is built or published, and add a container-level analogue of the existing `scripts/_smoke_test_core_harness.py` archive-based check — today's check only verifies a Core-only *extracted archive* can't import `adapters`/`meshtastic`; nothing currently verifies the same property for an actual Docker image.

### D7 — Dockerfiles and Compose

The actual containerization work, gated on D1–D6: a Core image (built from a Raspberry Pi OS-derived or equivalent base to satisfy the `--system-site-packages`/Picamera2 constraint if camera support is included) and an adapter image, wired together via D5's chosen carrier. Explicitly out of scope for D0 and not started here.

### D8 — Host-privileged feature triage

Per-feature decisions (informed by D2) for e-Paper GPIO/SPI, `hardware_config.py`'s `/boot/firmware/config.txt` editor, and `network_config.py`'s NetworkManager access: each either stays bare-metal-only (documented as "not available under Docker Server Mode"), becomes a privileged host-side sidecar/agent the containers call into, or is deliberately dropped from the containerized deployment path. `hardware_config.py`'s `/boot` editor in particular (§6 item 5 above) is the strongest candidate for "stays bare-metal-only, full stop" given it requires a physical reboot to take effect at all.

### D9 — Test-coverage backfill for load-bearing-but-untested modules

Not a single PR — a standing requirement gating any future extraction PR that touches: `meshsrv/schedule_engine.py`/`schedule_actions.py` (unattended, fires real mesh sends on a timer, zero tests), `meshsrv/radio_manager.py` (5-state machine, zero tests, ~20 call sites), `telemetry/telemetry.py` (zero tests despite heavy two-way global-mutation coupling with `server.py`), and the ~30 native `@app.route` handlers still defined directly in `server.py` rather than in `api/*.py` (`server-decomposition-audit.md` §3.12 — the single largest test-coverage gap this audit found, including the radio-profile-switch endpoints that shell out to `sudo systemctl restart`). Any D1–D8 PR that touches one of these modules should add characterization tests as part of that PR, not defer them.

---

## Stopping point

This completes D0. Per the brief: **no code changes, no file moves were made.** The next step, per the user's own stated process, is a separate focused pass to refine D1's exact bootstrap-decomposition boundaries using the findings in `server-decomposition-audit.md` §1 and §6 as input — stopping here for review before that pass begins.
