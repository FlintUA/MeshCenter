"""meshsrv/attachments/workspace.py

`MCAWorkspaceManager` (Execution Plan Step 0.6; design spec section 16.3;
ADR-0003). Computes every filesystem path MCAttach uses under
`data/mca/<principal-id>/` - no other module may build one of these paths
by string concatenation (see `test_no_stray_data_mca_path_construction` in
tests/test_mca_workspace.py).

Deliberately mirrors `storage/profile_manager.py::ProfileManager`'s shape
(constructor takes a `data_dir`, validates the scoping ID with a regex,
resolves and verifies the child path did not escape its parent) so a
reviewer already familiar with that module recognizes the pattern here
rather than learning a second one - see ADR-0003. An MCA principal is
*not* a radio profile (design spec section 16.2): this manager never reads
or writes anything under `data/profiles/`.

MIT-licensed Core code - does not import `meshtastic`.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import re
import shutil
from pathlib import Path
from typing import Optional

# MCA principal IDs are the hex form of an 8-byte identifier (matching
# codec.py's `sender_key_id: bytes(8)`, ADR-0001 section 3) - this is a
# provisional convention pending Step 1.2, which actually mints principal
# IDs; if Step 1.2 picks a different length/format this regex (and nothing
# else, since every path is computed here) is what changes.
_PRINCIPAL_ID_PATTERN = re.compile(r"[0-9a-f]{16}")

_KEYS_DIR_MODE = 0o700  # see ADR-0003: 0600 would remove the dir's execute bit


class WorkspacePathError(ValueError):
    """Raised for an invalid principal_id or a path that would escape its
    parent directory - never silently corrected."""


@dataclasses.dataclass(frozen=True)
class WorkspacePaths:
    """All paths for one MCA workspace (design spec section 16.3)."""

    root: Path
    attachments_db: Path
    spool_outgoing: Path
    cache_incoming: Path
    files: Path
    quarantine: Path
    keys: Path


class LowDiskLevel(enum.Enum):
    """Design spec section 16.5: warn under 15% free, block new downloads
    under 5% free."""

    OK = "OK"
    WARN = "WARN"
    BLOCK = "BLOCK"


def _validate_principal_id(principal_id: str) -> str:
    clean = str(principal_id or "").strip().lower()
    if not _PRINCIPAL_ID_PATTERN.fullmatch(clean):
        raise WorkspacePathError(f"invalid MCA principal ID: {principal_id!r}")
    return clean


class MCAWorkspaceManager:
    """Create, resolve, and enforce quota/traversal rules for one
    MCA-workspace archive tree rooted at ``data_dir / "mca" / <principal-id>``.
    """

    def __init__(self, data_dir: "str | os.PathLike[str]") -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.mca_dir = (self.data_dir / "mca").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.mca_dir.mkdir(parents=True, exist_ok=True)

    # ---- path resolution ------------------------------------------------

    def _workspace_dir(self, principal_id: str) -> Path:
        clean = _validate_principal_id(principal_id)
        path = (self.mca_dir / clean).resolve()
        if path.parent != self.mca_dir:
            raise WorkspacePathError("workspace path escaped the MCA root directory")
        return path

    def paths(self, principal_id: str) -> WorkspacePaths:
        """Return every path for this workspace without creating anything
        - use `ensure_workspace` when directories must exist."""
        root = self._workspace_dir(principal_id)
        return WorkspacePaths(
            root=root,
            attachments_db=root / "attachments.db",
            spool_outgoing=root / "spool" / "outgoing",
            cache_incoming=root / "cache" / "incoming",
            files=root / "files",
            quarantine=root / "quarantine",
            keys=root / "keys",
        )

    def ensure_workspace(self, principal_id: str) -> WorkspacePaths:
        """Create every workspace directory (idempotent) and lock down
        `keys/` to owner-only (`0700` - see ADR-0003 for why not the
        literal `0600` the design spec's prose names)."""
        paths = self.paths(principal_id)
        for directory in (paths.root, paths.spool_outgoing, paths.cache_incoming, paths.files, paths.quarantine):
            directory.mkdir(parents=True, exist_ok=True)
        paths.keys.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.keys, _KEYS_DIR_MODE)
        return paths

    # ---- safe file naming (design spec section 23.1 "Storage": filename
    # traversal, duplicate name) -----------------------------------------

    def resolve_saved_path(self, principal_id: str, requested_file_name: str) -> Path:
        """Turn a (possibly hostile) requested file name into a path
        guaranteed to resolve inside this workspace's `files/` directory.
        Rejects outright rather than silently sanitizing: any path
        separator (`/` or `\\`), a NUL byte, or a bare `.`/`..` name is a
        hard error, not something to strip down to a "safe" remainder -
        fail closed, exactly like the codec parser (ADR-0001 section 6)
        rejects malformed input instead of best-effort-recovering from it.
        Containment is re-verified on the resolved path as defense in
        depth against symlink or platform path-parsing surprises."""
        paths = self.paths(principal_id)
        raw = str(requested_file_name)
        if "\x00" in raw:
            raise WorkspacePathError(f"file name contains a NUL byte: {requested_file_name!r}")
        if "/" in raw or "\\" in raw:
            raise WorkspacePathError(f"file name must not contain a path separator: {requested_file_name!r}")
        stripped = raw.strip()
        if stripped in ("", ".", ".."):
            raise WorkspacePathError(f"unsafe file name: {requested_file_name!r}")
        candidate = (paths.files / stripped).resolve()
        files_dir_resolved = paths.files.resolve()
        if candidate.parent != files_dir_resolved:
            raise WorkspacePathError("saved path escaped the workspace files directory")
        return candidate

    def unique_file_name(self, principal_id: str, requested_file_name: str) -> Path:
        """Like `resolve_saved_path`, but appends ` (2)`, ` (3)`, ... before
        the extension if the name already exists, instead of overwriting a
        previously saved file (design spec section 23.1, "duplicate
        name")."""
        candidate = self.resolve_saved_path(principal_id, requested_file_name)
        if not candidate.exists():
            return candidate
        stem, suffix = candidate.stem, candidate.suffix
        counter = 2
        while True:
            renamed = candidate.with_name(f"{stem} ({counter}){suffix}")
            if not renamed.exists():
                return renamed
            counter += 1

    # ---- quota / low-disk checks (design spec section 16.5) -------------

    def files_dir_bytes(self, principal_id: str) -> int:
        paths = self.paths(principal_id)
        if not paths.files.exists():
            return 0
        return sum(f.stat().st_size for f in paths.files.rglob("*") if f.is_file())

    def is_over_quota(self, principal_id: str, max_bytes: int, additional_bytes: int = 0) -> bool:
        return self.files_dir_bytes(principal_id) + additional_bytes > max_bytes

    def low_disk_level(self, principal_id: str, warn_ratio: float = 0.15, block_ratio: float = 0.05) -> LowDiskLevel:
        paths = self.paths(principal_id)
        usage = shutil.disk_usage(paths.root if paths.root.exists() else self.data_dir)
        if usage.total == 0:
            return LowDiskLevel.OK
        free_ratio = usage.free / usage.total
        if free_ratio < block_ratio:
            return LowDiskLevel.BLOCK
        if free_ratio < warn_ratio:
            return LowDiskLevel.WARN
        return LowDiskLevel.OK
