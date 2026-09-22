# Core↔Adapter IPC carrier audit (D0)

**Status:** read-only audit, no behavior changed. Open decision for D5, not pre-fixed here — this document documents the current implementation precisely, then evaluates the two candidate carriers named in the D0 brief against what that implementation actually depends on, and makes an explicit recommendation.

**Files read directly for this section:** `meshsrv/adapter_ipc_client.py` (790 lines, read in full), `meshsrv/ipc_protocol.py` (framing/serialization, read in full), `adapters/meshtastic/ipc_server.py` (structural inventory from the parallel `modules/display`+`adapters` audit, cross-checked against `meshsrv/adapter_ipc_client.py`'s own docstring, which quotes the adapter side's behavior in detail).

---

## 1. What exists today, exactly

### 1.1 Process spawn and ownership

`AdapterSupervisor._spawn_locked()` (`meshsrv/adapter_ipc_client.py:338-376`) spawns the adapter as a **direct child process** of Core:

```python
subprocess.Popen(
    [adapter_python, "-m", "adapters.meshtastic.ipc_server", "--serial-port", ..., "--meshtastic-cli", ...],
    cwd=project_dir, env={**os.environ, "PYTHONPATH": project_dir},
    stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1,
    preexec_fn=_set_pdeathsig_to_sigkill if sys.platform == "linux" else None,
)
```

This is a **one persistent subprocess, spawned lazily on first call, multiplexing both transport types** (`transport_type: "serial"|"bluetooth"` is a field in every request, not a separate process per type — confirmed by `AdapterIPCTransport.__init__` taking a *shared* `AdapterSupervisor` instance for both `serial_ipc_transport` and `ble_ipc_transport` in `server.py`).

Three things are true about this ownership model that any carrier swap has to either preserve or deliberately replace:

1. **Core holds the adapter's actual OS process handle** (`self._proc: subprocess.Popen`) and can `proc.kill()` it directly (SIGKILL, `_kill_locked()`, line 556-576), then `proc.wait(timeout=5.0)` to confirm.
2. **Core is the adapter's literal parent process** — this is what makes `PR_SET_PDEATHSIG` (§1.4 below) work at all: the kernel only delivers that signal when *this specific parent* dies, because it's a parent-child relationship tracked by the kernel, not an application-level heartbeat.
3. **`deploy/meshcenter.service`'s `KillMode=control-group`** (systemd) is the *other* leg of orphan protection — it works because the adapter, never having been detached (`subprocess.Popen` doesn't set `start_new_session`/`setsid` here), stays in the same cgroup as Core, so systemd's cgroup-wide kill on `stop`/`restart`/crash-triggered-`Restart=on-failure` takes the adapter down too.

**Neither of these two protections survives a cross-container split, by either candidate carrier.** A UDS-over-shared-volume or a TCP-over-Docker-network design puts the adapter in a *different* container, hence a different process tree and (for TCP, definitionally; for UDS, in practice under Docker/Compose/K8s) a different cgroup. This is the single biggest architectural gap the carrier choice does **not** resolve — see §4.

### 1.2 Wire framing (carrier-agnostic layer, must stay untouched per the D0 brief)

`meshsrv/ipc_protocol.py`: `PROTOCOL_VERSION = 1`, explicit per-type `*_to_dict()`/`*_from_dict()` functions (not generic reflection) converting the `RadioTransport` ABC's dataclasses (`ConnectionInfo`, `TransportError`, `NodeInfo`, etc.) to/from plain dicts. Every request is:

```json
{"protocol_version": 1, "operation": "connect", "transport_type": "serial", "params": {...}, "timeout": 8.2}
```

and every response is either `{"ok": true, "result": {...}}` or `{"ok": false, "error": {"code": "...", "message": "..."}}` (per `AdapterIPCTransport._call()`, lines 611-669, and the docstring's description of `ipc_server.py`'s `make_ok_response`/`make_error_response`). This request/response/error shape is carrier-independent — it's just JSON — and per the D0 brief's own constraint, stays exactly as-is regardless of which carrier D5 picks.

### 1.3 How death/EOF is actually detected (the part that's carrier-sensitive)

