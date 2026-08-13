# MeshCenter — Known Issues (feature/time-system)

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
