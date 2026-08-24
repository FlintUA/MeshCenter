"""
MeshCenter Schedule Engine
Background ticker, fires once a minute, executes rules from schedules.json.
"""
import json
import time
import uuid
import threading
from pathlib import Path

from meshsrv.time_service import is_trusted, get_status as get_time_status
from meshsrv.notification_service import push_notification

SCHEDULES_FILE = Path('data/schedules.json')
_lock          = threading.Lock()
_running       = False

DAY_MAP = {0: 'mon', 1: 'tue', 2: 'wed', 3: 'thu', 4: 'fri', 5: 'sat', 6: 'sun'}


def _load() -> list:
    if not SCHEDULES_FILE.exists():
        SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULES_FILE.write_text('[]')
        return []
    try:
        data = json.loads(SCHEDULES_FILE.read_text())
        return [_normalize(r) for r in data] if isinstance(data, list) else []
    except Exception as e:
        print(f"[Schedule] Load error: {e}", flush=True)
        return []


def _save(rules: list):
    SCHEDULES_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=2))


def _normalize(r: dict) -> dict:
    r.setdefault('id', str(uuid.uuid4()))
    r.setdefault('enabled', True)
    r.setdefault('label', '')
    t = r.setdefault('trigger', {})
    t.setdefault('type', 'schedule')
    t.setdefault('mode', 'daily')
    t.setdefault('days', ['mon', 'tue', 'wed', 'thu', 'fri'])
    t.setdefault('time', '08:00')
    t.setdefault('interval_minutes', 60)
    t.setdefault('datetime', '')
    t.setdefault('last_fired_at', 0)
    r.setdefault('actions', [{'type': 'log_entry', 'params': {}}])
    n = r.setdefault('notify', {})
    n.setdefault('enabled', False)
    n.setdefault('signal', '')
    n.setdefault('details', '')
    m = n.setdefault('mesh_message', {})
    m.setdefault('enabled', False)
    m.setdefault('target_type', 'node')
    m.setdefault('node_id', '')
    m.setdefault('channel_index', 0)
    return r


def _should_fire(rule: dict, now_ts: int) -> bool:
    trigger = rule.get('trigger', {})
    mode = trigger.get('mode', 'daily')

    if mode == 'daily':
        import datetime as dt
        tz_name = get_time_status().get('timezone', 'UTC')
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
        now = dt.datetime.now(tz) if tz else dt.datetime.now()
        h, m = map(int, trigger.get('time', '00:00').split(':'))
        today = DAY_MAP.get(now.weekday(), '')
        if now.hour != h or now.minute != m:
            return False
        last = trigger.get('last_fired_at', 0)
        if now_ts - last < 50:
            return False
        return today in trigger.get('days', [])

    elif mode == 'interval':
        interval_s = trigger.get('interval_minutes', 60) * 60
        last = trigger.get('last_fired_at', 0)
        if last == 0:
            return True
        return (now_ts - last) >= interval_s

    elif mode == 'once':
        target_dt_str = trigger.get('datetime', '')
        if not target_dt_str:
            return False
        last = trigger.get('last_fired_at', 0)
        if last > 0:
            return False  # already fired
        import datetime as dt
        tz_name = get_time_status().get('timezone', 'UTC')
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
        try:
            target = dt.datetime.fromisoformat(target_dt_str)
        except ValueError:
            return False
        if tz and target.tzinfo is None:
            target = target.replace(tzinfo=tz)
        now = dt.datetime.now(tz) if tz else dt.datetime.now()
        # Fire when the current minute matches the target minute.
        return (now.year == target.year and now.month == target.month and
                now.day == target.day and now.hour == target.hour and
                now.minute == target.minute)

    return False


def _execute_actions(rule: dict):
    from meshsrv.schedule_actions import run_action
    for action in rule.get('actions', []):
        try:
            run_action(action, rule)
        except Exception as e:
            print(f"[Schedule] Action error in '{rule.get('label')}': {e}", flush=True)
            push_notification(
                level='error', source='schedule_engine',
                title=rule.get('label', 'Schedule'),
                body=f"Execution error: {e}"
            )


def _execute_notify(rule: dict):
    n = rule.get('notify', {})
    if not n.get('enabled'):
        return

    title = rule.get('label') or 'Schedule'
    details = n.get('details') or n.get('signal') or ''

    push_notification(level='info', source='schedule_engine', title=title, body=details)

    action_types = [a.get('type') for a in rule.get('actions', [])]
    already_mesh = 'mesh_send' in action_types or 'send_data_report' in action_types

    m = n.get('mesh_message', {})
    if m.get('enabled') and not already_mesh:
        signal = n.get('signal', '').strip()
        if signal:
            from meshsrv.schedule_actions import send_mesh_message
            send_mesh_message(
                message=signal,
                target_type=m.get('target_type', 'node'),
                node_id=m.get('node_id', ''),
                channel_index=m.get('channel_index', 0)
            )