`AdapterSupervisor.call()` (lines 392-554) does **not** poll for liveness on a timer. Detection happens in exactly three ways, all tied to pipe semantics:

1. **Pre-flight check**: `self._proc.poll() is not None` (line 424) — if the last-known process object reports it has already exited, respawn before sending. This is an OS-level "did the child exit" check (`waitpid(..., WNOHANG)` under the hood), not a framing-level signal.
2. **Blocking read + EOF-as-death, on a dedicated per-call thread**: a fresh `threading.Thread(target=_read, ...)` (line 496, named `adapter-ipc-reader`) calls `proc.stdout.readline()` in a loop. **An empty string return from `readline()` is Python's own signal for "the underlying pipe was closed"** (line 471-473: `if not raw: result_box.put(("closed", None)); return`). This is the actual death-detection mechanism: when the adapter process exits (crash, kill, normal exit), the kernel closes its stdout fd, which is the write end of Core's read pipe, which makes `readline()` return `""` almost immediately (no application-level heartbeat needed — this is automatic, kernel-mediated pipe-close propagation).
3. **A hard backstop timeout, not a liveness check**: the main thread (not the reader thread) does `result_box.get(timeout=timeout)` (line 500) — a `queue.Queue` with `maxsize=1`, fed by the reader thread. If nothing arrives (no EOF, no valid response, no protocol error) within the caller-declared `timeout`, `queue.Empty` fires, Core kills the adapter itself (line 502) and raises `TransportError(TIMEOUT)`. **This is what catches a wedged-but-still-alive adapter** — EOF-on-death only catches an adapter that has actually exited; a hung one (deadlocked internal thread, blocked syscall) never closes its pipes, so only the timeout backstop notices it.

**A structural detail worth flagging explicitly**: the reader thread is spawned fresh *per call* and is never explicitly joined or cancelled. If the timeout backstop fires first (case 3), the reader thread is still blocked inside `proc.stdout.readline()` — it only unblocks once `_kill_locked()`'s `proc.kill()` actually terminates the process and the kernel closes the pipe, at which point the orphaned reader thread tries to `result_box.put(("closed", None))` into a queue nobody is reading from anymore (`maxsize=1`, and by then a new call may have created a fresh queue) — it just blocks forever on `put()` as a harmless, permanently-parked daemon thread. This is safe today only because it's a `daemon=True` thread and the queue/process pairing is scoped per-call — but it's a concrete example of the design leaning on **pipe-close being instantaneous and synchronous with process death**, which is a property `readline()` on a pipe gives "for free" via the kernel and which a socket-based carrier would need to reproduce deliberately (see §2/§3).

### 1.4 Buffering behavior assumptions

