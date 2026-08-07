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

from storage.json_store import safe_write_json


INSTANCE_SCHEMA_VERSION = 1


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
        """Return schema-v1 data from either the old flat or current structure."""
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

        return {
            "schema_version": INSTANCE_SCHEMA_VERSION,
            "instance_name": instance_name,
            "hostname": hostname,
            "active_profile_id": active_profile_id,
            "radio": radio,
            "runtime": runtime,
        }

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
                elif raw.get("schema_version") != INSTANCE_SCHEMA_VERSION or "radio" not in raw or "runtime" not in raw:
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

    def save(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and atomically persist supplied identity data."""
        with self._lock:
            normalized = self._normalize(data, self._data)
            if not safe_write_json(str(self.path), normalized):
                raise RuntimeError(f"Could not save MeshCenter instance identity: {self.path}")
            self._data = normalized
            return copy.deepcopy(self._data)
