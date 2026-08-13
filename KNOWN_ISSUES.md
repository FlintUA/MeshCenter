# MeshCenter — Known Issues (feature/time-system)

## KI-001: e-Paper driver hang on start
Status: pre-existing, not a Stage 3 regression
Symptom: `[EPAPER] Display start() timed out`, refresh_count: 0
Likely cause: BUSY-pin race on the Waveshare HAT (see commit be5937b)
Stage 3 impact: clock overlay added to the renderer, hash-split dedup
implemented and proven by isolated test. Physical-panel visual check
pending until the hardware issue is resolved.
Pending: verify 12h-format clearance on WeAct 154 - Ukrainian title
"Повідомлення" (131px) + "10:52 AM" (56px) leaves ~5px margin.

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