- Core opens the pipes as `text=True, bufsize=1` (line 356-357) — Python-side line buffering for **Core's own read/write of the pipe object**, not a guarantee about the adapter's own stdout buffering.
- The adapter side (`ipc_server.py::serve_forever()`, per its own module structure confirmed by the parallel audit) explicitly wraps every `dispatcher.handle()` call in `contextlib.redirect_stdout(sys.stderr)` — a **P0-severity, live-caught fix** (per `adapter_ipc_client.py`'s own comments, lines 186-203) for a real incident: a single stray `print()` anywhere in the adapter's call stack (including inside the `meshtastic` library itself, which Core does not control) landing on real stdout would corrupt the JSON stream, since stdout **is** the IPC channel, not a log — there is no separate framing/length-prefix/delimiter beyond "one JSON object per newline," so any non-JSON byte on that stream is ambiguous with protocol data.
- Because that fix is "tolerate a *few* stray lines, not zero," `MAX_NON_JSON_LINES = 5` / `MAX_NON_JSON_BYTES = 4096` (lines 190-203) is baked directly into the reader loop, with an escalation to `TransportErrorCode.ADAPTER_PROTOCOL_ERROR` (a distinct, diagnosable error code) once exceeded — this is a defense-in-depth measure specifically because **stdout is a shared, unstructured byte stream with no built-in message boundaries beyond newlines**, and both application code and third-party library code can write to it.
- `stderr=subprocess.PIPE` plus a **separate**, permanently-running drain thread (`_drain_stderr`, lines 378-390) exists purely to prevent backpressure: an OS pipe has a finite kernel buffer (documented as "typically 64KB on Linux," line 362), and nothing was reading stderr before this fix — a sufficiently chatty adapter (more likely now that stray prints get redirected *there* instead of stdout) could fill that buffer and deadlock on its own write. This is a second, independent buffering hazard the pipe-based design has to actively manage, not something free.

### 1.5 Concurrency model

`self._proc_lock` (a plain `threading.Lock`) serializes the **entire** spawn/write/read/kill sequence — at most one request is ever in flight to the adapter at a time, deliberately (`TransportRouter` already serializes callers to one in-flight IPC call per the docstring; this lock is "defense in depth," line 332-335). This is a single-request-at-a-time protocol on a single persistent connection — no multiplexed request IDs, no pipelining.

---

## 2. Candidate: Unix domain socket (UDS) over a shared volume

**What changes:** instead of `subprocess.Popen(..., stdin=PIPE, stdout=PIPE)`, one side (convention: the adapter, since it's the long-lived server-role process once decoupled from being a literal child) `bind()`s a UDS at a path inside a directory both containers mount (a Docker named volume or bind mount shared between the Core and adapter containers/services). Core connects as a client.

**What ports over almost unchanged:**
- UDS is still a **byte stream** (`SOCK_STREAM`), still supports `readline()`-equivalent framing (newline-delimited JSON survives untouched — no protocol change needed, satisfying the D0 brief's constraint directly).
- **EOF-as-death still works the same way.** When the peer process closes its end of the socket (exit, crash, explicit close), a blocking `recv()`/`readline()` on the other end returns `0 bytes`/EOF, exactly analogous to today's pipe EOF. The entire reader-thread + `queue.Queue(timeout=...)` + EOF-detection pattern in `AdapterSupervisor.call()` ports over with only the `proc.stdout`/`proc.stdin` objects swapped for a socket's `makefile()` (or raw `recv`/`sendall`) — this is the single strongest point in UDS's favor: it requires the least *conceptual* rewrite of the death-detection logic that took multiple live-caught fixes (§1.3, §1.4) to get right for pipes.
- No network stack involved — same-host, kernel-mediated, comparable latency/throughput to a pipe.
- No new authentication/encryption surface: a UDS's access control is standard Unix file permissions on the socket path (and, in a shared-volume Docker setup, whichever UID/GID both containers agree to run as) — consistent with this project's existing preference for filesystem-permission-gated boundaries over network-exposed ones (e.g. the privileged helpers in `hardware/hardware_config.py`/`meshsrv/network_config.py` are invoked via `sudo -n` + narrow sudoers, never a network RPC; MCAttach's private key file is `chmod 0600`, not served over any socket).

**What has to change regardless (not a UDS-specific cost, see §4):**
- Core no longer directly owns the adapter's OS process — `proc.kill()`/`proc.wait()` in `_kill_locked()` has no equivalent over a socket. Killing/restarting the adapter becomes "close the connection and rely on the container/orchestrator to restart the adapter container," which is a different failure-recovery model than today's in-process respawn-on-next-call.
- The stale-socket-file problem: unlike an anonymous pipe (which only exists for the lifetime of the `Popen` call and needs no cleanup), a UDS bound to a filesystem path leaves a stale socket file behind if the adapter container restarts uncleanly — `bind()` fails with `EADDRINUSE` on a leftover file, so the adapter's startup needs an explicit unlink-if-stale-then-bind sequence (a well-known, standard Unix pattern, but a genuinely new failure mode this design doesn't have today).
- Reconnection-with-retry logic on Core's side is new: today, `_spawn_locked()` inherently gets a fresh pipe pair on every respawn because Core *is* the one spawning; with UDS, Core has to detect "the adapter container isn't up yet / just restarted" and retry connecting with backoff, rather than unconditionally spawning.
- The stray-`print()`-corrupts-the-stream risk (§1.4) is **identical** for UDS — it's still an unstructured byte-stream shared between protocol data and (if not carefully redirected) diagnostic output, so `MAX_NON_JSON_LINES`/`MAX_NON_JSON_BYTES` tolerance and stdout-redirection inside the adapter remain necessary exactly as today. (Though with a socket, the adapter's own accidental `print()` calls go to *its own* stdout, which is no longer the IPC channel at all if the adapter is a proper socket server rather than communicating over its stdout — this is actually a structural improvement UDS enables "for free": the IPC channel and the process's stdout/stderr become two genuinely separate streams, rather than the same stream with a redirect-based workaround. Worth flagging as a real simplification opportunity, not just a neutral port.)
- Docker Compose/K8s topology requirement: both containers must share a volume (a named volume, or an `emptyDir` in K8s) and, in K8s specifically, must be **co-scheduled on the same node** (a UDS cannot cross nodes) — for a single-Raspberry-Pi deployment this is not a real constraint (there's only one node), but it is a hard ceiling on future flexibility.

---

## 3. Candidate: TCP on the internal Docker network

**What changes:** the adapter listens on a fixed TCP port on the Docker-internal bridge/network; Core connects to it by service name (Docker Compose's built-in DNS) or a fixed address.

**What ports over almost unchanged:** same as UDS for the framing layer — `SOCK_STREAM`, newline-delimited JSON, the reader-thread/EOF/timeout pattern all still apply structurally.

**What's meaningfully different from UDS, not just "TCP instead of a socket path":**
- **EOF detection is less reliable in the general case.** A cleanly closed TCP connection (adapter process exits normally, or the kernel closes the socket on process death, same as UDS) does deliver EOF/FIN promptly, same as a pipe or UDS. But TCP additionally has failure modes that produce *no* EOF at all in bounded time — a frozen/paused container (e.g. `docker pause`, or a cgroup freeze, or certain OOM/swap-thrashing scenarios), a network partition between the two containers' network namespaces, or a half-open connection after an unclean peer crash (no FIN sent) can all leave Core's `recv()` blocked with no signal that anything is wrong. **The existing `queue.Queue(timeout=timeout)` backstop in `AdapterSupervisor.call()` already covers this** — a caller-declared timeout fires regardless of *why* the read never completed — so this isn't a correctness gap given the current design's bounded-timeout discipline, but it does mean TCP relies more heavily on that backstop (and on TCP keepalives, not currently part of the design, to detect a half-open connection *between* calls rather than only during one) than UDS or pipes do, where EOF is close to instantaneous on death.
- **New attack surface**: a listening TCP port, even on an internal Docker bridge network not exposed to the host or the internet, is a categorically different security posture than a filesystem-permission-gated UDS or an anonymous pipe with no listener at all. This project consistently prefers the narrower option elsewhere (arm's-length CLI invocation over a Python import, Wi-Fi password via helper stdin never argv, narrowly-scoped `sudo -n` rules over broad ones, `HttpOnly`/`SameSite=Lax` session cookies) — a listening TCP socket, even internal-only, is a step away from that pattern and would need its own explicit threat-model note (e.g., "is the Docker internal network trusted, and by whom else could a container on it connect to this port") that doesn't need to exist at all for UDS.
- **Genuine flexibility gain, but not one this project currently needs**: TCP is the only one of the two candidates that generalizes to the adapter and Core running on *different hosts/nodes* (e.g. a future "radio adapter on a dedicated small board talking over the network to a Core running elsewhere"). Nothing in the D0 brief, CLAUDE.md, or this audit's own findings (`workers=1`, single serial port, single Pi, `fcntl.flock()`-based single-instance guarding — see `dependency-map.md` §10) suggests this is an actual near-term requirement; today's entire architecture assumes exactly one Core process and one physical radio on one machine.
- Port management: a fixed port needs to be chosen and kept from colliding with anything else in the Docker network; Compose service-name DNS resolution handles addressing, but the port itself is one more piece of configuration surface that doesn't exist with a socket path under a shared volume.

---

## 4. The carrier-independent problem: process ownership and orphan protection

Regardless of which of the two candidates D5 picks, **both** carriers break the same two things simultaneously, because both replace "Core directly forks and owns the adapter" with "Core and the adapter are independently-managed containers that happen to talk over a channel":

1. `AdapterSupervisor._kill_locked()`'s `proc.kill()` + `proc.wait(timeout=5.0)` has no direct equivalent — there is no "the adapter's OS process" for Core to hold a handle to anymore. The nearest equivalents are (a) closing the IPC connection and hoping the adapter container's own supervisor/health-check restarts it, or (b) adding an explicit `"shutdown"`/`"restart"` operation to the existing operation set (`connect`/`disconnect`/`send_text`/... — see `ipc_server.py`'s dispatch list) that the adapter honors by exiting cleanly, relying on the container runtime's restart policy to bring it back. Either way this is a **new piece of design**, not a carrier detail — it needs its own decision in D5, informed by whichever carrier is chosen but not determined by it.
2. The two-legged orphan protection this audit confirmed is real and load-bearing (`KillMode=control-group` + `PR_SET_PDEATHSIG`, §1.1) is **entirely process-tree/cgroup-based** and has no meaning across a container boundary. A cross-container split needs an equivalent for "Core died unexpectedly, don't leave the adapter running with a stale BLE GATT session" — most plausibly a container-runtime health check + restart policy on the adapter side, plus (per §1.1's BLE-specific note) some mechanism for the *adapter itself* to notice it's been orphaned and run its own `bluetoothctl disconnect` cleanup, since Core will no longer be reachable to do it on the adapter's behalf the way `_kill_locked()` does today (lines 567-576, `subprocess.run(["bluetoothctl", "disconnect", ...])`, called from **Core's** process today specifically because the adapter is already dead by the time that cleanup runs — in a cross-container design, whichever side still has BlueZ/D-Bus access, likely the adapter's own container, would need to own this cleanup on its own exit/signal path instead).

Neither of these is resolved by choosing UDS vs. TCP — flagging this prominently so D5 doesn't treat the carrier choice as the whole decision.

---

## 5. Recommendation for D5

**Recommend Unix domain socket over a shared volume as the default**, for three concrete reasons grounded in what this audit actually found, not a generic "UDS is usually better" preference:

1. **Smallest actual rewrite of the part of this module that took the most live-debugging effort to get right.** Sections §1.3/§1.4 above aren't hypothetical — `MAX_NON_JSON_LINES`, the stderr-drain thread, the `redirect_stdout(sys.stderr)` fix, and the P0-A "self-reported TIMEOUT still needs a kill" fix are all named, dated fixes for real incidents (Droidian stdout-corruption cascade, a live BLE cleanup gap). UDS preserves the byte-stream-with-EOF-as-death model those fixes were built around almost exactly; TCP does too, but with weaker EOF guarantees under certain failure modes (frozen containers, network partitions) that this design's existing bounded-timeout backstop already covers defensively, but which add real uncertainty a from-scratch design wouldn't want to re-derive confidence in without new hardware-in-the-loop testing — the same kind of live verification this whole IPC module's own history shows is genuinely necessary for this codebase, not optional.
2. **No new network attack surface**, consistent with the project's existing security posture elsewhere in the codebase (this audit found zero precedent for "expose a new listening network port on the local system" anywhere else in the design — every existing privileged/cross-process boundary in this codebase is either a CLI subprocess, a `sudo -n` helper with narrow sudoers, or (for the adapter today) an OS pipe; a TCP listener would be the first of its kind).
3. **Matches the deployment topology this audit actually confirmed** — single Raspberry Pi, `workers=1`, one serial port, `fcntl.flock()`-guarded single-instance Core (`dependency-map.md` §10) — where TCP's cross-host flexibility is not a capability anything in this repository's stated direction (CLAUDE.md, this D0 brief) asks for. Recommend revisiting TCP specifically if/when a genuinely distributed deployment (adapter and Core on different physical hosts) becomes an actual requirement rather than a hypothetical one — at that point the carrier can be swapped again without touching `meshsrv/ipc_protocol.py`'s wire shape, since both candidates (and the current pipe transport) already share that same JSON request/response/error contract.

**Regardless of carrier choice, D5 needs to separately design:** (a) an explicit adapter-restart mechanism to replace `proc.kill()`+lazy-respawn, and (b) a replacement for the two-legged orphan/BLE-cleanup protection described in §4 — neither is a consequence of picking UDS over TCP or vice versa, and both are more architecturally significant than the carrier decision itself.
