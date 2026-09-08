"""meshsrv/attachments/dispatch.py

The worker-side command dispatcher (internal-rest-api.md §3.2/§3.4;
Execution Plan Step 1.6A.1). MIT-licensed Core code - stdlib only, never
`meshtastic`, never Flask.

Correction #2 of Step 1.6A.1's second pass: command execution must be
extensible *without* arbitrary callable commands. `Command` stays a closed
typed enumeration (see `commands.COMMAND_KINDS`); the mapping from a
command *kind* to the worker-side function that actually performs the
domain work lives here, as an explicit dispatcher over a kind->handler
table. A handler is a plain `Callable[[Command], CommandOutcome]` - the
single place a future sub-stage (1.6A.3/1.6A.4/1.6A.5) wires a real
command (create_draft, begin_download, provider register, ...) into the
worker without touching the drain loop, the queue, or the registry.

The one hard rule this module enforces: a command whose kind has **no
registered handler** is not a crash and not a silent no-op - it becomes a
terminal FAILED `CommandOutcome` carrying the stable error code
`unsupported_command_kind`. This is the "never crash the worker" guarantee:
the worker's drain loop can hand the dispatcher any `Command` that passed
`Command.__post_init__`'s kind check and still get a terminal result for a
kind that is enumerated but not yet implemented (or whose handler failed
to register), instead of an unhandled `KeyError`/`NotImplementedError`
taking the worker thread down. The dispatcher itself is a thin, pure
mapping and deliberately does **not** catch exceptions a handler raises -
a raising handler propagates out of `dispatch()` and the worker's drain
loop catches it there (see `service._execute_command`), so the "one bad
command must not stop the tick" policy has exactly one owner.

Nothing here touches SQLite, the filesystem, or the network - it is pure
in-memory control flow, so it is safe to import and use from either the
worker thread or a test.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Mapping, Optional, Protocol

from meshsrv.attachments.commands import Command

# ---- stable error codes (additive, snake_case - §10) ----------------------

# A command whose kind is in the closed enumeration but has no handler
# registered in this dispatcher (e.g. a kind whose sub-stage has not yet
# wired its handler). Terminal FAILED, never a crash, never a silent no-op.
UNSUPPORTED_COMMAND_KIND = "unsupported_command_kind"

# A registered handler raised an exception the worker's drain loop caught.
# The worker records this as a terminal FAILED rather than letting the
# exception kill the tick. Distinct from the handler's *own* error codes so
# a poller can tell "the handler declined this command" apart from "the
# handler itself broke".
COMMAND_EXECUTION_FAILED = "command_execution_failed"

# The one legal shape for a `CommandOutcome`'s `error_code`: a non-empty
# snake_case token. Enforced in `CommandOutcome.__post_init__` so a handler
# cannot construct a failed outcome with an empty, whitespace, or
# non-snake_case code - such a shape is a handler bug, caught by the worker
# (which records `COMMAND_EXECUTION_FAILED`) rather than ever reaching a
# poller as an ambiguous empty/odd error_code.
_STABLE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclasses.dataclass(frozen=True)
class CommandOutcome:
    """The worker-side result of executing one command, returned by a
    handler and translated by the worker into a `CommandRegistry`
    transition. Exactly one of two shapes, encoded by `error_code`:

    - **succeeded** (`error_code is None`): `resource_id` is the primary
      domain id the command produced (`attachment_id` or `provider_id`,
      §7.9) or `None`; `result` is the safe JSON-native payload (no
      secrets/absolute paths/ciphertext - the handler's responsibility,
      §7.9) or `None`.
    - **failed** (`error_code is not None`): a stable snake_case code;
      `resource_id`/`result` are ignored (must be `None`).

    Frozen so a handler's return value can't be mutated after the fact by a
    later step of the worker's own bookkeeping."""

    resource_id: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        # Enforce the two legal shapes at construction (a caller can't build a
        # mixed/ambiguous one). success: error_code is None (resource_id/result
        # optional). failed: error_code is a non-empty snake_case token, and
        # resource_id/result must both be None (a failed command produces no
        # primary id and no result payload - §7.9). A failed shape violating
        # any of this is a handler bug: it raises here, which the worker's
        # dispatch try/except converts to COMMAND_EXECUTION_FAILED.
        if self.error_code is None:
            return
        if not self.error_code or not _STABLE_ERROR_CODE_RE.match(self.error_code):
            raise ValueError(f"failed error_code must be non-empty snake_case, got {self.error_code!r}")
        if self.resource_id is not None:
            raise ValueError("a failed outcome must not carry resource_id")
        if self.result is not None:
            raise ValueError("a failed outcome must not carry result")

    @classmethod
    def succeeded(cls, *, resource_id: Optional[str] = None, result: Optional[Mapping[str, Any]] = None) -> "CommandOutcome":
        return cls(resource_id=resource_id, result=result)

    @classmethod
    def failed(cls, error_code: str) -> "CommandOutcome":
        return cls(error_code=error_code)


class CommandHandler(Protocol):
    """A worker-side handler for one command kind. Receives the frozen
    `Command` (already validated on the request thread - its `payload` is a
    read-only mapping) and returns a `CommandOutcome`. A handler is a
    closure over whatever worker-owned collaborators it needs
    (`conn`/`workspace_manager`/`principal`/`provider_registry`/
    `key_exchange`/`connectivity_monitor`/...), so it performs the *real*
    domain work (the only executor of it, §3.2) while the dispatcher stays
    a pure kind->callable table.

    May raise: the worker's drain loop catches any exception and records a
    terminal FAILED (`COMMAND_EXECUTION_FAILED`) rather than crashing the
    tick - so a handler that cannot complete its domain work (e.g. a Relay
    unreachable at execution time) may either return `CommandOutcome.failed`
    with its own stable code, or raise and let the worker record the generic
    one."""

    def __call__(self, command: Command) -> CommandOutcome: ...


class CommandDispatcher:
    """Maps each command kind to its worker-side handler (§3.2). Pure and
    thread-safe by construction (an immutable `dict` snapshot built once at
    construction, never mutated) - no lock, no SQLite, no I/O.

    `dispatch()` is the worker's single entry point: look up the handler
    for `command.kind` and call it. A kind with no handler returns
    `CommandOutcome.failed(UNSUPPORTED_COMMAND_KIND)` - the stable, terminal
    "not yet implemented / not registered" result, never a raise, so an
    enumerated-but-unwired kind can never crash the worker."""

    def __init__(self, handlers: Mapping[str, CommandHandler]):
        # A dict copy: the caller's mapping may be mutated after this, and
        # a frozen snapshot keeps dispatch()'s lookup deterministic.
        self._handlers: "dict[str, CommandHandler]" = dict(handlers)

    def handler_for(self, kind: str) -> Optional[CommandHandler]:
        """The registered handler for `kind`, or `None` if none is wired."""
        return self._handlers.get(kind)

    def dispatch(self, command: Command) -> CommandOutcome:
        """Execute `command` via its kind's handler, or return the stable
        `unsupported_command_kind` failure for an unwired kind. Does **not**
        catch a handler's own exception - that propagates to the worker's
        drain loop, which owns the catch-and-mark-failed policy."""
        handler = self._handlers.get(command.kind)
        if handler is None:
            return CommandOutcome.failed(UNSUPPORTED_COMMAND_KIND)
        return handler(command)

    def supported_kinds(self) -> frozenset:
        """The kinds this dispatcher can actually execute (a subset of
        `commands.COMMAND_KINDS`). For tests and future introspection."""
        return frozenset(self._handlers)
