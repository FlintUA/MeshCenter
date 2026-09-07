"""meshsrv/attachments/service.py

ADR-0008 decision 1 (Step 1.6A backend layer): AttachmentsService, the
single background worker that drives every non-terminal attachment
forward - this is what api_attachments.py's handlers will call wake()
into, once Step 1.6A's REST layer exists. This module deliberately does
NOT introduce a job queue on top of the existing (and, per ADR-0008's own
audit, never written-to) `mca_jobs` table: each tick is a direct SQL scan
of `attachments` for the rows sender.py's/receiver.py's own
AUTOMATIC_STATES sets say can be advanced without waiting for an external
event, followed by one sender.run_step()/receiver.run_step() call per row
- the tick body *is* the reconciliation pass, just run repeatedly.

One instance per workspace, constructed once at server startup alongside
ProviderRegistry/KeyExchangeCoordinator/ConnectivityMonitor (the same
"long-lived instance held for the process's life" shape used throughout
this codebase). A single daemon thread per workspace processes
attachments strictly serially inside one `threading.Lock`-held tick -
MeshCenter is single-process (ADR-0003), so no SQLite lease is needed,
and the design spec's "at most one crypto worker"/"at most two concurrent
transfers" requirements are satisfied trivially, since the real
concurrency ceiling here is 1 by construction (no thread pool).

API handlers must never call run_step() or touch the Relay/radio
themselves (ADR-0008): they validate input, make a cheap synchronous
domain-layer call (create_draft(), begin_download(), reject(), cancel()
...), call wake(), and return 202 Accepted. All the slow encrypt/upload/
download/decrypt/verify work happens on this module's own thread, never
on a Flask request thread.

`attachments.provider_id` is stored as Base64URL text for both
directions (the form `ProviderRegistry` keys rows by) - 'received' rows
always were (receiver.py, ADR-0007); 'sent' rows used to be stored as hex
instead (a reviewer-found defect: `ProviderRegistry.remove_or_disable()`'s
"is this provider still referenced?" check compares against the
Base64URL form, so it never matched a 'sent' row and could delete a
profile a real outgoing attachment still depended on - reproduced
locally). Fixed at the source in `sender.py` (ADR-0008-hardening), with
Migration 9 re-encoding any row a pre-fix process already wrote as hex.
`_provider_id_text()` below is now a thin, defensive pass-through kept
for the two call sites' clarity - see its own docstring for why it isn't
simply inlined.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional

from meshsrv.attachments import receiver, sender
from meshsrv.attachments.delivery.base import DeliveryAdapter
from meshsrv.attachments.identity import MCAPrincipal
from meshsrv.attachments.key_exchange import AddressStatus, KeyExchangeCoordinator
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay_client import RelayClient
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor

logger = logging.getLogger(__name__)

# Periodic safety-net cadence (module docstring: wake() is the normal
# trigger: a new draft, an inbound message, a user action, a
# ConnectivityMonitor transition - this is only the fallback for
# anything that didn't call wake(), e.g. a retry_at deadline elapsing).
DEFAULT_TICK_SECONDS = 5.0

# Per ADR-0008: a config constant, not a hard architectural limit - the
# real concurrency ceiling is 1 regardless (attachments are processed
# strictly serially within one locked tick), this only bounds how much
# work one tick takes before yielding back to the wake-driven loop.
MAX_ATTACHMENTS_PER_TICK = 8

RelayClientFactory = Callable[[str], Optional[RelayClient]]


class AttachmentsServiceError(RuntimeError):
    """Base class for this module's errors."""


def _provider_id_text(direction: str, provider_id_column: Optional[str]) -> Optional[str]:
    """Both directions store the same Base64URL encoding on disk now
    (module docstring) - this is a thin pass-through, not a real
    normalization step, kept only so both call sites below read the same
    way regardless of direction and so a future re-introduction of a
    per-direction difference has one obvious place to fix instead of two
    call sites drifting apart again. `direction` is accepted but
    currently unused - a defensive signature, not dead weight: a caller
    passing the wrong direction for a row would be a bug worth being able
    to assert on later, not something to silently ignore. Returns None
    unchanged: a draft can reach VALIDATING/ENCRYPTING before a relay
    lookup is ever needed."""
    return provider_id_column or None


