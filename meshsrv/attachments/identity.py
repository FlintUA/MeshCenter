"""meshsrv/attachments/identity.py

MCA principal creation and persistence (Execution Plan Step 1.2; design
spec section 7.1; ADR-0002 for the confirmed Ed25519->X25519 derivation
this module relies on). MIT-licensed Core code - does not import
`meshtastic`.

Scope, per spec 7.1: "Для первой реализации используется один MCA
principal на MCA workspace" - one workspace has exactly one MCA
principal, never a list. `mca_principal` (migration 4) is therefore a
single-row-per-workspace table, and `create_principal()` refuses to mint
a second one.

Identity, not radio profile: this module never reads or writes anything
under `data/profiles/` (design spec section 16.2) - an MCA principal
survives a Meshtastic radio swap, which is the entire point of separating
the two (spec 7.1's opening paragraph).

Key material handling (ADR-0003): the private Ed25519 seed (32 raw bytes)
is written to a file under this workspace's `keys/` directory (already
locked to 0700 by `MCAWorkspaceManager.ensure_workspace`) at file mode
0600, and is never stored in the database, logged, or included in any
export. Only the *filename* (not a full path, and never the key bytes)
lives in the `mca_principal.private_key_file` column.

`principal_id` vs. `key_id`: `principal_id` is the stable identifier the
workspace directory is keyed by (`MCAWorkspaceManager`/ADR-0003) - fixed
at creation and never changed by a future key rotation (spec 7.5, out of
scope for Step 1.2). `key_id` is the *current epoch's* 64-bit lookup
fingerprint (spec 7.1: "Сокращенный 64-bit key_id... используется только
для lookup") - at epoch 0 (the only epoch Step 1.2 creates) the two are
equal by construction, but code must not assume they stay equal forever;
that is exactly the distinction migration 4's schema keeps separate
columns for.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3
import time
from typing import Optional

import nacl.bindings as sodium
from nacl.signing import SigningKey

from meshsrv.attachments.workspace import MCAWorkspaceManager

_IDENTITY_KEY_FILE = "identity_ed25519.seed"
_KEY_FILE_MODE = 0o600


class IdentityError(RuntimeError):
    """Raised for a missing/corrupt MCA principal, a private-key/public-key
    mismatch on disk, or an attempt to mint a second principal in a
    workspace that already has one - never silently ignored or overwritten."""


@dataclasses.dataclass(frozen=True)
class MCAPrincipal:
    """Public record of this workspace's one MCA principal. Never carries
    private key material - see `load_signing_key()` for that, which reads
    it fresh from disk on every call rather than caching it in a
    long-lived object."""

    workspace_id: str
    principal_id: str  # stable, hex, 16 chars (8 bytes) - never changes across rotation
    key_id: str  # current epoch's key_id, hex, 16 chars - == principal_id at epoch 0
    epoch: int
    public_identity: bytes  # 32 raw bytes, Ed25519
    public_x25519: bytes  # 32 raw bytes, derived per ADR-0002
    private_key_file: str  # filename only, under this workspace's keys/ dir
    created_at: float
    status: str = "ACTIVE"


def compute_key_id(public_identity: bytes) -> str:
    """64-bit key_id (spec 7.1): first 8 bytes of SHA-256(public Ed25519
    key), hex-encoded - a lookup value only, exactly like `provider_id`
    (provider_registry.py). Trust is never based on this alone: "Доверие
    закрепляется за полным public key/fingerprint, а signature всегда
    проверяется полным ключом" (spec 7.1)."""
    if len(public_identity) != 32:
        raise IdentityError(f"public_identity must be 32 raw bytes (Ed25519), got {len(public_identity)}")
    return hashlib.sha256(public_identity).digest()[:8].hex()


def derive_x25519_public(public_identity: bytes) -> bytes:
    """ADR-0002: the confirmed Ed25519->X25519 conversion, verified
    byte-identical on `dev`/`prod`/the dev workstation - see
    docs/architecture/ADR-0002-crypto-suite.md and
    tests/crypto/test_key_derivation.py. Delegates to PyNaCl's binding of
    libsodium's own `crypto_sign_ed25519_pk_to_curve25519` - no
    reimplementation."""
    if len(public_identity) != 32:
        raise IdentityError(f"public_identity must be 32 raw bytes (Ed25519), got {len(public_identity)}")
    return sodium.crypto_sign_ed25519_pk_to_curve25519(public_identity)


def _row_to_principal(row: sqlite3.Row) -> MCAPrincipal:
    return MCAPrincipal(
        workspace_id=row["workspace_id"],
        principal_id=row["principal_id"],
        key_id=row["key_id"],
        epoch=row["epoch"],
        public_identity=bytes.fromhex(row["public_identity"]),
        public_x25519=bytes.fromhex(row["public_x25519"]),
        private_key_file=row["private_key_file"],
        created_at=row["created_at"],
        status=row["status"],
    )


