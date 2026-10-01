#!/usr/bin/env python3
"""Profile-scoped peripheral assignments for MeshCenter."""

from __future__ import annotations

import os
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict

from storage.json_store import safe_read_json, safe_write_json


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
            # F4.1: folded onto the shared storage.json_store helper - a
            # malformed devices.json is now quarantined (moved aside, not
            # silently discarded) instead of just being overwritten by the
            # save() call below with no trace of the original left behind.
            loaded = safe_read_json(self.path, default={})
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

            data["schema_version"] = self.SCHEMA_VERSION
            self.save(data)
            return deepcopy(data)

    def save(self, data: Dict[str, Any]) -> bool:
        """F4.1 PR 2: returns whether the write actually succeeded
        (previously always returned a deepcopy of the payload - truthy no
        matter what safe_write_json() did - so every caller that never
        checked it had no way to tell a failure from a success). No
        existing caller used the old return value for anything beyond
        discarding it or (load_or_create()) building its own, independent
        return value, so this is a safe, non-breaking contract change."""
        with self._lock:
            payload = deepcopy(data if isinstance(data, dict) else self._default())
            payload["schema_version"] = self.SCHEMA_VERSION
            payload["updated_at"] = self._now()
            # F4.1: folded onto the shared storage.json_store helper -
            # same unique-temp-name+fsync mechanism this already had
            # inline, now de-duplicated into one place.
            return safe_write_json(self.path, payload)