def _tick():
    if not is_trusted():
        return

    now_ts = int(time.time())
    with _lock:
        rules = _load()
        changed = False

        for rule in rules:
            if not rule.get('enabled'):
                continue
            if not _should_fire(rule, now_ts):
                continue

            label = rule.get('label', rule.get('id', '?'))
            print(f"[Schedule] Firing: '{label}'", flush=True)

            # NOTE on holding _lock across action execution: see the
            # docstring on start() below for the reasoning. Kept as-is for
            # this MVP - rules fire at most once/minute and the schedules
            # API is used interactively/infrequently, so a multi-second
            # stall on create/update/toggle/delete during a mesh-sending
            # tick is judged an acceptable trade for not having to build a
            # second synchronization mechanism (e.g. snapshot-then-execute-
            # outside-lock, which would reopen a race between "rule read for
            # execution" and "rule concurrently edited/deleted via the API")
            # for an MVP feature.
            _execute_actions(rule)
            _execute_notify(rule)

            rule['trigger']['last_fired_at'] = now_ts
            if rule['trigger'].get('mode') == 'once':
                rule['enabled'] = False
                print(f"[Schedule] Once rule '{label}' fired and disabled", flush=True)
            changed = True

        if changed:
            _save(rules)


def _loop():
    while _running:
        try:
            _tick()
        except Exception as e:
            print(f"[Schedule] Ticker error: {e}", flush=True)
        time.sleep(60 - time.time() % 60 + 0.5)


def start(nodes=None, state_lock=None, radio_transport=None,
          is_radio_available=None, LOCAL_NODE_ID=None,
          add_message=None, LOCAL_NODE_NAME=None, CHANNEL_CHAT_ID=None):
    """Start the schedule engine background thread.

    The optional keyword arguments are dependency-injected references into
    server.py's shared state, following the same DI-by-parameter-list
    convention used by api/*.register_*_routes(...) elsewhere in this
    codebase (see api/api_chat.py's register_chat_routes and
    api/api_node_tools.py's register_node_tools_routes). meshsrv/*.py
    modules never do `from server import ...` - server.py imports FROM
    meshsrv, so a reverse import would risk a circular import. Passing the
    handful of objects schedule_actions.py needs (nodes/state_lock for
    reading node telemetry, radio_transport/is_radio_available for sending
    mesh messages, LOCAL_NODE_ID as the default telemetry source,
    add_message/LOCAL_NODE_NAME/CHANNEL_CHAT_ID so a successful mesh_send/
    send_data_report also writes a local kind="me" chat-history record,
    mirroring what api/api_chat.py's send worker does) at start() call time
    avoids that entirely, exactly like node_time_sync.py avoids it by
    receiving an already-open `interface` as a parameter instead of
    importing server.py to get one.

    Task 44: radio_session/get_meshtastic_port/RadioBusyError (Serial-
    specific, direct-SerialInterface concepts) were replaced by a single
    `radio_transport` (RadioTransport instance, see
    meshsrv/radio_transport.py) - schedule_actions.py no longer imports
    `meshtastic` at all, sending through radio_transport.send_text()
    instead of opening its own SerialInterface.

    IMPORTANT DESIGN NOTE - _lock held during action execution:
    _tick() acquires the module-level threading.Lock() `_lock` and holds it
    for the entire duration of _execute_actions()/_execute_notify() for
    every rule that fires this minute. mesh_send / send_data_report actions
    go through send_mesh_message(), which uses radio_session() + a
    short-lived SerialInterface - per api/api_chat.py's own comments this
    reliably takes several seconds (pause listener, wait for serial
    release, connect, send, cooldown, resume listener). While that is
    happening, any concurrent call to create_rule/update_rule/toggle_rule/
    delete_rule (all of which also acquire `_lock`) blocks for the same
    duration - e.g. the Settings UI's schedule list could feel stuck for a
    few seconds if a user edits a schedule at the exact moment another one
    fires and sends over mesh.
    DECISION: kept as-is for this MVP. Schedules fire at most once/minute,
    the schedule API is used interactively and infrequently (not from a
    hot path), and a few seconds of occasional latency on those endpoints
    is judged an acceptable trade against the complexity of restructuring
    to release the lock before running actions (which would need a
    snapshot-then-execute-outside-lock split and reopens a race between
    "the rule that's about to run" and "the same rule being concurrently
    edited or deleted via the API").
    """
    global _running
    if _running:
        return
    _running = True

    from meshsrv.schedule_actions import configure as _configure_actions
    _configure_actions(
        nodes=nodes,
        state_lock=state_lock,
        radio_transport=radio_transport,
        is_radio_available=is_radio_available,
        LOCAL_NODE_ID=LOCAL_NODE_ID,
        add_message=add_message,
        LOCAL_NODE_NAME=LOCAL_NODE_NAME,
        CHANNEL_CHAT_ID=CHANNEL_CHAT_ID,
    )

    t = threading.Thread(target=_loop, daemon=True, name='schedule-engine')
    t.start()
    print("[Schedule] Engine started", flush=True)


def get_all_rules() -> list:
    with _lock:
        return _load()


def create_rule(data: dict) -> dict:
    rule = _normalize(data)
    rule['id'] = str(uuid.uuid4())
    with _lock:
        rules = _load()
        rules.append(rule)
        _save(rules)
    return rule


def update_rule(rule_id: str, data: dict):
    with _lock:
        rules = _load()
        for i, r in enumerate(rules):
            if r['id'] == rule_id:
                data['id'] = rule_id
                rules[i] = _normalize(data)
                _save(rules)
                return rules[i]
    return None


def toggle_rule(rule_id: str):
    with _lock:
        rules = _load()
        for r in rules:
            if r['id'] == rule_id:
                r['enabled'] = not r['enabled']
                _save(rules)
                return r
    return None


def delete_rule(rule_id: str) -> bool:
    with _lock:
        rules = _load()
        new = [r for r in rules if r['id'] != rule_id]
        if len(new) == len(rules):
            return False
        _save(new)
    return True