def load_principal(conn: sqlite3.Connection, workspace_id: str) -> Optional[MCAPrincipal]:
    """Public-info-only lookup - never touches disk/private key material.
    Returns `None` if this workspace has not yet created its principal
    (design spec section 7.1's "при первом включении MCAttach в
    Settings" has not happened yet)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mca_principal WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    return _row_to_principal(row) if row is not None else None


def create_principal(
    conn: sqlite3.Connection,
    workspace_manager: MCAWorkspaceManager,
    workspace_id: str,
    *,
    now: Optional[float] = None,
) -> MCAPrincipal:
    """Mint this workspace's one MCA principal: a fresh Ed25519 keypair,
    generated locally with the OS CSPRNG (spec 8.1) - never derived from
    anything predictable, never imported from elsewhere. Raises
    `IdentityError` if this workspace already has a principal; use
    `ensure_principal()` for the normal idempotent "first enablement or
    no-op" flow.
    """
    if load_principal(conn, workspace_id) is not None:
        raise IdentityError(f"workspace {workspace_id!r} already has an MCA principal")

    signing_key = SigningKey.generate()
    public_identity = bytes(signing_key.verify_key)
    public_x25519 = derive_x25519_public(public_identity)
    key_id = compute_key_id(public_identity)
    principal_id = key_id  # genesis epoch: see module docstring
    epoch = 0
    now = time.time() if now is None else now

    paths = workspace_manager.ensure_workspace(principal_id)
    key_path = paths.keys / _IDENTITY_KEY_FILE
    # O_EXCL: refuse to silently overwrite an existing key file - a
    # workspace directory that already has one but no DB row would mean
    # something went wrong earlier (partial create, manual tampering);
    # failing loudly here is safer than generating a second key that
    # doesn't match whatever is already on disk.
    # getattr(..., 0): os.O_BINARY only exists on Windows, where os.open()
    # otherwise defaults to text mode and silently corrupts any raw byte
    # in the seed that happens to be \n (0x0A -> 0x0D 0x0A) - a real,
    # non-theoretical bug, reproduced locally on ~1-in-9 random seeds. A
    # no-op on POSIX (this project's only real target), so unconditionally
    # OR-ing it in is always safe.
    fd = os.open(
        key_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        _KEY_FILE_MODE,
    )
    try:
        os.write(fd, bytes(signing_key))
    finally:
        os.close(fd)
    os.chmod(key_path, _KEY_FILE_MODE)

    conn.execute(
        """
        INSERT INTO mca_principal
            (workspace_id, principal_id, key_id, epoch, public_identity,
             public_x25519, private_key_file, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
        """,
        (
            workspace_id,
            principal_id,
            key_id,
            epoch,
            public_identity.hex(),
            public_x25519.hex(),
            _IDENTITY_KEY_FILE,
            now,
        ),
    )
    conn.commit()

    return MCAPrincipal(
        workspace_id=workspace_id,
        principal_id=principal_id,
        key_id=key_id,
        epoch=epoch,
        public_identity=public_identity,
        public_x25519=public_x25519,
        private_key_file=_IDENTITY_KEY_FILE,
        created_at=now,
    )


def ensure_principal(
    conn: sqlite3.Connection,
    workspace_manager: MCAWorkspaceManager,
    workspace_id: str,
    *,
    now: Optional[float] = None,
) -> MCAPrincipal:
    """Idempotent entry point for "first enablement" (spec 7.1): returns
    the existing principal if this workspace already has one, otherwise
    creates it. This is what Settings' "enable MCAttach" action should
    call - never `create_principal()` directly, which is deliberately
    strict about not being called twice."""
    existing = load_principal(conn, workspace_id)
    if existing is not None:
        return existing
    return create_principal(conn, workspace_manager, workspace_id, now=now)


def load_signing_key(workspace_manager: MCAWorkspaceManager, principal: MCAPrincipal) -> SigningKey:
    """Read the private Ed25519 seed back from disk. Deliberately not
    cached on `MCAPrincipal` (a frozen, freely-passed-around dataclass) -
    callers that need to sign a message call this immediately before
    doing so, keeping the private key's lifetime in memory as short as
    practical. Cross-checks the loaded key's public half against the
    stored `public_identity` and raises `IdentityError` on any mismatch,
    rather than silently signing with the wrong key."""
    paths = workspace_manager.paths(principal.principal_id)
    key_path = paths.keys / principal.private_key_file
    if not key_path.exists():
        raise IdentityError(f"private key file missing for principal {principal.principal_id}: {key_path}")
    seed = key_path.read_bytes()
    if len(seed) != 32:
        raise IdentityError(f"private key file {key_path} is not a 32-byte seed (got {len(seed)} bytes)")
    signing_key = SigningKey(seed)
    if bytes(signing_key.verify_key) != principal.public_identity:
        raise IdentityError(
            f"private key file {key_path} does not match the stored public identity for "
            f"principal {principal.principal_id} - possible corruption or tampering"
        )
    return signing_key
