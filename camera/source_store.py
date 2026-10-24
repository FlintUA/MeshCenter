"""Saved network camera sources (CAM2, Phase 1a).

Two instance-scoped files under the data directory (a LAN camera belongs to
the installation, not to a radio profile - like epaper_config.json):

  data/camera_sources.json   schema v1 - the source list, NO secrets
  data/camera_secrets.json   schema v1 - {source id: {"password": ...}}, mode 0600

The password is NOT encrypted at rest. MeshCenter's own session key and the
camera files sit beside each other, so a key kept next to the data would be
obfuscation, not encryption; the honest model is "a file readable only by the
MeshCenter user (and root)". The password is write-only everywhere else: the
public representation only says ``password_set``, and nothing in this module
ever logs or returns it except `get_password()`, which only the driver calls.

Writes go through storage/json_store.safe_write_json (atomic, quarantines a
corrupt file on read). On a failed write the in-memory state is restored, so a
failure never leaves memory and disk disagreeing.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from camera import address_policy, rtsp_input
from storage.json_store import safe_read_json, safe_write_json

SCHEMA_VERSION = 1
MAX_SOURCES = 8
KIND_RTSP = "rtsp"
PROFILES = ("sub", "main")
TRANSPORTS = ("tcp", "udp")
UNSET = object()  # "leave the password as it is"
CONTROL_TYPES = ("none", "reolink")
CONTROL_SCHEMES = ("https", "http")
CONTROL_INPUT_FIELDS = ("type", "scheme", "port", "channel", "allow_http", "snapshot_quality")
SNAPSHOT_QUALITIES = ("full", "small")


def default_control() -> dict:
    """Camera-side control (CAM2 Phase 2). type "none" = RTSP only, the default and the state of every
    source saved before Phase 2. `pinned_sha256` and `capabilities` are written only by the server
    (set_control_state): the pin after the user confirmed a fingerprint, the capabilities after a probe."""
    return {"type": "none", "scheme": "https", "port": 443, "channel": 0, "allow_http": False,
            "snapshot_quality": "full", "pinned_sha256": "", "capabilities": {}}

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class SourceValidationError(ValueError):
    """A rejected field. ``code`` is machine readable, ``field`` names the input."""

    def __init__(self, code: str, message: str, field: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class SourceNotFound(KeyError):
    pass


class SourceStorageError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, field: str, *, min_len: int = 0, max_len: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SourceValidationError("invalid_field", f"{field} must be text.", field)
    value = value.strip()
    if _CTRL.search(value):
        raise SourceValidationError("invalid_field", f"{field} contains a control character.", field)
    if required and not value:
        raise SourceValidationError("missing_field", f"{field} is required.", field)
    if len(value) < min_len or len(value) > max_len:
        raise SourceValidationError("invalid_field", f"{field} must be {min_len}-{max_len} characters.", field)
    return value


class CameraSourceStore:
    def __init__(
        self,
        data_dir: str,
        *,
        host_validator: Callable[[str], list] | None = None,
    ):
        self.data_dir = os.path.abspath(data_dir)
        self.sources_path = os.path.join(self.data_dir, "camera_sources.json")
        self.secrets_path = os.path.join(self.data_dir, "camera_secrets.json")
        self._validate_host = host_validator or address_policy.validate_camera_host
        self._lock = threading.RLock()

    # -- persistence -------------------------------------------------------

    def _load(self) -> tuple[list[dict], dict[str, dict]]:
        sources_doc = safe_read_json(self.sources_path, default={})
        secrets_doc = safe_read_json(self.secrets_path, default={})
        sources = sources_doc.get("sources") if isinstance(sources_doc, dict) else None
        secrets = secrets_doc.get("secrets") if isinstance(secrets_doc, dict) else None
        sources = [s for s in (sources or []) if isinstance(s, dict) and isinstance(s.get("id"), str)]
        secrets = {k: v for k, v in (secrets or {}).items() if isinstance(v, dict)}
        return sources, secrets

    def _save(self, sources: list[dict], secrets: dict[str, dict]) -> None:
        if not safe_write_json(self.secrets_path, {"schema_version": SCHEMA_VERSION, "secrets": secrets}):
            raise SourceStorageError("secrets write failed")
        try:
            os.chmod(self.secrets_path, 0o600)
        except OSError:
            pass  # non-POSIX filesystem; mkstemp already created the file 0600 where it matters
        if not safe_write_json(self.sources_path, {"schema_version": SCHEMA_VERSION, "sources": sources}):
            raise SourceStorageError("sources write failed")

    def _commit(self, new_sources, new_secrets, old_sources, old_secrets) -> None:
        try:
            self._save(new_sources, new_secrets)
        except SourceStorageError:
            try:  # restore the previous state on disk so memory (nothing is cached) and disk agree
                self._save(old_sources, old_secrets)
            except SourceStorageError:
                pass
            raise

    # -- representation ----------------------------------------------------

    @staticmethod
    def _public(source: dict, secrets: dict[str, dict]) -> dict:
        out = {k: copy.deepcopy(v) for k, v in source.items()}
        out["control"] = {**default_control(), **(out.get("control") if isinstance(out.get("control"), dict) else {})}
        out["password_set"] = bool((secrets.get(source["id"]) or {}).get("password"))
        return out

    def list_sources(self) -> list[dict]:
        with self._lock:
            sources, secrets = self._load()
            return [self._public(s, secrets) for s in sources]

    def get_source(self, source_id: str) -> dict:
        with self._lock:
            sources, secrets = self._load()
            for source in sources:
                if source["id"] == source_id:
                    return self._public(source, secrets)
        raise SourceNotFound(source_id)

    def get_password(self, source_id: str) -> str:
        """For the driver only. Never put the result in a log line, an API response or an exception."""
        with self._lock:
            _sources, secrets = self._load()
            return str((secrets.get(source_id) or {}).get("password") or "")

    # -- validation --------------------------------------------------------

    def _clean(self, fields: dict, existing: dict | None, all_sources: list[dict]) -> dict:
        def pick(name, default=""):
            if name in fields:
                return fields[name]
            return existing.get(name, default) if existing else default

        name = _text(pick("name"), "name", min_len=1, max_len=60, required=True)
        for other in all_sources:
            if other["id"] != (existing or {}).get("id") and other["name"].casefold() == name.casefold():
                raise SourceValidationError("duplicate_name", "A camera source with this name already exists.", "name")

        host = _text(pick("host"), "host", max_len=253, required=True)
        try:
            self._validate_host(host)
        except address_policy.AddressPolicyError as error:
            raise SourceValidationError(error.code, error.message, "host")

        port = pick("port", 554)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise SourceValidationError("invalid_field", "port must be a number from 1 to 65535.", "port")

        username = _text(pick("username"), "username", max_len=64)
        if ":" in username or "@" in username:
            raise SourceValidationError("invalid_field", "username must not contain ':' or '@'.", "username")

        paths = {}
        for key in ("main_path", "sub_path"):
            value = _text(pick(key), key, max_len=256)
            if value and ("://" in value or value.lower().startswith("rtsp:")):
                # a pasted full address: say so instead of a generic "not valid"
                raise SourceValidationError("path_is_url", f"{key} must be only the part after the port, not the full rtsp:// address.", key)
            if value and not rtsp_input.valid_stream_path(value):
                raise SourceValidationError("invalid_field", f"{key} contains characters that are not allowed.", key)
            paths[key] = value

        profile = pick("preferred_profile", "sub")
        if profile not in PROFILES:
            raise SourceValidationError("invalid_field", "preferred_profile must be 'sub' or 'main'.", "preferred_profile")
        if not paths["sub_path" if profile == "sub" else "main_path"]:
            raise SourceValidationError("missing_field", f"The {profile} path is required for the preferred profile.", f"{profile}_path")

        transport = pick("transport", "tcp")
        if transport not in TRANSPORTS:
            raise SourceValidationError("invalid_field", "transport must be 'tcp' or 'udp'.", "transport")

        timeout = pick("connect_timeout_s", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 2 <= timeout <= 60:
            raise SourceValidationError("invalid_field", "connect_timeout_s must be 2-60 seconds.", "connect_timeout_s")

        control = self._clean_control(pick("control", None), existing, host)

        return {
            "name": name, "kind": KIND_RTSP, "host": host, "port": port, "username": username,
            "main_path": paths["main_path"], "sub_path": paths["sub_path"],
            "preferred_profile": profile, "transport": transport, "connect_timeout_s": timeout,
            "control": control,
        }

    @staticmethod
    def _clean_control(value: Any, existing: dict | None, host: str) -> dict:
        old = {**default_control(), **((existing or {}).get("control") or {})}
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise SourceValidationError("invalid_field", "control must be an object.", "control")
        if value is not old:  # the stored copy (returned unchanged by pick()) is already clean
            unknown = sorted(set(value) - set(CONTROL_INPUT_FIELDS) - {"pinned_sha256", "capabilities"})
            if unknown:
                raise SourceValidationError("unknown_field", "Unknown control field: " + unknown[0], "control")
        merged = {**old, **{k: v for k, v in value.items() if k in CONTROL_INPUT_FIELDS}}
        if merged["type"] not in CONTROL_TYPES:
            raise SourceValidationError("invalid_field", "control type must be 'none' or 'reolink'.", "control")
        if merged["scheme"] not in CONTROL_SCHEMES:
            raise SourceValidationError("invalid_field", "control scheme must be 'https' or 'http'.", "control")
        port = merged["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise SourceValidationError("invalid_field", "control port must be a number from 1 to 65535.", "control_port")
        channel = merged["channel"]
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 31:
            raise SourceValidationError("invalid_field", "control channel must be 0-31.", "control_channel")
        if merged["snapshot_quality"] not in SNAPSHOT_QUALITIES:
            raise SourceValidationError("invalid_field", "snapshot_quality must be 'full' or 'small'.", "control_snapshot_quality")
        if not isinstance(merged["allow_http"], bool):
            raise SourceValidationError("invalid_field", "allow_http must be true or false.", "control")
        if merged["type"] != "none" and merged["scheme"] == "http" and not merged["allow_http"]:
            raise SourceValidationError("http_not_allowed", "Plain HTTP needs an explicit opt-in (allow_http).", "control")
        clean = {k: merged[k] for k in CONTROL_INPUT_FIELDS}
        clean["pinned_sha256"] = old["pinned_sha256"]
        clean["capabilities"] = old["capabilities"]
        # a pin and a probe result belong to one device and one address: any change of where we talk to clears them
        moved = existing is not None and (existing.get("host") != host or old["port"] != port or old["scheme"] != merged["scheme"]
                                          or old["channel"] != channel or old["type"] != merged["type"])
        if moved:
            clean["pinned_sha256"], clean["capabilities"] = "", {}
        return clean

    @staticmethod
    def _clean_password(value: Any) -> str:
        if not isinstance(value, str):
            raise SourceValidationError("invalid_field", "password must be text.", "password")
        if _CTRL.search(value) or len(value) > 128:
            raise SourceValidationError("invalid_field", "password must be at most 128 characters without control characters.", "password")
        return value

    # -- mutation ----------------------------------------------------------

    def create(self, fields: dict, password: str | None = None) -> dict:
        with self._lock:
            old_sources, old_secrets = self._load()
            if len(old_sources) >= MAX_SOURCES:
                raise SourceValidationError("too_many_sources", f"At most {MAX_SOURCES} camera sources can be saved.")
            clean = self._clean(fields, None, old_sources)
            source = {"id": "ip:" + uuid.uuid4().hex, **clean, "created_at": _now(), "updated_at": _now()}
            new_sources = old_sources + [source]
            new_secrets = copy.deepcopy(old_secrets)
            if password:
                new_secrets[source["id"]] = {"password": self._clean_password(password)}
            self._commit(new_sources, new_secrets, old_sources, old_secrets)
            return self._public(source, new_secrets)

    def update(self, source_id: str, fields: dict, password: Any = UNSET) -> dict:
        """``password``: UNSET keeps it, None or "" clears it, any other text replaces it."""
        with self._lock:
            old_sources, old_secrets = self._load()
            index = next((i for i, s in enumerate(old_sources) if s["id"] == source_id), None)
            if index is None:
                raise SourceNotFound(source_id)
            existing = old_sources[index]
            clean = self._clean(fields, existing, old_sources)
            updated = {**existing, **clean, "updated_at": _now()}
            new_sources = list(old_sources)
            new_sources[index] = updated
            new_secrets = copy.deepcopy(old_secrets)
            if password is not UNSET:
                if password:
                    new_secrets[source_id] = {"password": self._clean_password(password)}
                else:
                    new_secrets.pop(source_id, None)
            self._commit(new_sources, new_secrets, old_sources, old_secrets)
            return self._public(updated, new_secrets)

    def set_control_state(self, source_id: str, *, pinned_sha256: Any = UNSET, capabilities: Any = UNSET) -> dict:
        """Server-side writes of the control pin and the probe result (never from user input directly)."""
        with self._lock:
            old_sources, old_secrets = self._load()
            index = next((i for i, s in enumerate(old_sources) if s["id"] == source_id), None)
            if index is None:
                raise SourceNotFound(source_id)
            control = {**default_control(), **(old_sources[index].get("control") or {})}
            if pinned_sha256 is not UNSET:
                control["pinned_sha256"] = pinned_sha256
            if capabilities is not UNSET:
                control["capabilities"] = capabilities
            updated = {**old_sources[index], "control": control}  # updated_at is for user edits: the control client is keyed on it
            new_sources = list(old_sources)
            new_sources[index] = updated
            self._commit(new_sources, old_secrets, old_sources, old_secrets)
            return self._public(updated, old_secrets)

    def delete(self, source_id: str) -> None:
        with self._lock:
            old_sources, old_secrets = self._load()
            new_sources = [s for s in old_sources if s["id"] != source_id]
            if len(new_sources) == len(old_sources):
                raise SourceNotFound(source_id)
            new_secrets = {k: v for k, v in old_secrets.items() if k != source_id}
            self._commit(new_sources, new_secrets, old_sources, old_secrets)
