"""The vault runtime (ADR-0013 Decisions 6 and 7): the state machine of one workspace's vault, and the in-memory key ring.

States: ``disabled`` (no ``vault.json``), ``locked`` (a vault exists, no key in memory: every start of the service),
``unlocked`` (the storage key is in a locked-in-RAM buffer), ``migrating`` (``vault-migration.json`` exists; PR 3).
A vault can only be created by PR 3; here it is recognised by its files, so the whole state machine is testable
with a vault the core library made.

Design points:
* The key lives in one ``SecretBuffer`` (zeroed by ``lock()``, mlock'ed where the OS allows). A generation counter
  lets a streaming reader notice that the vault was locked under it.
* Unlock runs on the request thread (the passphrase must never enter the command queue or the command registry),
  serialized with the MCAttach worker by the service's tick lock, so Argon2's memory never competes with a tick.
* On a node with little RAM (Zero 2 W, about 120 MB free) the unlock is refused with a clear ``insufficient_memory``
  instead of risking an out-of-memory kill: it needs the header's ``memlimit`` plus a margin.
* Wrong attempts are rate limited: 1 s, 2 s, 4 s, 4 s, then 60 s from the fifth failure on; never a permanent lockout.
  The counter is in memory (a restart resets it; an attacker who can restart the service already has the node).
* Nothing here logs or returns a passphrase, a key, a file name or a comment.
"""
import math
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from meshsrv.attachments.vault import header as header_mod
from meshsrv.attachments.vault import kdf, memory, passphrase as passphrase_mod, recovery as recovery_mod, swapcheck
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.keys import subkey as _subkey

HEADER_FILE = "vault.json"
MIGRATION_FILE = "vault-migration.json"
HEADER_BACKUP_FILE = "vault.json.bak"
RAM_MARGIN_BYTES = 16 * 1024 * 1024
# What a migration needs ON TOP of the running process (measured: a Zero 2 W peaked at 74 MiB total, of which about 40 MiB
# was the service itself): chunk buffers, the in-memory verify, SQLite page cache and VACUUM. Enable adds the KDF memory.
MIGRATION_WORKING_BYTES = 16 * 1024 * 1024
LOCK_WAIT_SECONDS = 5.0

STATE_DISABLED, STATE_LOCKED, STATE_UNLOCKED, STATE_MIGRATING = "disabled", "locked", "unlocked", "migrating"


MEMINFO_PATH = "/proc/meminfo"


EXPERIMENTAL_ENV = "MESHCENTER_MCA_VAULT_EXPERIMENTAL"


def enable_allowed() -> bool:
    """Until the encrypted file operations of PR 4 exist (receive, send, preview, download under the vault) the
    migration is available only where this variable is set to 1: the live round on a copy, and the tests. With it
    unset an enable is refused, so no node can end up with an encrypted workspace its service cannot read yet."""
    return os.environ.get(EXPERIMENTAL_ENV, "") == "1"


