"""Tests for the outgoing-message status state machine in server.py:
add_message() (creation, status="pending" for the async send flow),
update_message_status() (pending -> sent/failed, in place - never a new
record), and reconcile_interrupted_sends() (fails out anything still
"pending" after a restart).

Deliberately does NOT drive this through api_chat.py's actual /api/send
Flask route or its send_queue/background send_worker thread (see
api/api_chat.py:register_chat_routes) - that thread is real (started
unconditionally at import time, see api_chat.py:264) and talks to an
actual SerialInterface once given a job, which would mean either mocking
serial I/O deeply or letting a background thread attempt a real open()
against the fake serial port from tests/conftest.py with no clean way to
synchronize on/observe the outcome from the test. The state-transition
primitives tested here are exactly what that route and worker are built
on top of (see api_chat.py's own calls to add_message()/
update_message_status()), so this covers the invariants task item 3a
actually asks about - "pending" creation, transition without duplication,
retry reusing the same record - without that risk.
"""


def test_add_message_with_pending_status_is_stored_as_pending(server_module):
    before = len(server_module.messages)
    msg = server_module.add_message(
        "me", "Me", "hello there", node_id="!820af75a", status="pending",
    )
    assert msg["status"] == "pending"
    assert len(server_module.messages) == before + 1
    assert server_module.messages[-1]["id"] == msg["id"]


def test_add_message_without_status_defaults_to_sent(server_module):
    # Messages created outside the async /api/send flow (rx, system,
    # waypoint notifications) are final immediately - see add_message()'s
    # own comment on the "status" field.
    msg = server_module.add_message("rx", "Flint TAP2", "hi", node_id="!820af75a")
    assert msg["status"] == "sent"


def test_update_message_status_transitions_in_place_without_duplicating(server_module):
    msg = server_module.add_message(
        "me", "Me", "will succeed", node_id="!820af75a", status="pending",
    )
    count_before = len(server_module.messages)

    updated = server_module.update_message_status(
        msg["id"], msg["chat_id"], "sent", packet_id=123456,
    )

    assert updated["status"] == "sent"
    assert updated["packet_id"] == 123456
    assert updated["id"] == msg["id"]
    # No new message record was created - the same one was mutated in place.
    assert len(server_module.messages) == count_before


def test_update_message_status_records_failure_with_error(server_module):
    msg = server_module.add_message(
        "me", "Me", "will fail", node_id="!820af75a", status="pending",
    )

    updated = server_module.update_message_status(
        msg["id"], msg["chat_id"], "failed", error="Radio busy",
    )

    assert updated["status"] == "failed"
    assert updated["error"] == "Radio busy"


def test_update_message_status_clears_stale_error_on_success(server_module):
    msg = server_module.add_message(
        "me", "Me", "retried message", node_id="!820af75a", status="pending",
    )
    server_module.update_message_status(msg["id"], msg["chat_id"], "failed", error="timeout")

    # A subsequent successful send (e.g. the user hit Retry) must clear the
    # stale error rather than leaving a "sent" bubble that still shows one.
    updated = server_module.update_message_status(msg["id"], msg["chat_id"], "sent")

    assert updated["status"] == "sent"
    assert "error" not in updated


def test_update_message_status_unknown_message_returns_none(server_module):
    result = server_module.update_message_status("does-not-exist", "!820af75a", "sent")
    assert result is None


def test_retry_reuses_the_same_message_record_not_a_new_one(server_module):
    # Mirrors exactly what api_chat.py's api_send_retry() route does with a
    # failed message: flips its status back to "pending" and re-queues the
    # same id - it never calls add_message() again. Asserting the message
    # count is unchanged and the id is preserved is the actual "retry does
    # not create a duplicate" invariant task item 3a asks about.
    msg = server_module.add_message(
        "me", "Me", "flaky send", node_id="!820af75a", status="pending",
    )
    server_module.update_message_status(msg["id"], msg["chat_id"], "failed", error="no ack")
    count_before_retry = len(server_module.messages)

    retried = server_module.update_message_status(msg["id"], msg["chat_id"], "pending")

    assert retried["id"] == msg["id"]
    assert retried["status"] == "pending"
    assert len(server_module.messages) == count_before_retry


def test_reconcile_interrupted_sends_fails_out_pending_messages(server_module):
    msg = server_module.add_message(
        "me", "Me", "in flight when the process died", node_id="!820af75a", status="pending",
    )

    server_module.reconcile_interrupted_sends()

    updated = next(m for m in server_module.messages if m["id"] == msg["id"])
    assert updated["status"] == "failed"
    assert updated["error_code"] == "interrupted_by_restart"


def test_reconcile_interrupted_sends_leaves_final_messages_untouched(server_module):
    sent_msg = server_module.add_message("me", "Me", "already sent", node_id="!820af75a", status="sent")
    failed_msg = server_module.add_message("me", "Me", "already failed", node_id="!820af75a", status="failed")

    server_module.reconcile_interrupted_sends()

    updated_sent = next(m for m in server_module.messages if m["id"] == sent_msg["id"])
    updated_failed = next(m for m in server_module.messages if m["id"] == failed_msg["id"])
    assert updated_sent["status"] == "sent"
    assert updated_failed["status"] == "failed"
    assert "error_code" not in updated_failed
