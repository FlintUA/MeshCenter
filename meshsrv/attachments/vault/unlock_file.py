"""Unattended unlock from a file (ADR-0013 decision 9, PR 6): for a NAS that restarts by itself.

`MESHCENTER_MCA_VAULT_UNLOCK_FILE` names a file (a Docker secret, normally `/run/secrets/<name>`) that holds the vault
passphrase. It is OFF by default and honoured only in container mode. When it is set, the vault is enabled and locked at
start, the file is read once and the vault is unlocked with it. The trade-off is the whole point of the feature and is
written in docs/MCAttach_Vault.md: the key then sits next to the data, so anyone who can read the volume and the secret
can read the vault. Every use is noted in the System Log.

Rules: one attempt per process start (no retry loop; a wrong passphrase also counts against the unlock rate limit, like a
typed one), the file is read as UTF-8 text, at most 1 KiB, only the trailing line break is removed, and nothing from the
file ever reaches a log line, an error or a status.
"""
import os
from typing import Callable, Optional, Tuple

from meshsrv import runtime_env
from meshsrv.attachments.vault.errors import VaultError

ENV = "MESHCENTER_MCA_VAULT_UNLOCK_FILE"
MAX_BYTES = 1024


def read_secret(path: str) -> Tuple[Optional[str], Optional[str]]:
    """(passphrase, None) or (None, problem code). The problem is a stable code, never the file's content."""
    if not path or not os.path.isabs(path):
        return None, "path_not_absolute"
    try:
        real = os.path.realpath(path)             # a secret mounted as a symlink (Kubernetes style) is fine; what it points at must be a file
        if not os.path.isfile(real):
            return None, "not_a_file"
        if os.path.getsize(real) > MAX_BYTES:
            return None, "too_large"
        with open(real, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(raw) > MAX_BYTES:
        return None, "too_large"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not_text"
    text = text[:-2] if text.endswith("\r\n") else (text[:-1] if text.endswith("\n") else text)
    if not text:
        return None, "empty"
    return text, None


def maybe_unlock(vault, log: Callable[[str, str, str], None], environ=None) -> str:
    """Unlock `vault` from the configured file, once. Returns a short outcome code (for tests and the log):
    "off", "ignored_not_container", "no_vault", "already_unlocked", "unlocked" or "failed:<code>"."""
    environ = os.environ if environ is None else environ
    path = (environ.get(ENV) or "").strip()
    if not path:
        return "off"
    if not runtime_env.is_container():
        log("WARNING", "MCAttach unlock file ignored", "%s is only used in container mode" % ENV)
        return "ignored_not_container"
    if vault is None or not vault.is_enabled():
        return "no_vault"
    if not vault.is_locked():
        return "already_unlocked"
    secret, problem = read_secret(path)
    if problem is not None:
        log("WARNING", "MCAttach unlock file not usable", "The unlock file cannot be used (%s). MCAttach stays locked." % problem)
        return "failed:" + problem
    try:
        vault.unlock(passphrase=secret)
    except VaultError as error:
        log("WARNING", "MCAttach unlock from the file failed", "The vault stays locked (%s). Unlock it in the UI." % error.code)
        return "failed:" + error.code
    finally:
        secret = None
    log("WARNING", "MCAttach unlocked from the unlock file",
        "The vault key is derived from a passphrase file next to the data. This is the unattended-start trade-off (see the vault guide).")
    return "unlocked"
