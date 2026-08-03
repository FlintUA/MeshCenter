#!/usr/bin/env python3
"""Profile-scoped peripheral assignments for MeshCenter."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict


class DeviceManager:
    SCHEMA_VERSION = 1

    def __init__(self, profile_dir: str):
        self.profile_dir = os.path.abspath(profile_dir)
        self.path = os.path.join(self.profile_dir, "devices.json")
        self._lock = threading.RLock()
        os.makedirs(self.profile_dir, exist_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _default(self) -> Dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": self._now(),
            "devices": {
                "camera": {
                    "type": "camera",
                    "assigned": True,
                    "enabled": True,
                    "source": "csi",
                    "model": "",
                },
                "environment": {
                    "type": "sensor",
                    "assigned": True,
                    "enabled": True,
                    "driver": "",
                },
                "power": {
                    "type": "sensor",
                    "assigned": True,
                    "enabled": True,
                    "driver": "",
                },
            },
        }

    def load_or_create(self) -> Dict[str, Any]:
        with self._lock:
            data = self._default()
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data.update({k: v for k, v in loaded.items() if k != "devices"})
                    if isinstance(loaded.get("devices"), dict):
                        for key, value in loaded["devices"].items():
                            if isinstance(value, dict):
                                data["devices"].setdefault(key, {}).update(value)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError):
                # Keep a usable default; a malformed file is replaced atomically.
                pass

            data["schema_version"] = self.SCHEMA_VERSION
            self.save(data)
            return deepcopy(data)

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            payload = deepcopy(data if isinstance(data, dict) else self._default())
            payload["schema_version"] = self.SCHEMA_VERSION
            payload["updated_at"] = self._now()
            os.makedirs(self.profile_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".devices.", suffix=".tmp", dir=self.profile_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            return deepcopy(payload)
