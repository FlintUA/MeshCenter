"""Tests for meshsrv/attachments/commands.py (internal-rest-api.md §3.2 /
§3.4; Execution Plan Step 1.6A.1).

Covers the frozen/immutable `Command` model (rejects unknown kinds, freezes
the payload, never prints the payload - the "no token in repr/logs" rule),
`mint_command_id()`'s uniqueness/shape, and the bounded `CommandQueue`
(backpressure raises `CommandQueueFull`, drain raises `queue.Empty` when
idle, FIFO order). Pure stdlib - no Flask/SQLite/network, safe in CI.
"""

import queue

import pytest

from meshsrv.attachments.commands import (
    COMMAND_KINDS,
    COMMAND_QUEUE_MAXSIZE,
    Command,
    CommandQueue,
    CommandQueueFull,
    mint_command_id,
)


def _cmd(kind="attachment_create", payload=None, command_id="cmd-1", created_at=0.0):
    return Command(
        command_id=command_id,
        kind=kind,
        payload=payload if payload is not None else {"attachment_id": "att-1"},
        created_at=created_at,
    )


# --- Command model ----------------------------------------------------------

def test_command_kinds_is_exactly_the_twenty_three_enumerated_types():
    assert len(COMMAND_KINDS) == 23
    for expected in (
        "attachment_create", "attachment_retry", "attachment_download",
        "attachment_save", "attachment_reject", "attachment_cancel",
        "attachment_revoke", "attachment_delete_local_content",
        "attachment_import", "attachment_copy_code", "attachment_add_delivery",
        "contact_request_key", "contact_confirm", "contact_accept_key_change",
        "contact_reject_key_change", "provider_probe", "provider_register",
        "provider_update", "provider_set_default", "provider_remove",
        "provider_set_upload_token", "provider_clear_upload_token",
        "provider_check",
    ):
        assert expected in COMMAND_KINDS


def test_command_is_frozen():
    cmd = _cmd()
    with pytest.raises(AttributeError):
        cmd.kind = "provider_check"
    with pytest.raises(AttributeError):
        cmd.command_id = "other"


def test_command_rejects_unknown_kind():
    with pytest.raises(ValueError):
        _cmd(kind="attachment_frobnicate")


def test_command_payload_is_frozen_read_only():
    cmd = _cmd(payload={"a": 1, "b": 2})
    with pytest.raises(TypeError):
        cmd.payload["a"] = 99
    with pytest.raises(TypeError):
        cmd.payload["new"] = "x"


def test_command_repr_never_includes_payload_or_secrets():
    # The whole point of the custom repr: a provider_set_upload_token
    # command carries a raw secret; repr must not print any of it.
    secret = "super-secret-token-value"
    cmd = _cmd(
        kind="provider_set_upload_token",
        payload={"provider_id": "AbCdEfGhIjK", "upload_token": secret},
    )
    text = repr(cmd)
    assert secret not in text
    # The kind name legitimately contains the substring "upload_token", but
    # the payload *value* (and any payload key beyond the kind) must not.
    assert "AbCdEfGhIjK" not in text
    assert "command_id='cmd-1'" in text
    assert "kind='provider_set_upload_token'" in text


def test_mint_command_id_is_32_hex_and_unique():
    seen = {mint_command_id() for _ in range(1000)}
    assert len(seen) == 1000  # all distinct
    for cid in seen:
        assert len(cid) == 32
        assert all(ch in "0123456789abcdef" for ch in cid)


# --- CommandQueue -----------------------------------------------------------

def test_queue_is_bounded_and_raises_command_queue_full():
    q = CommandQueue(maxsize=2)
    q.put_nowait(_cmd(command_id="a"))
    q.put_nowait(_cmd(command_id="b"))
    with pytest.raises(CommandQueueFull):
        q.put_nowait(_cmd(command_id="c"))
    assert q.qsize() == 2


def test_queue_default_maxsize_is_the_named_constant():
    q = CommandQueue()
    assert q.qsize() == 0
    for i in range(COMMAND_QUEUE_MAXSIZE):
        q.put_nowait(_cmd(command_id=f"cmd-{i}"))
    with pytest.raises(CommandQueueFull):
        q.put_nowait(_cmd(command_id="overflow"))


def test_queue_drains_fifo_and_raises_empty_when_idle():
    q = CommandQueue(maxsize=4)
    q.put_nowait(_cmd(command_id="first"))
    q.put_nowait(_cmd(command_id="second"))

    assert q.get_nowait().command_id == "first"
    assert q.get_nowait().command_id == "second"
    with pytest.raises(queue.Empty):
        q.get_nowait()


def test_queue_does_not_block_on_put_nowait():
    # put_nowait must return immediately, never block - a full queue raises
    # rather than waiting for the worker to drain (the request thread may
    # never block on the worker, §3.2).
    q = CommandQueue(maxsize=1)
    q.put_nowait(_cmd(command_id="a"))
    # A second put on a full queue raises immediately (no deadlock).
    with pytest.raises(CommandQueueFull):
        q.put_nowait(_cmd(command_id="b"))
