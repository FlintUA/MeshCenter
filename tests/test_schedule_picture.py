"""G3: the schedule action "send camera picture" - pure logic with a fake clock and fake collaborators."""
import pytest

from meshsrv import schedule_engine, schedule_picture
from meshsrv.schedule_picture import MIN_INTERVAL_S, PictureDeps, PictureError, RuleInvalid


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class World:
    """Fake camera, radio and contact; records what the action did."""

    def __init__(self):
        self.clock = Clock()
        self.radio = True
        self.trusted = {"!b0f14d2a"}
        self.sources = {"cam1"}
        self.take_error = None
        self.create_error = None
        self.taken, self.prepared, self.sent = [], [], []
        self.logs, self.notes = [], []

    def deps(self):
        return PictureDeps(
            clock=self.clock,
            radio_available=lambda: self.radio,
            source_exists=lambda sid: sid in self.sources,
            contact_trusted=lambda cid: cid in self.trusted,
            take_picture=self._take,
            prepare_copy=self._prepare,
            create_transfer=self._create,
            log=lambda *a: self.logs.append(a),
            notify=lambda *a: self.notes.append(a),
        )

    def _take(self, source_id):
        if self.take_error:
            raise PictureError(self.take_error)
        self.taken.append(source_id)
        return {"filename": "2026/10/31/MC_cam1.jpg", "display_name": "MC_cam1.jpg"}

    def _prepare(self, filename, size_key):
        self.prepared.append((filename, size_key))
        return b"jpeg-bytes"

    def _create(self, contact_id, name, data):
        if self.create_error:
            raise PictureError(self.create_error)
        self.sent.append((contact_id, name, data))


def action(**params):
    base = {"source_id": "cam1", "contact_id": "!b0f14d2a"}
    base.update(params)
    return {"type": "send_camera_picture", "params": base}


RULE = {"id": "r1", "label": "Garden"}


def test_sends_a_resized_picture_by_default():
    w = World()
    a = action()
    assert schedule_picture.run(a, RULE, w.deps()) == "sent"
    assert w.taken == ["cam1"]
    assert w.prepared == [("2026/10/31/MC_cam1.jpg", "1280")]
    assert w.sent == [("!b0f14d2a", "MC_cam1.jpg", b"jpeg-bytes")]
    assert a["state"]["last_sent_at"] == w.clock.now
    assert w.logs[-1][0] == "INFO" and w.notes == []


def test_resize_choice_is_used():
    w = World()
    schedule_picture.run(action(resize="original"), RULE, w.deps())
    assert w.prepared[0][1] == "original"


def test_second_run_inside_fifteen_minutes_is_refused_at_run_time():
    w = World()
    a = action()
    assert schedule_picture.run(a, RULE, w.deps()) == "sent"
    w.clock.now += MIN_INTERVAL_S - 60
    assert schedule_picture.run(a, RULE, w.deps()) == "too_soon"
    assert len(w.sent) == 1 and len(w.taken) == 1, "a refused run never touches the camera"
    assert w.notes and w.notes[-1][0] == "warning"
    w.clock.now += 60
    assert schedule_picture.run(a, RULE, w.deps()) == "sent"
    assert len(w.sent) == 2


def test_a_fifteen_minute_rule_is_not_refused_by_sub_second_tick_skew():
    # live check on .104: the second run came 899.x s after the first and was wrongly refused
    w = World()
    a = action()
    assert schedule_picture.run(a, RULE, w.deps()) == "sent"
    w.clock.now += MIN_INTERVAL_S - 0.9
    assert schedule_picture.run(a, RULE, w.deps()) == "sent"
    w.clock.now += MIN_INTERVAL_S - schedule_picture.TICK_TOLERANCE_S - 1
    assert schedule_picture.run(a, RULE, w.deps()) == "too_soon"


def test_a_failed_attempt_also_counts_so_a_broken_setup_cannot_hammer():
    w = World()
    a = action()
    w.radio = False
    assert schedule_picture.run(a, RULE, w.deps()) == "radio_unavailable"
    w.radio = True
    w.clock.now += 60
    assert schedule_picture.run(a, RULE, w.deps()) == "too_soon"