def free_ram_bytes(meminfo_path: Optional[str] = None) -> Optional[int]:
    """MemAvailable in bytes (what the kernel estimates can be given to a new allocation without swapping: free pages
    plus the reclaimable page cache), NOT MemFree, which on a Pi is always small because the kernel uses RAM for cache.
    None when it cannot be read (then no refusal is made on memory grounds)."""
    try:
        with open(meminfo_path or MEMINFO_PATH, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def retry_delay_after(failures: int) -> int:
    """Seconds to wait after the n-th consecutive failure: 1, 2, 4, 4, then 60 from the fifth on."""
    if failures <= 0:
        return 0
    if failures >= 5:
        return 60
    return (1, 2, 4, 4)[failures - 1]


class VaultRuntime:
    def __init__(
        self,
        workspace_dir,
        *,
        log: Optional[Callable[[str, str, str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        free_ram: Callable[[], Optional[int]] = free_ram_bytes,
        serialize_with=None,
        swap_inspector: Callable[[], Dict[str, object]] = swapcheck.inspect_swaps,
    ):
        self._dir = Path(workspace_dir)
        self._log = log or (lambda level, title, details: None)
        self._clock = clock
        self._free_ram = free_ram
        self._serialize_with = serialize_with       # the service's tick lock (anything with acquire(timeout=))
        self._swap_inspector = swap_inspector
        self._key: Optional[memory.SecretBuffer] = None
        self._generation = 0
        self._guard = threading.RLock()             # protects the key and the counters
        self._unlock_mutex = threading.Lock()       # one unlock at a time
        self._failures = 0
        self._next_attempt_at = 0.0
        self._postponed_logged = False
        self._progress: Optional[Dict[str, object]] = None   # the running enable / disable / resume job
        self._handoffs: Dict[str, Tuple[bytes, float]] = {}  # one-shot secrets for a job, never queued or logged
        self._recovery_pending: Optional[str] = None         # a new recovery key waiting to be shown once
        self._reissue_recovery = False                       # an interrupted enable was resumed: issue a new recovery key

    # ---- files and state ---------------------------------------------------------------------------------------

    @property
    def header_path(self) -> Path:
        return self._dir / HEADER_FILE

    def is_enabled(self) -> bool:
        return self.header_path.exists() or (self._dir / MIGRATION_FILE).exists()

    def state(self) -> str:
        if not self.is_enabled():
            return STATE_DISABLED
        if (self._dir / MIGRATION_FILE).exists():
            return STATE_MIGRATING
        with self._guard:
            return STATE_UNLOCKED if self._key is not None else STATE_LOCKED

    def is_locked(self) -> bool:
        """True when a vault exists and its key is not in memory (a migrating vault counts as locked until unlocked)."""
        if not self.is_enabled():
            return False
        with self._guard:
            return self._key is None

    @property
    def generation(self) -> int:
        return self._generation

    def header_problem(self) -> Optional[str]:
        """None when there is no vault or its header is readable and supported; otherwise the error code. A build that
        cannot read the header must not start MCAttach (fail closed, ADR Decision 11)."""
        if not self.header_path.exists():
            return None
        try:
            header_mod.VaultHeader.from_json(self.header_path.read_text(encoding="utf-8"))
        except VaultError as error:
            return error.code
        except OSError:
            return "invalid_header"
        return None

    def _read_header(self) -> header_mod.VaultHeader:
        try:
            return header_mod.VaultHeader.from_json(self.header_path.read_text(encoding="utf-8"))
        except OSError:
            raise VaultError("invalid_header")

    # ---- the key ring --------------------------------------------------------------------------------------------

    def storage_key(self) -> Optional[bytes]:
        """A copy of the storage key, or None while locked. The caller keeps it only as long as it needs it."""
        with self._guard:
            return bytes(self._key) if self._key is not None else None

    def subkey(self, purpose: str) -> Optional[bytes]:
        key = self.storage_key()
        return _subkey(key, purpose) if key is not None else None

    # ---- unlock / lock ---------------------------------------------------------------------------------------------

    def unlock(self, *, passphrase: Optional[str] = None, recovery_key: Optional[str] = None) -> Dict[str, object]:
        if not self.is_enabled():
            raise VaultError("vault_not_enabled")
        if not passphrase and not recovery_key:
            raise VaultError("passphrase_required")
        if not self._unlock_mutex.acquire(timeout=LOCK_WAIT_SECONDS):
            raise VaultError("vault_busy")
        try:
            with self._guard:
                if self._key is not None:
                    return self.status()                         # already unlocked: idempotent
            self._check_rate_limit()
            header = self._read_header()
            self._check_memory(header)
            lock = self._serialize_with
            if lock is not None and not lock.acquire(timeout=LOCK_WAIT_SECONDS):
                raise VaultError("vault_busy")
            try:
                key = self._derive(header, passphrase, recovery_key)
            finally:
                if lock is not None:
                    lock.release()
            self._install(key)
            self._after_success()
            return self.status()
        finally:
            self._unlock_mutex.release()

    def _check_rate_limit(self) -> None:
        with self._guard:
            wait = self._next_attempt_at - self._clock()
        if wait > 0:
            error = VaultError("unlock_rate_limited")
            error.retry_in_s = int(wait) + 1
            raise error

    def _check_memory(self, header: header_mod.VaultHeader) -> None:
        needed = header.kdf_params.memlimit + RAM_MARGIN_BYTES
        available = self._free_ram()
        if available is not None and available < needed:
            error = VaultError("insufficient_memory", "Not enough free memory to unlock now (%d MiB needed, %d MiB free)."
                               % (needed // (1024 * 1024), available // (1024 * 1024)))
            error.needed_bytes, error.available_bytes = needed, available
            self._log("WARNING", "MCAttach vault unlock refused",
                      "Not enough free memory (%d MiB needed, %d MiB free)" % (needed // (1024 * 1024), available // (1024 * 1024)))
            raise error

    def migration_memory_needed(self, op: str) -> int:
        """Bytes of free memory required before an enable ("enable") or a disable ("disable") may start. An enable derives
        a key (at least the 32 MiB floor; the calibration ceiling is a quarter of what is free, so this is never more than
        the floor on a small node); a disable derives nothing."""
        needed = MIGRATION_WORKING_BYTES + RAM_MARGIN_BYTES
        if op == "enable":
            needed += kdf.MIN_MEMLIMIT
        return needed

    def _journal_op(self) -> str:
        try:
            import json
            return str(json.loads((self._dir / MIGRATION_FILE).read_text(encoding="utf-8")).get("op") or "enable")
        except (OSError, ValueError):
            return "enable"

    def check_migration_memory(self, op: Optional[str] = None) -> None:
        """Refuse BEFORE anything is touched (HTTP 503 insufficient_memory), never fail in the middle of a migration.
        `op` None means a resume: the interrupted job's own kind, read from the journal."""
        op = op or self._journal_op()
        needed = self.migration_memory_needed(op)
        available = self._free_ram()
        if available is not None and available < needed:
            error = VaultError("insufficient_memory", "Not enough free memory for this now (%d MiB needed, %d MiB free)."
                               % (needed // (1024 * 1024), available // (1024 * 1024)))
            error.needed_bytes, error.available_bytes = needed, available
            self._log("WARNING", "MCAttach vault %s refused" % op,
                      "Not enough free memory (%d MiB needed, %d MiB free)" % (needed // (1024 * 1024), available // (1024 * 1024)))
            raise error

    def _derive(self, header: header_mod.VaultHeader, passphrase: Optional[str], recovery_key: Optional[str]) -> bytearray:
        try:
            if recovery_key:
                key = header_mod.unlock_with_recovery(header, recovery_mod.decode(recovery_key))
            else:
                key = header_mod.unlock(header, passphrase_mod.normalize(passphrase))
            return key
        except VaultError as error:
            if error.code in ("unlock_failed", "invalid_recovery_key"):
                self._register_failure()
            raise

    def _register_failure(self) -> None:
        with self._guard:
            self._failures += 1
            failures = self._failures
            self._next_attempt_at = self._clock() + retry_delay_after(failures)
        self._log("WARNING", "MCAttach vault unlock failed", "Failed unlock attempt %d" % failures)

    def _install(self, storage_key: bytearray) -> None:
        buf = memory.SecretBuffer(bytes(storage_key))
        for i in range(len(storage_key)):
            storage_key[i] = 0
        with self._guard:
            old, self._key = self._key, buf
            self._generation += 1
            self._failures = 0
            self._next_attempt_at = 0.0
            self._postponed_logged = False
        if old is not None:
            old.zero()
        if not buf.locked:
            self._log("WARNING", "MCAttach vault key not locked in RAM",
                      "The key could not be locked into memory; it may be written to swap")

    def _after_success(self) -> None:
        # a header rewritten by a passphrase change or re-calibration keeps its old copy only until the first unlock
        backup = self._dir / HEADER_BACKUP_FILE
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self._log("INFO", "MCAttach vault unlocked", "MCAttach files can be opened")

    def lock(self) -> Dict[str, object]:
        """Lock now: zero the key, bump the generation (a reader streaming under the old one must stop)."""
        with self._guard:
            old, self._key = self._key, None
            if old is not None:
                self._generation += 1
            self._postponed_logged = False
        if old is not None:
            old.zero()
            self._log("INFO", "MCAttach vault locked", "MCAttach files are locked")
        return self.status()

    # ---- observability -------------------------------------------------------------------------------------------

    # ---- jobs: progress, hand-offs, the one-time recovery key ---------------------------------------------------------

    HANDOFF_TTL_S = 120.0

    def set_progress(self, phase: str, done: int, total: int) -> None:
        with self._guard:
            self._progress = {"phase": phase, "done": done, "total": total}

    def clear_progress(self) -> None:
        with self._guard:
            self._progress = None

    def put_handoff(self, token: str, secret: bytes) -> None:
        """The passphrase for a job goes from the request thread to the worker through here, in memory, once. It is
        never in the command queue, the command registry, a log line or a response."""
        with self._guard:
            now = self._clock()
            for key in [k for k, (_v, t) in self._handoffs.items() if now - t > self.HANDOFF_TTL_S]:
                del self._handoffs[key]
            self._handoffs[token] = (bytes(secret), now)

    def take_handoff(self, token: str) -> Optional[bytes]:
        with self._guard:
            entry = self._handoffs.pop(token, None)
        if entry is None or self._clock() - entry[1] > self.HANDOFF_TTL_S:
            return None
        return entry[0]

    def finish_enable(self, storage_key: bytearray, recovery_key: bytes) -> None:
        """An enable finished: the vault is now unlocked with the key the job made, and the recovery key waits to be
        shown once (`take_recovery_key`)."""
        self._install(storage_key)
        with self._guard:
            self._recovery_pending = recovery_mod.encode(recovery_key)
            self._progress = None
        self._log("INFO", "MCAttach vault enabled", "MCAttach files are now encrypted")

    def finish_disable(self) -> None:
        old = None
        with self._guard:
            old, self._key = self._key, None
            self._generation += 1
            self._progress = None
            self._recovery_pending = None
        if old is not None:
            old.zero()
        self._log("INFO", "MCAttach vault disabled", "MCAttach files are plain again")

    def take_recovery_key(self) -> Optional[str]:
        """The recovery key text, once. After this call it is gone from memory."""
        with self._guard:
            text, self._recovery_pending = self._recovery_pending, None
        return text

    def note_enable_resumed(self) -> None:
        """An enable was finished after a crash: its recovery key was never shown. Say so until a new one is issued."""
        with self._guard:
            self._reissue_recovery = True
            self._progress = None

    def has_recovery_pending(self) -> bool:
        with self._guard:
            return self._recovery_pending is not None

    # ---- header operations (an unlocked vault; they never touch data) ---------------------------------------------

    def _write_header(self, new_header: header_mod.VaultHeader) -> None:
        """Atomic rewrite; the previous header is kept as vault.json.bak until the next successful unlock."""
        path = self.header_path
        backup = self._dir / HEADER_BACKUP_FILE
        if path.exists():
            backup.write_bytes(path.read_bytes())
        part = path.with_name(path.name + ".part")
        with open(part, "w", encoding="utf-8") as handle:
            handle.write(new_header.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, path)

    def _require_unlocked_key(self) -> bytes:
        key = self.storage_key()
        if key is None:
            raise VaultError("vault_locked")
        return key

    def _verify_current(self, header: header_mod.VaultHeader, current_passphrase: str) -> None:
        """Re-authentication for a header change: the current passphrase must derive the same storage key."""
        derived = header_mod.unlock(header, passphrase_mod.normalize(current_passphrase))
        try:
            if bytes(derived) != self._require_unlocked_key():
                raise VaultError("unlock_failed")
        finally:
            for i in range(len(derived)):
                derived[i] = 0

    def _locked_section(self):
        lock = self._serialize_with
        if lock is not None and not lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise VaultError("vault_busy")
        return lock

    def change_passphrase(self, current_passphrase: str, new_passphrase: str) -> Dict[str, object]:
        assessment = passphrase_mod.assess(new_passphrase)
        if not assessment["ok"]:
            error = VaultError("passphrase_too_weak")
            error.level = assessment["level"]
            raise error
        header = self._read_header()
        lock = self._locked_section()
        try:
            self._check_memory(header)
            self._verify_current(header, current_passphrase)
            new_header = header_mod.change_passphrase(header, self._require_unlocked_key(),
                                                      passphrase_mod.normalize(new_passphrase), header.kdf_params)
            self._write_header(new_header)
        finally:
            if lock is not None:
                lock.release()
        self._log("INFO", "MCAttach vault passphrase changed", "The passphrase was changed")
        return self.status()

    def recalibrate(self, current_passphrase: str, *, calibrate=None) -> Dict[str, object]:
        """Derive the parameters again for this host (about one second, within a quarter of the free RAM) and re-wrap
        the storage key under the same passphrase. Files are not touched."""
        header = self._read_header()
        calibrate = calibrate or (lambda: kdf.calibrate(ceiling_bytes=self._calibration_ceiling()))
        lock = self._locked_section()
        try:
            self._check_memory(header)
            self._verify_current(header, current_passphrase)
            params = calibrate()
            new_header = header_mod.change_passphrase(header, self._require_unlocked_key(),
                                                      passphrase_mod.normalize(current_passphrase), params)
            self._write_header(new_header)
        finally:
            if lock is not None:
                lock.release()
        self._log("INFO", "MCAttach vault re-calibrated", "The key derivation cost was re-calibrated for this node")
        return self.status()

    def _calibration_ceiling(self) -> int:
        available = self._free_ram()
        return kdf.MIN_MEMLIMIT if available is None else max(kdf.MIN_MEMLIMIT, available // 4)

    def new_recovery_key(self, current_passphrase: str) -> str:
        """Issue a new recovery key (the old one stops working). Returns its text, to be shown once."""
        header = self._read_header()
        lock = self._locked_section()
        try:
            self._check_memory(header)
            self._verify_current(header, current_passphrase)
            new_header, recovery_key = header_mod.new_recovery_key(header, self._require_unlocked_key())
            self._write_header(new_header)
            with self._guard:
                self._reissue_recovery = False
        finally:
            if lock is not None:
                lock.release()
        self._log("INFO", "MCAttach vault recovery key replaced", "A new recovery key was issued")
        return recovery_mod.encode(recovery_key)

    def note_dropped(self, count: int) -> None:
        """One System Log line when frames that waited for the unlock were dropped (the waiting queue was full)."""
        self._log("WARNING", "MCAttach locked: messages dropped",
                  "%d incoming control message(s) were dropped because the queue of messages waiting for the unlock is full" % count)

    def note_postponed(self, what: str) -> None:
        """One System Log line per lock period for work that waits for the unlock (key requests ...)."""
        with self._guard:
            if self._postponed_logged:
                return
            self._postponed_logged = True
        self._log("INFO", "MCAttach locked", "%s postponed until the vault is unlocked" % what)

    def status(self) -> Dict[str, object]:
        state = self.state()
        out: Dict[str, object] = {"state": state, "enabled": state != STATE_DISABLED, "locked": state in (STATE_LOCKED, STATE_MIGRATING)}
        if state == STATE_DISABLED:
            with self._guard:
                if self._progress is not None:
                    out["progress"] = dict(self._progress)
                out["recovery_key_pending"] = self._recovery_pending is not None
            return out
        with self._guard:
            wait = max(0, math.ceil(self._next_attempt_at - self._clock()))
            out["failed_attempts"] = self._failures
            out["retry_in_s"] = wait
            out["key_locked_in_ram"] = self._key.locked if self._key is not None else None
        try:
            params = self._read_header().kdf_params
            out["kdf"] = {"memlimit_mib": params.memlimit // (1024 * 1024), "opslimit": params.opslimit, "measured_ms": params.measured_ms}
            out["min_free_ram_mib"] = (params.memlimit + RAM_MARGIN_BYTES) // (1024 * 1024)
        except VaultError:
            out["kdf"] = None
        swap = self._swap_inspector()
        out["swap"] = {"disk_swap": bool(swap.get("disk_swap")), "known": bool(swap.get("known"))}
        with self._guard:
            if self._progress is not None:
                out["progress"] = dict(self._progress)
            out["recovery_key_pending"] = self._recovery_pending is not None
            out["recovery_key_needs_reissue"] = self._reissue_recovery
        return out
