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
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from meshsrv.attachments.vault import header as header_mod
from meshsrv.attachments.vault import kdf, memory, passphrase as passphrase_mod, recovery as recovery_mod, swapcheck
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.keys import subkey as _subkey

HEADER_FILE = "vault.json"
MIGRATION_FILE = "vault-migration.json"
HEADER_BACKUP_FILE = "vault.json.bak"
RAM_MARGIN_BYTES = 16 * 1024 * 1024
LOCK_WAIT_SECONDS = 5.0

STATE_DISABLED, STATE_LOCKED, STATE_UNLOCKED, STATE_MIGRATING = "disabled", "locked", "unlocked", "migrating"


MEMINFO_PATH = "/proc/meminfo"


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
        return out
