"""Persistence of a TCP "Release radio" (RR1).

A released TCP radio must stay released across a service/container restart:
"until I reconnect" has no end time, a timed release keeps its ABSOLUTE end time.
The record lives next to the rest of the radio profile (data/profiles/<id>/
radio_release.json), written atomically through storage.json_store. The path is
looked up lazily because the active profile is only resolved at import time of
server.py.

Only TCP releases are stored. A serial release is deliberately in-memory only.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from storage.json_store import safe_read_json, safe_write_json

FILE_NAME = "radio_release.json"


class RadioReleaseStore:
    def __init__(self, profile_dir_provider: Callable[[], str]) -> None:
        self._profile_dir_provider = profile_dir_provider

    def _path(self) -> Optional[str]:
        profile_dir = self._profile_dir_provider()
        return os.path.join(profile_dir, FILE_NAME) if profile_dir else None

    def load(self) -> Optional[dict]:
        path = self._path()
        if not path or not os.path.exists(path):
            return None
        data = safe_read_json(path, None)
        return data if isinstance(data, dict) else None

    def save(self, record: dict) -> bool:
        path = self._path()
        if not path:
            return False
        return bool(safe_write_json(path, dict(record)))

    def clear(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"[RADIO RELEASE] Could not remove {path}: {error}", flush=True)
