# MeshCenter — Known Issues

## KI-001: e-Paper driver hang on start (Waveshare 2.13" color HAT) - RESOLVED
Status: resolved 2026-08-13, on dev node (.104)
Original symptom: `[EPAPER] Display start() timed out after 75s`, repeated
across all 3 retry attempts, refresh_count stuck at 0.
Root cause: NOT a hardware BUSY-pin race as originally suspected. A
standalone script bypassing the long-running service process started the
exact same driver cleanly in under 1s, with real BUSY transitions
(1->0->1->0->1) and a clean `get_status()`. Restarting meshcenter.service
(a fresh process) then also started cleanly - refresh_count:1,
last_duration ~21.5s (realistic for a full 4-color refresh), and the
result was visually confirmed on the physical panel (test pattern, System
page, and the Stage 3 clock overlay all rendered correctly, time matched
the Pi's clock). The hang was orphaned GPIO/vendor-module state left
behind after repeatedly switching drivers (WeAct <-> Waveshare) on the
same physical GPIO pins within one long-running process - exactly the
"orphaned thread still touching epdconfig.implementation" scenario
already called out in waveshare_213g.py's own docstring.
Fix: `sudo systemctl restart meshcenter.service` (fresh process) cleared
the stuck state. No code change was needed.
Follow-up: if this recurs WITHOUT a driver switch beforehand, treat it as
a new/different issue - this specific instance is now closed.
Stage 3 impact: now fully physically verified on real hardware (previously verified only via logs and an isolated fake-driver test).

## KI-001b: WeAct 1.54" panel - no visible refresh despite clean protocol (open)
Status: open, unresolved (dev node .104)
Symptom: the same physical WeAct panel that previously worked completes
clear()/render() calls successfully - BUSY genuinely transitions during
init(), the SSD1681 command sequence is protocol-correct, refresh_count
increments, error_count stays 0 - but the physical panel shows zero
visible change on any command (clear, checkerboard+text test pattern,
or the normal status page).
Ruled out: wrong host, disabled feature flag, wrong config schema, wiring
order (RST/DC/CS/BUSY physically re-verified twice by the user), and the
RST/BUSY signals specifically (confirmed responsive via a live GPIO
trace during a real init() call). Waveshare 2.13" now confirmed working
correctly on this exact Pi using the same GPIO pin numbers (see KI-001
above), which weakens a "Pi-side GPIO/SPI hardware fault" theory - note
this doesn't fully rule out a WeAct-board-specific fault, since each HAT
is a physically separate board/cable even when both use the same pin
numbering convention.
Suspected: the SPI bulk-data lines (MOSI/GPIO10, CLK/GPIO11) - the actual
pixel-data path - were never individually tested (only RST/DC/CS/BUSY
were traced); or an internal panel/flex-cable fault (e.g. ESD or handling
damage), since the failure appeared only after this exact panel was
uninstalled and reinstalled.
Next step: continuity-test MOSI/CLK specifically with the panel
disconnected and powered off, or physically inspect/reseat just that
wire pair.

## KI-002: meshtastic 2.7.11 has no getTime()
Status: library limitation
Symptom: drift evaluation in node_time_sync.py always gets None for
node_time -> decision is always 'invalid', drift thresholds unused.
Resolution path: evaluate_drift() and the constants are kept intentionally.
When getTime() lands in the library, uncomment the _get_node_time() call
in try_sync().

## KI-003: send_data_report field picker not in the UI
Status: backend complete, UI is a placeholder
Symptom: the schedule form shows "Data report field selection isn't
available in the UI yet - configure via the API for now." for
send_data_report (see `_renderDataReportParams()` in static/chat.js).
Next step: build a real telemetry-field picker in the schedule form.

## KI-004: Schedule lock held during action execution
Status: deliberate MVP tradeoff
Symptom: schedule_engine._tick()'s _lock is held while actions run, so a
mesh-sending tick can block the schedules CRUD API for a few seconds.
Next step: move execution outside the lock if this becomes a real problem.

## KI-005: Timer mesh target picker
Status: RESOLVED - was already correct as of Stage 7, contrary to an
earlier stage's draft note
Verified by reading the real code (Stage 8 audit): the timer form's
notify-mesh section calls the exact same generic, prefix-based
`_renderTargetPicker('tm', ...)` / `_scheduleReadTarget('tm')` helpers the
schedule form uses (static/chat.js, `openTimerForm()` /
`createTimerFromForm()`), not a hardcoded or partially-wired picker. No
further work needed here; this entry is kept only for historical record
that the earlier "incomplete" note was stale.

## KI-006: mesh_send / send_data_report had no local chat-history record
Status: FIXED in Stage 8
Symptom (as verified live in Stage 7): `schedule_actions.send_mesh_message()`
transmitted successfully over radio but never wrote a local `kind: "me"`
chat record, so schedule/timer-triggered sends were invisible in the
MeshCenter chat UI (`GET /api/messages?chat_id=...` never showed them).
Fix: `schedule_engine.start()` now also receives `add_message`,
`LOCAL_NODE_NAME`, and `CHANNEL_CHAT_ID` from server.py (server.py's own
`add_message` function/globals - the exact mechanism api/api_chat.py's
send worker uses), threaded through to
`meshsrv/schedule_actions.configure()`. On a successful mesh send,
`send_mesh_message()` now calls `add_message("me", LOCAL_NODE_NAME, text,
node_id=LOCAL_NODE_ID, chat_id=<node_id-or-channel-id>)` under
`state_lock`, exactly mirroring api/api_chat.py's own post-send bookkeeping
(api/api_chat.py:117-153). Verified by direct wiring test (configure()
called with a stub `add_message` and the same chat_id-derivation logic
used in production, confirming the call reaches the injected function with
the right arguments) - not verified with a live radio send in this stage
(no additional live-send authorization was granted beyond the one already
used and cleaned up in Stage 7).

## KI-007: Chat-list timestamps don't follow the 12h/24h toggle
Status: planned
Symptom: message timestamps shown in the chat list are formatted
server-side and don't react to the `Settings > Units` 12h/24h display
toggle the rest of the interface respects (`TimeFormatter`).
Next step: route chat-list timestamp formatting through the same
client-side `TimeFormatter` the Time card and other timestamps already use,
instead of a server-formatted string.

## KI-008: Serial hot-reconnect (H2-C) - RESOLVED
Status: resolved 2026-10-04, live-verified on dev (192.168.2.104), PR #331
Background: previously, an unplugged-then-replugged serial radio
(including a USB cable swap/reseat, or a power cycle of the radio itself)
did not reliably recover without a full `sudo systemctl restart
meshcenter.service` - the listener's own fixed, once-resolved port and
device identity never noticed the physical change. Confirmed live on dev
(192.168.2.104, 2026-10-03) across three distinct failure shapes: the
listener subprocess going hung-but-alive instead of exiting; the radio
re-enumerating at a different `/dev/ttyACMx` path; and a boot-time
identity check failure permanently disabling recovery even once the radio
came back.
What changed: the serial listener (`meshsrv/serial_port_supervisor.py`,
`meshsrv/serial_reconnect.py`) now detects a genuine disconnect (the
Meshtastic library's own stdout warning, or a changed/missing device
node), resolves the radio's `/dev/serial/by-id/*` link so a changed
`/dev/ttyACMx` number doesn't matter, and re-verifies the radio's identity
with a real `--info` probe before resuming - so a different physical
radio appearing at the same path is never silently treated as the
accepted one. If a different radio is detected, the listener halts (no
automatic resume) and shows "A different radio was connected" in the
System Log and as a notification; use **Reconnect Radio** (`Settings >
Meshtastic Radio`) once you've confirmed which radio should actually be
plugged in, or switch profiles if you intend to use the new one.
Boot-time identity failures (radio not present/verified at service
start) now enter the same wait-and-recheck state instead of permanently
disabling recovery. The separate adapter subprocess (send/`get_channels()`)
now also tracks the listener's current verified port
(`current_verified_port()`/`on_port_changed()`), so it no longer keeps
retrying a stale path after a real device-path change - see PR #331's
description for the full round-4 design writeup.
Live verification: Steps 1-3 (USB unplug/replug on the same path, on a
renumbered `ttyACM0->ttyACM1` path, and a forced path change while the
adapter has an outstanding call) each passed across two independent
rounds on dev, including a 30-minute quiet-period comparison against
`main` (see PR #331 description) confirming no regression. **Not yet
live-verified: a different physical radio connected on replug (the
identity-MISMATCH path)** - no second USB Meshtastic radio was available
for this round. That path is covered by the automated test suite
(`tests/test_serial_hot_reconnect.py`) but remains live-unverified; treat
it as the one still-open sub-case of this otherwise-resolved issue.
Status detail: covered by an extensive unit-test suite
(`tests/test_serial_hot_reconnect.py`, `tests/test_serial_reconnect.py`,
`tests/test_serial_only_routes_non_serial.py`,
`tests/test_start_runtime_worker_gate.py`, `tests/test_adapter_ipc_client.py`,
`tests/test_autorecovery_supervisor_gate.py`,
`tests/test_channel_discovery_port_absent_backoff.py`) and mutation-tested
against the specific regressions each test claims to catch.

## KI-009: Quiet-period listener restarts during normal operation (monitoring, not confirmed as a #331 regression)
Status: **closed, pre-existing/cosmetic** - resolved by the 24h passive
measurement below (PR #331 merged as `23d3671`), not a release blocker
Symptom: during normal operation the serial listener subprocess restarts
a handful of times per hour for two unrelated, non-hardware reasons:
1. `get_channels()`/`set_device_time()` legitimately pause the listener
   while they claim the serial port through the adapter subprocess - the
   claim itself takes 10-15s (channel discovery is a real multi-packet
   radio round-trip, not a cache hit), occasionally landing the health
   check mid-claim and reporting `LISTENER_DOWN` instead of the more
   accurate `PAUSED`. Router-lock hold mechanism:
   `meshsrv/transport_router.py:151-181` (`TransportRouter._delegate()`);
   caller: `api/api_chat.py:276` (`discover_radio_channels()`). Confirmed
   present at the same 13-15s hold times on both `main` and the PR branch
   - pre-existing, unrelated to #331.
2. An independent Meshtastic-library-internal event - stdout line
   `DEBUG file:stream_interface.py _disconnected line:102 Closing our
   port`, process exit code 1, with **no** corresponding dmesg/USB event.
   Confirmed by code inspection this is NOT the H2-C disconnect-detection
   path: `DISCONNECT_LINE_MARKER` (`meshsrv/serial_reconnect.py:42`) is
   `"device reports readiness to read but returned no data"`, which does
   not match this line, so `line_signals_disconnect()` returns `False`
   and the event falls through to the plain `return_code != 0` respawn
   branch (`meshsrv/serial_port_supervisor.py:399-404`) - a path #331
   did not add or modify. Self-recovers within ~5s every time, no new
   `[IDENTITY]` re-check line, no lost messages observed.
Investigation so far (2026-10-04/05, dev node, item 2 specifically):
- **The pre-H2-C historical baseline no longer exists.** `journalctl`'s
  own retention on dev had already rotated past the point needed
  (`--disk-usage` reported 7.2M total, one boot listed) by the time this
  was checked - everything before 2026-10-05 10:21 CEST is gone,
  including all of main's pre-#331 history. `dmesg`'s much smaller-volume
  kernel ring buffer still reached back to September, confirming this
  was rotation, not an actual reboot - but the application-level lines
  needed to count this specific event are gone for good. No further
  historical reconstruction is possible on this node.
- **Two comparisons were run same-day instead, with different results.**
  A controlled, same-tab-condition, back-to-back A/B (`main` 21:14-21:46
  vs PR `45770d1` 21:46-22:17, 2026-10-04) measured 1 event/32min on
  `main` vs 4 events/31min on the PR branch (~4.2x). A much larger but
  uncontrolled same-day sample (~10h of PR-branch operation spanning
  normal daytime use, browser activity unknown) measured ~3.8/hr,
  against that same single `main` data point at ~1.9/hr (~2.0x) - right
  at the "pre-existing" threshold instead of clearly over it. The two
  comparisons disagree because the controlled pair has an n=1 main
  sample (high variance) and the larger sample isn't actually
  apples-to-apples (unknown tab/load conditions).
- **A candidate mechanism was raised and considered, then set aside.**
  `radio_health_worker`'s ~30s tick unconditionally calls
  `listener_supervisor.terminate_if_device_changed()` for every serial
  tick (`server.py:6197`), which calls `capture_device_identity(port)` -
  an `os.stat()` of the device node plus, for USB, a sysfs walk
  (`meshsrv/serial_reconnect.py:95-130`) - a new per-tick filesystem
  touch that doesn't exist on `main`. Reviewed and set aside as the
  cause: neither operation opens the tty itself, so there's no plausible
  path from a `stat()`/sysfs read to the Meshtastic library deciding to
  close its own port. Not ruled out with a live trace, just judged
  unlikely enough not to justify a defensive code change without
  evidence.
Current plan: passive 24h measurement, no deliberate interaction beyond
normal use, starting from the 2026-10-05 merge/deploy of `23d3671` to
dev/camtest/pixel-111. After 24h, count "Closing our port"+exit-1 (no
dmesg USB event) and `LISTENER_DOWN` occurrences per hour, excluding any
window with a deliberate replug or maintenance restart. Decision: a
sustained rate above ~3/hr, or any occurrence that doesn't self-recover
within its normal ~5-30s window, opens a dedicated follow-up that
captures `lsof`/`strace` on the listener and adapter PIDs at the moment
of the next occurrence to find what (if anything) touches the port
concurrently, before considering any code change. A rate at or below
that, with every occurrence self-recovering, closes this out as
pre-existing/cosmetic and this entry is updated to reflect that.

**24h measurement result (dev, 2026-10-05 16:35 CEST -
2026-10-06 16:46 CEST, ~24h12m):** `journalctl -u meshcenter.service`
over the full window shows **zero** "Closing our port" lines and
**zero** `LISTENER_DOWN` lines (2,902 `radio_health_worker` ticks, all
`status=OK`) - a measured rate of **0/hr** for both signals, well below
the ~3/hr threshold. One service restart occurred inside the window
(18:04:28-18:04:50 CEST), tied to the deploy of PR #332 (this same
KI-009 doc commit) and PR #333 (unrelated H2-D work) - excluded as
deliberate maintenance per the plan above; it was the only gap, lasted
22s, and dev's checkout stayed on a descendant of `23d3671` (confirmed
via `git merge-base --is-ancestor 23d3671 HEAD`) for the entire window,
so the measurement covers continuous #331-inclusive operation
throughout. `dmesg -T` shows no USB disconnect/reconnect events for the
radio's device during the window either (the only USB JTAG/serial
events in the ring buffer are from 2026-10-04, two days prior, and
unrelated to the Meshtastic radio). Decision: rate at/below threshold,
closing this out as pre-existing/cosmetic per the stated rule - no
further action needed on #331 specifically. The original item 2
quirk ("Closing our port"/exit-1, no dmesg event) may still occur at
low background rates under different conditions (the same-day A/B
upstream suggested it exists on `main` too, at ~1-4/hr depending on
sample), but this measurement found no evidence it is elevated by
#331's hot-reconnect changes.