@pytest.mark.parametrize("break_it, code", [
    (lambda w: setattr(w, "radio", False), "radio_unavailable"),
    (lambda w: w.trusted.clear(), "key_unavailable"),
    (lambda w: w.sources.clear(), "camera_unavailable"),
    (lambda w: setattr(w, "take_error", "camera_off"), "camera_off"),
    (lambda w: setattr(w, "take_error", "auth_failed"), "auth_failed"),
    (lambda w: setattr(w, "create_error", "command_queue_full"), "command_queue_full"),
])
def test_missing_camera_radio_or_key_skips_with_one_log_line_and_one_notification(break_it, code):
    w = World()
    break_it(w)
    assert schedule_picture.run(action(), RULE, w.deps()) == code
    assert len(w.logs) == 1 and w.logs[0][0] == "WARNING" and code in w.logs[0][2]
    assert len(w.notes) == 1 and code in w.notes[0][2]
    assert w.sent == []


def test_nothing_is_taken_when_the_radio_or_key_is_missing():
    w = World()
    w.trusted.clear()
    schedule_picture.run(action(), RULE, w.deps())
    assert w.taken == [], "no picture is saved to the gallery for a send that cannot happen"


# ---- validation ----------------------------------------------------------------------------------------------------

def rule(trigger=None, act=None):
    return {"trigger": trigger or {"mode": "daily"}, "actions": [act or action()]}


def test_validate_accepts_a_good_rule_and_ignores_other_actions():
    schedule_picture.validate_rule(rule())
    schedule_picture.validate_rule({"trigger": {"mode": "interval", "interval_minutes": 1},
                                    "actions": [{"type": "mesh_send", "params": {}}]})


@pytest.mark.parametrize("minutes, ok", [(14, False), (15, True), (60, True), ("x", False), (0, False)])
def test_interval_below_fifteen_minutes_is_refused_on_save(minutes, ok):
    r = rule({"mode": "interval", "interval_minutes": minutes})
    if ok:
        schedule_picture.validate_rule(r)
    else:
        with pytest.raises(RuleInvalid) as info:
            schedule_picture.validate_rule(r)
        assert info.value.code == "interval_too_short"


@pytest.mark.parametrize("params, code", [
    ({"source_id": ""}, "camera_required"),
    ({"contact_id": ""}, "contact_required"),
    ({"contact_id": "Echo"}, "contact_required"),
    ({"resize": "800"}, "invalid_resize"),
])
def test_incomplete_picture_actions_are_refused(params, code):
    with pytest.raises(RuleInvalid) as info:
        schedule_picture.validate_rule(rule(act=action(**params)))
    assert info.value.code == code


def test_engine_refuses_to_save_and_normalizes_the_params(tmp_path):
    schedule_engine.configure_storage(tmp_path)
    with pytest.raises(RuleInvalid):
        schedule_engine.create_rule(rule({"mode": "interval", "interval_minutes": 5}))
    created = schedule_engine.create_rule(rule({"mode": "interval", "interval_minutes": 30}, action(contact_id="!B0F14D2A")))
    assert created["actions"][0]["params"]["contact_id"] == "!b0f14d2a"
    assert created["actions"][0]["params"]["resize"] == "1280"
    with pytest.raises(RuleInvalid):
        schedule_engine.update_rule(created["id"], rule({"mode": "interval", "interval_minutes": 14}))
    assert schedule_engine.get_all_rules()[0]["trigger"]["interval_minutes"] == 30, "a refused update changes nothing"


def test_run_state_survives_in_the_saved_rule(server_module, tmp_path, monkeypatch):
    schedule_engine.configure_storage(tmp_path)
    schedule_engine.create_rule(rule({"mode": "interval", "interval_minutes": 30}))
    w = World()
    monkeypatch.setattr(schedule_engine, "is_trusted", lambda: True)
    from meshsrv import schedule_actions
    schedule_actions.configure(picture_deps=w.deps())
    try:
        w.clock.now = 5_000_000.0
        monkeypatch.setattr(schedule_engine.time, "time", lambda: 5_000_000.0)
        schedule_engine._tick()
        assert len(w.sent) == 1
        saved = schedule_engine.get_all_rules()[0]
        assert saved["actions"][0]["state"]["last_run_at"] == 5_000_000.0
        assert saved["trigger"]["last_fired_at"] == 5_000_000
    finally:
        schedule_actions.configure()
