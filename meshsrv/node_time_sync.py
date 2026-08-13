#!/usr/bin/env python3
"""
MeshCenter Node Time Sync
One-way sync: MeshCenter -> Meshtastic node.
Uses radio_session() — does not create an independent SerialInterface.

The write path is meshtastic.node.Node.setTime(timeSec), confirmed present
in the installed meshtastic==2.7.11 package (venv/lib/python3.13/site-packages/
meshtastic/node.py:944). It sends an AdminMessage with `set_time_only` set
to the given Unix timestamp via the node's own admin channel. Called on
`interface.localNode` it is fire-and-forget for the local node (no ACK/NAK
wait — Node._sendAdmin() only registers an onResponse callback when the
target is a remote node, see node.py:944-957), so this module treats it as
a synchronous call: if it doesn't raise, the request was handed to the
radio successfully.

There is no library-level "read the node's current clock" call (no
`getTime`, no CLI --get-time flag, and NodeInfo/DeviceMetadata as exposed by
this meshtastic version carry no live clock field) — `_get_node_time()`
always returns None, and `evaluate_drift()` already treats that as
'invalid', which forces a sync. This makes the sync effectively
unconditional (every eligible connect, throttled by MIN_SYNC_INTERVAL_S)
rather than drift-aware, which is the best this library version supports.

KNOWN LIMITATION — meshtastic 2.7.11
setTime() is implemented, getTime() is not (neither in the Python API nor
the CLI). Because of this, _get_node_time() always returns None,
evaluate_drift() would always return 'invalid', and the drift thresholds
(DRIFT_SMALL_S / DRIFT_MEDIUM_S / DRIFT_LARGE_S) are currently unused —
try_sync() below calls _set_node_time() directly instead of routing
through evaluate_drift(), since that routing was dead code (every path
other than 'skip' ends up syncing anyway, and 'skip' was unreachable with
node_time always None).

evaluate_drift() and the drift constants are KEPT intentionally: once the
library gains a getTime()-equivalent, uncomment the _get_node_time() /
evaluate_drift() call in try_sync() below and the drift logic will work
without any architectural rewrite.

Current behavior: sync runs on every reconnect; the only rate limit is
MIN_SYNC_INTERVAL_S.
"""
import time
import threading
from meshsrv.time_service import get_status as get_time_status, is_trusted

DRIFT_SMALL_S  =   60
DRIFT_MEDIUM_S =  300
DRIFT_LARGE_S  = 3600
NODE_TIME_INVALID_THRESHOLD_S = 86400 * 365

_sync_lock   = threading.Lock()
_last_sync_ts = 0.0
MIN_SYNC_INTERVAL_S = 300

# How long to wait after the listener transitions to running before the
# first sync attempt. The listener subprocess needs a moment to settle
# (open the port, start streaming) before radio_session() tries to pause
# it and grab the port for a competing SerialInterface - attempting sync
# immediately after startup caused Serial port still busy contention
# and destabilized the listener (observed live on a cold service restart).
STARTUP_SYNC_DELAY_S = 30


def _get_node_time(interface):
    """Read the node's current Unix timestamp. Return None if unavailable.

    The installed meshtastic package (2.7.11) exposes no getter for the
    node's onboard clock — no Node.getTime()/get_time(), no admin
    get_time_request in admin_pb2.AdminMessage, and no `--get-time` CLI
    flag (only `--set-time`). NodeInfo/DeviceMetrics as parsed by this
    version also carry no live clock field. There is nothing real to read
    here, so this always returns None by design (not a stub to fill in
    later) and evaluate_drift() below already handles None as 'invalid'.
    """
    return None


def _set_node_time(interface, ts: int) -> bool:
    """Set the node's clock. Return True on success.

    Calls meshtastic.node.Node.setTime(timeSec) on interface.localNode
    (venv/lib/python3.13/site-packages/meshtastic/node.py:944). Confirmed
    by cross-referencing the CLI: `meshtastic --set-time [TIMESTAMP]`
    resolves to `interface.getNode(args.dest, ...).setTime(args.set_time)`
    in meshtastic/__main__.py:342, and for the local node args.dest
    defaults to the local node, i.e. the same call this function makes.

    setTime() internally calls self.ensureSessionKey() (may block briefly
    requesting a session key admin config if one isn't cached yet) and then
    sends a single AdminMessage(set_time_only=ts) via Node._sendAdmin().
    For interface.localNode specifically, _sendAdmin() passes
    onResponse=None (see node.py:955-957: onResponse is only set to
    self.onAckNak when `self != self.iface.localNode`), so there is no
    ACK/NAK wait for the local node - the call returns as soon as the
    packet has been handed to sendData(). No async response/callback to
    wait on for this path.
    """
    local_node = getattr(interface, "localNode", None)
    if local_node is None:
        return False
    local_node.setTime(int(ts))
    return True


def evaluate_drift(node_time, system_time) -> str:
    """Return 'skip' | 'sync_when_free' | 'sync_now' | 'invalid'.

    Currently unreachable from try_sync() — kept for when the library
    gains a getTime()-equivalent (see KNOWN LIMITATION note above)."""
    if node_time is None:
        return 'invalid'
    drift = abs(system_time - node_time)
    if node_time < 1_000_000 or node_time > system_time + NODE_TIME_INVALID_THRESHOLD_S:
        return 'invalid'
    if drift < DRIFT_SMALL_S:
        return 'skip'
    elif drift < DRIFT_LARGE_S:
        return 'sync_when_free'
    else:
        return 'sync_now'


def try_sync(interface, log_fn=None) -> str:
    """Entry point. Call inside an already-open radio_session(). Returns
    'synced' | 'skipped' | 'failed' | 'untrusted' | 'too_soon'."""
    global _last_sync_ts
    with _sync_lock:
        now = time.time()
        if now - _last_sync_ts < MIN_SYNC_INTERVAL_S:
            return 'too_soon'

    if not is_trusted():
        msg = "Node time sync skipped: system time is not trusted"
        if log_fn:
            log_fn(msg, level='WARNING')
        return 'untrusted'

    system_time = get_time_status()['utc']

    # NOTE: drift evaluation disabled — getTime() unavailable in
    # meshtastic 2.7.11. Once the library supports it, uncomment:
    # node_time = _get_node_time(interface)
    # decision  = evaluate_drift(node_time, system_time)
    # if decision == 'skip':
    #     return 'skipped'

    try:
        success = _set_node_time(interface, system_time)
        if success:
            with _sync_lock:
                _last_sync_ts = time.time()
            if log_fn:
                log_fn("Node time synchronized", level='INFO')
            return 'synced'
        else:
            if log_fn:
                log_fn("Node time sync failed: setTime returned False",
                       level='WARNING')
            return 'failed'

    except Exception as e:
        if log_fn:
            log_fn(f"Node time sync error: {e}", level='ERROR')
        return 'failed'
