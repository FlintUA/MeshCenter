"""tests/_sender_crash_driver.py

Standalone driver process for the kill-9 restart-recovery tests
(tests/test_sender_crash_recovery.py). Not a pytest module (leading
underscore) - spawned as a real OS subprocess so the test can send it a
genuine SIGKILL and then start a *fresh* process against the same on-disk
`attachments.db`, proving durability across an actual process death, not
just "call the function again in the same interpreter".

Usage:
    python _sender_crash_driver.py <db_path> <data_dir> <relay_base_url>
        <upload_access_token> <source_path> <recipient_pub_hex>
        <attachment_id_or_NEW> <stop_after_state_or_NONE> <sent_log_path>

On success prints one line "ATTACHMENT_ID=<id>" (only for a NEW draft) and
exits 0 once the attachment reaches SENT or a terminal state. If
`stop_after_state` is reached, sends SIGKILL to itself immediately after
that state was durably committed (run_step()'s handlers always commit
before returning - see sender.py's module docstring) - the test asserts
the process actually died by signal, not by a normal exit, so this can
never silently degrade into "just returns early".
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys

sys.path.insert(0, os.getcwd())

from meshsrv.attachments import codec, identity, sender  # noqa: E402
from meshsrv.attachments.db import migrations  # noqa: E402
from meshsrv.attachments.delivery.base import (  # noqa: E402
    AckSemantics,
    ConnectorState,
    DeliveryAdapter,
    DeliveryCapabilities,
    DeliveryReceipt,
    Route,
    RouteType,
    UnsupportedRouteError,
    WireFormat,
)
from meshsrv.attachments.relay_client import RelayClient  # noqa: E402
from meshsrv.attachments.workspace import MCAWorkspaceManager  # noqa: E402


class LoggingTextAdapter(DeliveryAdapter):
    """A minimal real DeliveryAdapter whose `send()` appends one line to a
    file on disk instead of an in-memory structure - unlike
    `delivery/fakes.py::FakeTextAdapter`'s `InMemoryEther`, this survives
    (and is observable from) a separate OS process, which is the whole
    point of this driver: the parent test process reads this file after
    the subprocess is killed and restarted to prove "sent exactly once"
    across a real process death."""

    adapter_id = "logging-text"

    def __init__(self, sent_log_path: str):
        self._sent_log_path = sent_log_path

    def capabilities(self) -> DeliveryCapabilities:
        return DeliveryCapabilities(
            wire_formats=frozenset({WireFormat.MCA1_TEXT}),
            max_payload_bytes=180,
            supports_direct=True,
            supports_channel=False,
            supports_incoming=False,
            ack_semantics=AckSemantics.BEST_EFFORT,
            connector_state=ConnectorState.READY,
        )

    def resolve_route(self, user_selection):
        address = user_selection.get("address")
        if not address:
            raise UnsupportedRouteError("logging-text requires user_selection['address']")
        return Route(route_type=RouteType.DIRECT, route_id=str(address), destination_address=str(address))

    def encode(self, logical_message: bytes, route: Route) -> bytes:
        return codec.to_text(logical_message).encode("ascii")

    def send(self, wire_payload: bytes, route: Route, idempotency_key: str) -> DeliveryReceipt:
        with open(self._sent_log_path, "a", encoding="ascii") as fh:
            fh.write(wire_payload.decode("ascii") + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return DeliveryReceipt(sent=True, idempotency_key=idempotency_key, external_message_id=idempotency_key)

    def ingest(self, transport_event):
        return None


def main() -> int:
    (
        db_path, data_dir, relay_base_url, upload_access_token, source_path,
        recipient_pub_hex, attachment_id_arg, stop_after_state, sent_log_path,
    ) = sys.argv[1:10]
    stop_after_state = None if stop_after_state == "NONE" else stop_after_state

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(conn)
    wsm = MCAWorkspaceManager(data_dir)
    principal = identity.ensure_principal(conn, wsm, "local")

    recipient_pub = bytes.fromhex(recipient_pub_hex)
    recipient_key_id = identity.compute_key_id(recipient_pub)
    recipient_identities = {recipient_key_id: recipient_pub}

    if attachment_id_arg == "NEW":
        attachment_id = sender.create_draft(
            conn, wsm, principal,
            workspace_id="local",
            source_path=source_path,
            file_name=os.path.basename(source_path),
            mime_type="text/plain",
            recipients=[sender.RecipientTarget(public_identity=recipient_pub, key_id=recipient_key_id)],
            adapter_id="logging-text",
            connector_profile_id="default",
            route_type="DIRECT",
            route_id="receiver-addr",
            provider_id=b"\x02" * 8,
        )
        print(f"ATTACHMENT_ID={attachment_id}", flush=True)
        if stop_after_state == "DRAFT":
            os.kill(os.getpid(), signal.SIGKILL)
    else:
        attachment_id = attachment_id_arg

    relay_client = RelayClient(relay_base_url, upload_access_token=upload_access_token)
    delivery_adapter = LoggingTextAdapter(sent_log_path)

    for _ in range(30):
        state = sender.get_state(conn, attachment_id)
        if state in sender.TERMINAL_STATES or state == sender.SENT:
            break
        new_state = sender.run_step(
            conn,
            workspace_manager=wsm,
            principal=principal,
            recipient_identities=recipient_identities,
            relay_client=relay_client,
            delivery_adapter=delivery_adapter,
            attachment_id=attachment_id,
        )
        print(f"STATE={new_state}", flush=True)
        if new_state == stop_after_state:
            os.kill(os.getpid(), signal.SIGKILL)
        if new_state == state:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
