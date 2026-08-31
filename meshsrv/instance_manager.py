#!/usr/bin/env python3
"""Persistent identity storage for one MeshCenter installation.

This module deliberately does not probe or reconfigure a Meshtastic radio.
It only owns the accepted instance identity stored in ``data/instance.json``.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Mapping

from meshsrv.installation_identity import generate_installation_id, is_valid_installation_id
from storage.json_store import safe_write_json


INSTANCE_SCHEMA_VERSION = 2


class InstanceManager:
    """Load, validate, migrate and atomically save MeshCenter identity data."""

    def __init__(self, filepath: str | os.PathLike) -> None:
        self.path = Path(filepath).expanduser().resolve()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _datetime_text(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        # Preserve ISO-8601 timestamps. Legacy numeric timestamps are kept as text
        # rather than discarded, so migration never loses detection history.
        return text

    @staticmethod
    def _default_instance_name(hostname: str) -> str:
        clean_hostname = str(hostname or "").strip()
        return f"MeshCenter {clean_hostname}" if clean_hostname else "MeshCenter"

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError) as error:
            print(f"[INSTANCE] Could not read {self.path}: {error}", flush=True)
            return {}

    def _normalize(self, raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
        """Return schema-v2 data from either the old flat/v1 or current structure."""
        raw = dict(raw or {})
        defaults = dict(defaults or {})

        nested_radio = raw.get("radio") if isinstance(raw.get("radio"), dict) else {}
        nested_runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
        configured = raw.get("configured") if isinstance(raw.get("configured"), dict) else {}
        default_radio = defaults.get("radio") if isinstance(defaults.get("radio"), dict) else {}
        default_runtime = defaults.get("runtime") if isinstance(defaults.get("runtime"), dict) else {}

        hostname = self._text(raw.get("hostname")) or self._text(defaults.get("hostname")) or socket.gethostname()
        instance_name = self._text(raw.get("instance_name")) or self._text(defaults.get("instance_name"))
        if not instance_name:
            instance_name = self._default_instance_name(hostname)

        def first_text(*values: Any) -> str:
            for value in values:
                text = self._text(value)
                if text:
                    return text
            return ""

        radio = {
            "node_id": first_text(
                nested_radio.get("node_id"), raw.get("node_id"), configured.get("node_id"), default_radio.get("node_id")
            ),
            "long_name": first_text(
                nested_radio.get("long_name"), raw.get("long_name"), configured.get("long_name"), default_radio.get("long_name")
            ),
            "short_name": first_text(nested_radio.get("short_name"), raw.get("short_name"), default_radio.get("short_name")),
            "hardware": first_text(nested_radio.get("hardware"), raw.get("hardware"), default_radio.get("hardware")),
            "role": first_text(nested_radio.get("role"), raw.get("role"), default_radio.get("role")),
            "port": first_text(
                nested_radio.get("port"), nested_radio.get("serial_port"), raw.get("serial_port"), default_radio.get("port")
            ),
            "firmware_version": first_text(
                nested_radio.get("firmware_version"), raw.get("firmware_version"), default_radio.get("firmware_version")
            ),
        }

        detected_radio = nested_runtime.get("last_detected_radio")
        if not isinstance(detected_radio, dict):
            detected_radio = default_runtime.get("last_detected_radio")
        if not isinstance(detected_radio, dict):
            detected_radio = {}

        runtime = {
            "cli_path": first_text(nested_runtime.get("cli_path"), raw.get("cli_path"), default_runtime.get("cli_path")),
            "last_detected_at": self._datetime_text(
                nested_runtime.get("last_detected_at", raw.get("last_detected_at", default_runtime.get("last_detected_at")))
            ),
            "identity_status": first_text(
                nested_runtime.get("identity_status"), raw.get("identity_status"), default_runtime.get("identity_status")
            ) or "NOT_CHECKED",
            "last_error": first_text(
                nested_runtime.get("last_error"), raw.get("last_error"), default_runtime.get("last_error")
            ) or None,
            "last_detected_radio": {
                "node_id": first_text(detected_radio.get("node_id")),
                "long_name": first_text(detected_radio.get("long_name")),
                "short_name": first_text(detected_radio.get("short_name")),
                "hardware": first_text(detected_radio.get("hardware")),
                "role": first_text(detected_radio.get("role")),
                "port": first_text(detected_radio.get("port")),
                "firmware_version": first_text(detected_radio.get("firmware_version")),
            },
        }

        active_profile_id = first_text(raw.get("active_profile_id"), defaults.get("active_profile_id"))
        installation = self._normalize_installation(raw, defaults, had_any_data=bool(raw))

        return {
            "schema_version": INSTANCE_SCHEMA_VERSION,
            "instance_name": instance_name,
            "hostname": hostname,
            "active_profile_id": active_profile_id,
            "radio": radio,
            "runtime": runtime,
            "installation": installation,
        }

    def _normalize_installation(
        self, raw: Mapping[str, Any], defaults: Mapping[str, Any], had_any_data: bool
    ) -> dict[str, Any]:
        """Return schema-v2 installation identity: an ID generated once, then
        forward-carried on every later normalize pass (see _normalize()'s
        callers - load_or_create() runs this once at startup, save() runs it
        on every profile switch / radio accept, so this must never regenerate
        an already-valid ID on a routine pass, only on a genuinely missing or
        invalid one)."""
        nested = raw.get("installation") if isinstance(raw.get("installation"), dict) else {}
        default_installation = defaults.get("installation") if isinstance(defaults.get("installation"), dict) else {}

        existing_id = self._text(nested.get("id")) or self._text(default_installation.get("id"))

        if is_valid_installation_id(existing_id):
            return {
                "id": existing_id,
                "assigned_at": self._datetime_text(nested.get("assigned_at", default_installation.get("assigned_at"))),
                "time_source": self._text(nested.get("time_source")) or self._text(default_installation.get("time_source")) or "pending",
                "assignment_reason": (
                    self._text(nested.get("assignment_reason"))
                    or self._text(default_installation.get("assignment_reason"))
                    or ("migration" if had_any_data else "fresh_install")
                ),
            }

        # No valid ID found - either nothing was there (fresh install / a
        # genuine v1 file predating this field), or something was there but
        # failed format validation (corrupted/tampered file). Both mint a new
        # ID with assignment_reason "migration"/"fresh_install" (spec doesn't
        # define a distinct enum value for the corrupted case), but the
        # corrupted case additionally gets a WARNING system-log entry so it's
        # not fully silent - a replaced ID is more noteworthy than an
        # ordinary v1-to-v2 migration, even though the reason field can't
        # distinguish them.
        if existing_id:
            self._log_corrupted_id_replaced(existing_id)

        return {
            "id": generate_installation_id(),
            "assigned_at": None,
            "time_source": "pending",
            "assignment_reason": "migration" if had_any_data else "fresh_install",
        }

    def _log_corrupted_id_replaced(self, invalid_id: str) -> None:
        try:
            from system_log import log_system_event  # noqa: PLC0415 - lazy: keeps this module config.py-free at import time

            log_system_event(
                "Installation ID replaced",
                "WARNING",
                f"Stored installation ID failed format validation and was regenerated: {invalid_id!r}",
                source="instance",
            )
        except Exception as error:
            print(f"[INSTANCE] Could not log installation ID replacement: {error}", flush=True)

    def load_or_create(self, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Load the file, migrate legacy data and persist the normalized schema."""
        with self._lock:
            raw = self._read_raw()
            normalized = self._normalize(raw, defaults or {})
            if raw != normalized or not self.path.exists():
                if not safe_write_json(str(self.path), normalized):
                    raise RuntimeError(f"Could not save MeshCenter instance identity: {self.path}")
                if not raw:
                    print(f"[INSTANCE] Created instance identity: {self.path}", flush=True)
                elif (
                    raw.get("schema_version") != INSTANCE_SCHEMA_VERSION
                    or "radio" not in raw
                    or "runtime" not in raw
                    or "installation" not in raw
                ):
                    print(
                        f"[INSTANCE] Migrated instance identity to schema {INSTANCE_SCHEMA_VERSION}: {self.path}",
                        flush=True,
                    )
                else:
                    print(f"[INSTANCE] Normalized instance identity: {self.path}", flush=True)
            else:
                print(f"[INSTANCE] Loaded instance identity: {self.path}", flush=True)
            self._data = normalized
            return copy.deepcopy(self._data)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def peek(self) -> dict[str, Any] | None:
        """Read the file's raw "installation" block, if any, WITHOUT
        writing, normalizing, or generating anything - safe to call on a
        missing or corrupted file without destroying it (unlike
        load_or_create(), which self-heals by writing a freshly-normalized
        replacement the moment raw data doesn't match its normalized form -
        exactly what a missing/corrupted file always produces).

        Deliberately does not route through _normalize(): that function
        calls generate_installation_id() (a different random value on every
        call) and logs a WARNING via _log_corrupted_id_replaced() whenever
        it sees an invalid existing id - both are real side effects that a
        genuinely read-only peek must never trigger. Returns the stored
        sub-dict verbatim, unvalidated - for a corrupted file, this is the
        diagnostic value: showing exactly what's actually stored, not a
        fabricated replacement.

        Returns None if the file doesn't exist, isn't valid JSON, or has no
        "installation" key yet - callers should treat that as "not yet
        initialized", not attempt to interpret a placeholder.
        """
        raw = self._read_raw()
        installation = raw.get("installation")
        return dict(installation) if isinstance(installation, dict) else None

    def save(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and atomically persist supplied identity data."""
        with self._lock:
            normalized = self._normalize(data, self._data)
            if not safe_write_json(str(self.path), normalized):
                raise RuntimeError(f"Could not save MeshCenter instance identity: {self.path}")
            self._data = normalized
            return copy.deepcopy(self._data)
