"""Tests for meshsrv/attachments/dispatch.py (internal-rest-api.md §3.2/§3.4;
Execution Plan Step 1.6A.1, correction #2).

Covers the worker-side command dispatcher: the frozen `CommandOutcome`
(succeeded/failed shapes), the `CommandHandler` protocol being a plain
callable, and `CommandDispatcher`'s core guarantee - an enumerated-but-
unwired kind becomes a terminal `unsupported_command_kind` failure, never
a crash, while a registered kind dispatches to its handler and a raising
handler propagates (the worker's drain loop, not the dispatcher, owns
catch-and-mark-failed). Pure stdlib - no Flask/SQLite/network, safe in CI.
"""

import pytest

from meshsrv.attachments.commands import COMMAND_KINDS, Command
from meshsrv.attachments.dispatch import (
    COMMAND_EXECUTION_FAILED,
    UNSUPPORTED_COMMAND_KIND,
    CommandDispatcher,
    CommandOutcome,
)


def _command(kind="attachment_cancel", command_id="cmd-1"):
    return Command(command_id=command_id, kind=kind, payload={}, created_at=0.0)


# --- CommandOutcome ---------------------------------------------------------

def test_outcome_succeeded_has_no_error_code():
    outcome = CommandOutcome.succeeded(resource_id="att-1", result={"action": "created"})
    assert outcome.error_code is None
    assert outcome.resource_id == "att-1"
    assert outcome.result == {"action": "created"}


def test_outcome_failed_has_only_error_code():
    outcome = CommandOutcome.failed("some_error")
    assert outcome.error_code == "some_error"
    assert outcome.resource_id is None
    assert outcome.result is None


def test_outcome_is_frozen():
    outcome = CommandOutcome.succeeded()
    with pytest.raises(AttributeError):
        outcome.error_code = "mutated"


# --- CommandDispatcher: unwired kind ---------------------------------------

def test_unwired_kind_returns_terminal_unsupported_failure():
    dispatcher = CommandDispatcher({})
    outcome = dispatcher.dispatch(_command(kind="attachment_cancel"))
    assert outcome.error_code == UNSUPPORTED_COMMAND_KIND
    assert outcome.resource_id is None
    assert outcome.result is None


def test_every_enumerated_kind_is_unsupported_on_an_empty_dispatcher():
    # The closed enumeration, dispatched through an empty handler table,
    # must produce a terminal failure for *every* kind - never a KeyError
    # or NotImplementedError - so an enumerated-but-unwired kind can never
    # crash the worker.
    dispatcher = CommandDispatcher({})
    for kind in COMMAND_KINDS:
        outcome = dispatcher.dispatch(_command(kind=kind))
        assert outcome.error_code == UNSUPPORTED_COMMAND_KIND


# --- CommandDispatcher: wired kind ------------------------------------------

def test_wired_kind_dispatches_to_its_handler():
    calls = []

    def handler(command):
        calls.append(command.command_id)
        return CommandOutcome.succeeded(resource_id="att-9")

    dispatcher = CommandDispatcher({"attachment_cancel": handler})
    outcome = dispatcher.dispatch(_command(kind="attachment_cancel", command_id="cmd-7"))
    assert calls == ["cmd-7"]
    assert outcome.error_code is None
    assert outcome.resource_id == "att-9"


def test_handler_receives_the_exact_command():
    seen = {}

    def handler(command):
        seen["command"] = command
        return CommandOutcome.succeeded()

    command = _command(kind="attachment_cancel", command_id="cmd-3")
    CommandDispatcher({"attachment_cancel": handler}).dispatch(command)
    assert seen["command"] is command


def test_handler_for_and_supported_kinds():
    def handler(command):
        return CommandOutcome.succeeded()

    dispatcher = CommandDispatcher({"attachment_cancel": handler})
    assert dispatcher.handler_for("attachment_cancel") is handler
    assert dispatcher.handler_for("provider_probe") is None
    assert dispatcher.supported_kinds() == frozenset({"attachment_cancel"})


def test_dispatcher_snapshots_the_handler_mapping_at_construction():
    # A caller mutating its own mapping after construction must not change
    # the dispatcher's already-built table.
    handlers = {"attachment_cancel": lambda command: CommandOutcome.succeeded()}
    dispatcher = CommandDispatcher(handlers)
    handlers["provider_probe"] = lambda command: CommandOutcome.succeeded()
    assert dispatcher.handler_for("provider_probe") is None


# --- raising handler --------------------------------------------------------

def test_a_raising_handler_propagates_out_of_dispatch():
    # The dispatcher is a thin mapping and must NOT swallow a handler's
    # exception - the worker's drain loop (service._execute_command) owns
    # the catch-and-mark-failed policy, so the dispatcher never masks a
    # real bug as a silent failure.
    def handler(command):
        raise RuntimeError("handler bug")

    dispatcher = CommandDispatcher({"attachment_cancel": handler})
    with pytest.raises(RuntimeError, match="handler bug"):
        dispatcher.dispatch(_command(kind="attachment_cancel"))


def test_command_execution_failed_code_is_distinct_and_stable():
    # The worker records this code for a handler that raised; it must be a
    # stable snake_case code distinct from unsupported_command_kind so a
    # poller can tell "unwired kind" from "handler broke".
    assert COMMAND_EXECUTION_FAILED == "command_execution_failed"
    assert COMMAND_EXECUTION_FAILED != UNSUPPORTED_COMMAND_KIND
