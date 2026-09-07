"""meshsrv/attachments/contacts.py

ADR-0008 decision 4 (Step 1.6A backend layer): the minimal contacts
backend the send form's "Кому" field and a future Files-tab per-contact
badge need. This is deliberately a thin translation layer over
`key_exchange.py`'s already-existing TOFU/key-change machinery
(`mca_recipient_bindings`'s `tofu_confirmed_at`, `pending_public_identity`,
`pending_key_epoch`, `pending_detected_at` columns) - it adds no new
schema, no new state machine, and no new trust decision of its own.

Explicitly NOT in scope here (per the ADR and the Execution Plan's own
MVP narrowing to Meshtastic-direct-only): a general contacts directory or
picker. The send form's "Кому" field is pre-filled from the open direct
chat's own contact - a single (adapter_id, transport_address) pair the
caller already knows - never a list this module produces. Full
contacts/Relay management is Step 1.7+.
"""

from __future__ import annotations

import enum
from typing import Optional

from meshsrv.attachments.key_exchange import AddressStatus, KeyExchangeCoordinator, KeyExchangeError


class ContactError(RuntimeError):
    """Base class for this module's errors."""


class ContactStatus(str, enum.Enum):
    """The send-form/Files-tab vocabulary (ADR-0008), mapped 1:1 from
    `key_exchange.AddressStatus` - a separate enum so the UI/API layer
    never has to import `key_exchange` directly for what is, from that
    layer's point of view, just "what does this contact's badge say"."""

    KEY_UNKNOWN = "key_unknown"
    CONFIRMATION_REQUIRED = "confirmation_required"
    TRUSTED = "trusted"
    KEY_CHANGED = "key_changed"


_STATUS_MAP = {
    AddressStatus.KEY_UNKNOWN: ContactStatus.KEY_UNKNOWN,
    AddressStatus.KEY_UNVERIFIED: ContactStatus.CONFIRMATION_REQUIRED,
    AddressStatus.MCA_READY: ContactStatus.TRUSTED,
    AddressStatus.KEY_CHANGED: ContactStatus.KEY_CHANGED,
}


def contact_status(coordinator: KeyExchangeCoordinator, source_address: str) -> ContactStatus:
    """Read-only - never sends anything, never performs I/O. `source_address`
    is the same transport address `key_exchange.py`'s own methods key on
    (e.g. the Meshtastic node address of the open direct chat)."""
    return _STATUS_MAP[coordinator.get_status(source_address)]


def confirm_binding(coordinator: KeyExchangeCoordinator, source_address: str, *, now: Optional[float] = None) -> None:
    """Spec 7.3's "Доверять этому MCA-ключу" action, under the contacts
    vocabulary. Refuses when the key is still unknown (`KEY_UNKNOWN`) -
    there is nothing to confirm yet in that case, that path is
    `build_key_request()`'s job instead, not this one's."""
    if coordinator.get_binding(source_address) is None:
        raise ContactError(f"no key on file yet for {source_address!r} - nothing to confirm")
    coordinator.confirm_tofu(source_address, now=now)


def accept_key_change(coordinator: KeyExchangeCoordinator, source_address: str, *, now: Optional[float] = None) -> None:
    """Promotes a parked key change into the trusted binding (the new key
    still needs its own, separate `confirm_binding()` call afterwards -
    `accept_pending_key_change()` deliberately resets `tofu_confirmed_at`
    to NULL, exactly like a first-time introduction)."""
    try:
        coordinator.accept_pending_key_change(source_address, now=now)
    except KeyExchangeError as exc:
        raise ContactError(str(exc)) from exc


def reject_key_change(coordinator: KeyExchangeCoordinator, source_address: str) -> None:
    """Dismisses a parked key change, leaving the existing trusted binding
    untouched. Not a blacklist: a later KEY_ANNOUNCE carrying the same
    identity can park it again."""
    try:
        coordinator.reject_pending_key_change(source_address)
    except KeyExchangeError as exc:
        raise ContactError(str(exc)) from exc
