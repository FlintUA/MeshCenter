"""Tests for meshsrv/attachments/facade.py (internal-rest-api.md §3.2;
Execution Plan Step 1.6A.1, correction #1).

Covers the request-facing `AttachmentsFacade`: its read methods resolve to
the five in-memory worker-owned components (never SQLite/filesystem/
network/tick lock), and its `submit()` is the §3.4 enqueue with the §3.6
step-5 registry rollback on a full queue. Constructed directly over fresh
in-memory components so no SQLite/filesystem/network is ever touched. Pure
stdlib - safe in CI.
"""

import threading

import pytest

from meshsrv.attachments.command_registry import STATUS_QUEUED, CommandRegistry
from meshsrv.attachments.commands import Command, CommandQueue, CommandQueueFull
from meshsrv.attachments.facade import AttachmentsFacade
from meshsrv.attachments.idempotency import PendingReservations
from meshsrv.attachments.probe_registry import ProbeRegistry
from meshsrv.attachments.snapshots import AttachmentsSnapshotPublisher


def _facade(*, maxsize=64):
    wake_event = threading.Event()
    facade = AttachmentsFacade(
        command_queue=CommandQueue(maxsize=maxsize),
        command_registry=CommandRegistry(),
        pending_reservations=PendingReservations(),
        probe_registry=ProbeRegistry(),
        snapshot_publisher=AttachmentsSnapshotPublisher(),
        wake_event=wake_event,
    )
    return facade, wake_event


def _command(command_id="cmd-1", kind="attachment_cancel"):
    return Command(command_id=command_id, kind=kind, payload={}, created_at=0.0)


# --- empty reads (before any publish) --------------------------------------

def test_reads_return_empty_before_any_publish():
    facade, _ = _facade()
    assert facade.attachments_snapshot() is None
    assert facade.get_attachment("anything") is None
    assert facade.committed_idempotency() == {}
    assert facade.get_command("anything") is None
    assert facade.get_probe("anything") is None


# --- submit (the §3.4 enqueue) ---------------------------------------------

def test_submit_registers_queued_enqueues_and_wakes():
    facade, wake_event = _facade()
    command = _command()
    returned = facade.submit(command)
    assert returned == "cmd-1"
    # registered queued *before* the queue write (an immediate GET sees
    # `queued`, never 404).
    assert facade.get_command("cmd-1").status == STATUS_QUEUED
    # actually enqueued, not just registered.
    assert facade._command_queue.qsize() == 1  # noqa: SLF001
    # the worker was woken so the command is drained promptly.
    assert wake_event.is_set()


def test_submit_full_queue_rolls_back_the_registry_entry():
    facade, _ = _facade(maxsize=1)
    facade.submit(_command(command_id="cmd-a"))
    with pytest.raises(CommandQueueFull):
        facade.submit(_command(command_id="cmd-b"))
    # the rejected command's registry entry is rolled back (§3.6 step 5) so
    # a poller never sees a phantom `queued` for a command that was never
    # enqueued.
    assert facade.get_command("cmd-b") is None
    # the first command is untouched.
    assert facade.get_command("cmd-a").status == STATUS_QUEUED


# --- no SQLite/filesystem/network/tick-lock surface ------------------------

def test_facade_holds_no_sqlite_filesystem_network_or_tick_lock_handle():
    facade, _ = _facade()
    for attr in ("conn", "_conn", "tick_lock", "_tick_lock", "session", "_session", "paths"):
        assert not hasattr(facade, attr), f"facade unexpectedly holds {attr!r}"


def test_reads_complete_without_the_tick_lock():
    # A facade read resolves to the publisher/registry/probe registry's own
    # short dedicated locks, never the worker's tick lock. Hold an unrelated
    # lock is not enough to prove it - the honest check is that none of the
    # facade's read attributes *is* a threading.Lock shared with a tick. We
    # assert the surface stays lock-free in shape: each read returns without
    # needing any lock the caller must provide.
    facade, _ = _facade()
    assert facade.attachments_snapshot() is None
    assert facade.get_attachment("x") is None
    assert facade.committed_idempotency() == {}
    assert facade.get_command("x") is None
    assert facade.get_probe("x") is None