class AttachmentsService:
    """See module docstring. Construct once per workspace with its
    already-constructed collaborators (this module builds none of them
    itself, matching `ConnectivityMonitor`'s own "takes a `ProviderRegistry`,
    builds nothing else" shape)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_manager: MCAWorkspaceManager,
        principal: MCAPrincipal,
        provider_registry: ProviderRegistry,
        key_exchange: KeyExchangeCoordinator,
        connectivity_monitor: ConnectivityMonitor,
        delivery_adapter: Optional[DeliveryAdapter] = None,
        relay_client_factory: Optional[RelayClientFactory] = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        max_per_tick: int = MAX_ATTACHMENTS_PER_TICK,
        now_fn=time.time,
    ):
        self._conn = conn
        self._workspace_manager = workspace_manager
        self._principal = principal
        self._provider_registry = provider_registry
        self._key_exchange = key_exchange
        self._connectivity = connectivity_monitor
        self._delivery_adapter = delivery_adapter
        self._relay_client_factory = relay_client_factory or self._default_relay_client
        self._tick_seconds = tick_seconds
        self._max_per_tick = max_per_tick
        self._now = now_fn

        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Idempotent: calling start() on an already-running service is a
        no-op, not a second thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="mca-attachments-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: Optional[float] = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def wake(self) -> None:
        """The only method API handlers (once Step 1.6A exists) are meant
        to call after a domain-layer action. Never blocks, never touches
        the database or network - just flags the worker thread's wait()
        to return early."""
        self._wake_event.set()

    def _run(self) -> None:
        logger.info("AttachmentsService worker started (workspace_id=%s)", self._principal.workspace_id)
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self._tick_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the worker thread must never die from one bad tick
                logger.exception("AttachmentsService tick failed")

    # ---- one tick ---------------------------------------------------------

    def tick(self) -> int:
        """One reconciliation pass: up to `max_per_tick` non-terminal
        attachments, one run_step() call each. Safe to call directly (a
        synchronous pass at startup right after resume_pending()/
        reconcile_pending(), or from a test) as well as from the worker
        thread - the lock serializes concurrent callers instead of
        racing them, matching "one crypto worker" (ADR-0008)."""
        with self._lock:
            return self._tick_locked()

    def _tick_locked(self) -> int:
        # The only network I/O this tick performs itself - everything
        # after this is either a cheap DB scan or a run_step() call,
        # which does its own I/O only when a row is actually due.
        self._connectivity.refresh()

        processed = 0
        for row in self._due_rows():
            try:
                if row["direction"] == "sent":
                    self._step_sent(row)
                else:
                    self._step_received(row)
            except Exception:  # noqa: BLE001 - one bad attachment must not stop the whole tick
                logger.exception(
                    "AttachmentsService: run_step failed for attachment %s (direction=%s)",
                    row["id"],
                    row["direction"],
                )
            processed += 1
        return processed

    def _due_rows(self) -> List[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        sent_placeholders = ",".join("?" for _ in sender.AUTOMATIC_STATES)
        received_placeholders = ",".join("?" for _ in receiver.AUTOMATIC_STATES)
        return self._conn.execute(
            f"""
            SELECT id, direction, provider_id FROM attachments
            WHERE workspace_id = ? AND (
                (direction = 'sent' AND state IN ({sent_placeholders}))
                OR (direction = 'received' AND state IN ({received_placeholders}))
            )
            ORDER BY created_at
            LIMIT ?
            """,
            (
                self._principal.workspace_id,
                *sender.AUTOMATIC_STATES,
                *receiver.AUTOMATIC_STATES,
                self._max_per_tick,
            ),
        ).fetchall()

    # ---- per-direction step ------------------------------------------------

    def _step_sent(self, row: sqlite3.Row) -> None:
        attachment_id = row["id"]
        state = sender.get_state(self._conn, attachment_id)
        recipient_identities = None
        if state == sender.ENCRYPTING:
            recipient_identities = self._resolve_recipient_identities(attachment_id)
            required_key_ids = self._required_recipient_key_ids(attachment_id)
            if any(key_id not in recipient_identities for key_id in required_key_ids):
                # Reviewer-found defect (PR #227 defect #6): at least one
                # recipient's key_exchange binding is not currently
                # trusted (see _resolve_recipient_identities()'s
                # docstring) - fail the whole attachment now, before any
                # envelope is sealed to an unverified or superseded key,
                # rather than handing sender.run_step() a partial mapping
                # and letting _public_identity_for() raise mid-encrypt.
                sender.fail_recipients_not_trusted(self._conn, attachment_id, self._now())
                return
        provider_id_text = _provider_id_text(row["direction"], row["provider_id"])
        relay_client = self._relay_client_factory(provider_id_text) if provider_id_text else None
        sender.run_step(
            self._conn,
            workspace_manager=self._workspace_manager,
            principal=self._principal,
            recipient_identities=recipient_identities,
            relay_client=relay_client,
            delivery_adapter=self._delivery_adapter,
            network_available=self._can_attempt(provider_id_text),
            attachment_id=attachment_id,
            now=self._now(),
        )

    def _step_received(self, row: sqlite3.Row) -> None:
        provider_id_text = _provider_id_text(row["direction"], row["provider_id"])
        relay_client = self._relay_client_factory(provider_id_text) if provider_id_text else None
        receiver.run_step(
            self._conn,
            workspace_manager=self._workspace_manager,
            principal=self._principal,
            provider_registry=self._provider_registry,
            key_exchange=self._key_exchange,
            network_available=self._can_attempt(provider_id_text),
            attachment_id=row["id"],
            relay_client=relay_client,
            now=self._now(),
        )

    def _can_attempt(self, provider_id_text: Optional[str]) -> bool:
        """Feeds `ConnectivityMonitor.can_attempt_relay()` (advisory,
        fail-open on an unknown provider_id) into the exact
        `network_available: bool` parameter both state machines already
        take - their signatures are unchanged by ADR-0008 (decision 2)."""
        if provider_id_text is None:
            return False
        return self._connectivity.can_attempt_relay(provider_id_text)

    def _resolve_recipient_identities(self, attachment_id: str) -> Dict[str, bytes]:
        """`run_step()`'s ENCRYPTING handler needs `{key_id_hex:
        public_identity_bytes}` for every recipient (sender.py's own
        docstring: it deliberately doesn't persist that value a second
        time). `attachment_recipients.recipient_principal_id` holds each
        recipient's key_id (create_draft()'s own INSERT) - re-resolving
        the public identity from `key_exchange`'s bindings table here is
        exactly what a caller driving run_step() after a restart, rather
        than right after create_draft(), has to do instead of reusing an
        in-memory value that no longer exists.

        Reviewer-found defect (PR #227 defect #6): this used to hand back
        `binding.public_identity` for ANY binding it found, trusted or
        not - so an attachment could be encrypted and sent to a recipient
        whose key was never TOFU-confirmed, or whose key_exchange binding
        had since received a conflicting KEY_ANNOUNCE (`KEY_CHANGED`,
        parked in `pending_public_identity` until the user explicitly
        accepts it). A recipient is only included here when their binding
        is currently `AddressStatus.MCA_READY` - `_step_sent()` treats
        any recipient missing from this mapping as a reason to fail the
        whole attachment (`sender.fail_recipients_not_trusted()`) rather
        than seal a copy to an unverified or superseded key."""
        self._conn.row_factory = sqlite3.Row
        recipient_rows = self._conn.execute(
            "SELECT DISTINCT recipient_principal_id FROM attachment_recipients WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchall()
        identities: Dict[str, bytes] = {}
        for recipient_row in recipient_rows:
            key_id = recipient_row["recipient_principal_id"]
            if not key_id:
                continue
            binding = self._key_exchange.get_binding_by_key_id(key_id)
            if binding is not None and binding.status == AddressStatus.MCA_READY:
                identities[key_id] = binding.public_identity
        return identities

    def _required_recipient_key_ids(self, attachment_id: str) -> List[str]:
        """The full recipient list `_resolve_recipient_identities()` above
        is filtering - kept as its own query (rather than folded into
        that method's return value) so `_step_sent()` can tell "no
        recipients at all" (nothing to compare against - unreachable in
        practice, create_draft() already refuses that) apart from "some
        recipients resolved, some didn't" without the caller needing to
        re-derive the untrusted set from a dict of only the trusted
        ones."""
        self._conn.row_factory = sqlite3.Row
        recipient_rows = self._conn.execute(
            "SELECT DISTINCT recipient_principal_id FROM attachment_recipients WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchall()
        return [row["recipient_principal_id"] for row in recipient_rows if row["recipient_principal_id"]]

    def _default_relay_client(self, provider_id_text: str) -> Optional[RelayClient]:
        """The production factory: resolve the pinned profile and its
        upload token (None is fine - RelayClient's download-path calls
        never need a bearer token, only the upload ones do) from the
        already-injected `ProviderRegistry`. Tests inject their own
        `relay_client_factory` instead of exercising real HTTPS/file I/O
        through this path."""
        profile = self._provider_registry.resolve(provider_id_text)
        if profile is None:
            return None
        upload_token = self._provider_registry.get_upload_token(
            provider_id_text, self._workspace_manager, self._principal.principal_id
        )
        return RelayClient(profile.origin, upload_access_token=upload_token)
