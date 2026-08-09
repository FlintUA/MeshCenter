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
    # v1 -> v2: a single "devices.camera" object became "devices.cameras",
    # a dict keyed by CameraDriver id (see camera/camera_manager.py), plus
    # a top-level "active_camera_id" - multiple cameras (CSI + USB) can now
    # be registered at once, only one of them active. See load_or_create()
    # for the migration of an existing v1 file.
    SCHEMA_VERSION = 2

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
            # Which CameraDriver id (camera_manager.CameraManager.active_id)
            # was last selected - restored at startup so the active camera
            # survives a restart. None until a camera has actually been
            # discovered and selected at least once.
            "active_camera_id": None,
            "devices": {
                # Keyed by CameraDriver.id, e.g. "csi" or
                # "usb:046d:09a4:video0" - not pre-populated with a default
                # entry the way environment/power are, since which cameras
                # exist is only known after runtime discovery.
                "cameras": {},
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
                    loaded_devices = loaded.get("devices")
                    if isinstance(loaded_devices, dict):
                        # Schema v1 -> v2 migration: the old single "camera"
                        # object becomes one entry in "cameras", keyed
                        # "csi" - that was the only camera type that could
                        # exist under v1, so this is an unambiguous rename,
                        # not a guess.
                        legacy_camera = loaded_devices.get("camera")
                        if isinstance(legacy_camera, dict) and "cameras" not in loaded_devices:
                            data["devices"]["cameras"]["csi"] = {
                                k: v for k, v in legacy_camera.items() if k != "type"
                            }

                        for key, value in loaded_devices.items():
                            if key == "camera":
                                continue  # migrated above; don't also recreate the old flat key
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
