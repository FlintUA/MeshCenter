#!/usr/bin/env python3
"""Radio-scoped storage profiles for MeshCenter.

The manager keeps Raspberry Pi / instance data in ``data/`` and moves only
radio-specific state under ``data/profiles/<node-id>/``.  It never accepts or
switches to a different radio automatically.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from meshsrv.radio_connections import remember_connection, set_preferred_transport
from meshsrv.radio_endpoint import normalize_radio_record
from storage.json_store import safe_read_json, safe_write_json

PROFILE_SCHEMA_VERSION = 1

PROFILE_FILES = {
    "messages": "messages.json",
    "nodes": "nodes.json",
    "sensors": "sensors.json",
    "chats": "chats.json",
    "deleted_dm": "deleted_dm.json",
    "telemetry_history": "telemetry_history.json",
    "waypoints_db": "waypoints.db",
    "node_debug": "nodes_debug.log",
}

PROFILE_DIRECTORIES = {
    "node_icons": "node_icons",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ProfileManager:
    """Create, migrate and resolve one accepted radio profile."""

    def __init__(self, data_dir: str | os.PathLike) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.profiles_dir = (self.data_dir / "profiles").resolve()
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def profile_id_from_node_id(node_id: Any) -> str:
        text = str(node_id or "").strip().lower()
        match = re.fullmatch(r"!([0-9a-f]{8})", text)
        if not match:
            raise ValueError(f"Invalid Meshtastic node ID for profile: {node_id!r}")
        return match.group(1)

    def _profile_dir(self, profile_id: str) -> Path:
        clean = str(profile_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8}", clean):
            raise ValueError(f"Invalid profile ID: {profile_id!r}")
        path = (self.profiles_dir / clean).resolve()
        if path.parent != self.profiles_dir:
            raise ValueError("Profile path escaped profiles directory")
        return path

    def path(self, profile_id: str, key: str) -> str:
        if key not in PROFILE_FILES:
            raise KeyError(f"Unknown profile file key: {key}")
        return str(self._profile_dir(profile_id) / PROFILE_FILES[key])

    def directory(self, profile_id: str, key: str) -> str:
        if key not in PROFILE_DIRECTORIES:
            raise KeyError(f"Unknown profile directory key: {key}")
        return str(self._profile_dir(profile_id) / PROFILE_DIRECTORIES[key])

    def _profile_metadata(self, profile_id: str, radio: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
        existing = dict(existing or {})
        created_at = str(existing.get("created_at") or "").strip() or now_iso()
        existing_radio = existing.get("radio") if isinstance(existing.get("radio"), dict) else {}

        # normalize_radio_record() defaults a missing/legacy transport to
        # "serial" with endpoint={"port": <the flat `port` field below>} -
        # self-normalizing here (rather than trusting every caller to have
        # done it already) means create_clean_profile()/ensure_profile()
        # are safe to call with a bare detected-radio dict (no transport/
        # endpoint keys at all) exactly as every existing call site already
        # does, and still get a correctly-shaped stored record. See
        # meshsrv/radio_endpoint.py's own module docstring for the full
        # backward-compatibility contract this preserves.
        #
        # MERGE, not overwrite (Radio Profiles & Connections Model, PR 1):
        # `radio` here is only ever THIS call's single freshly-detected
        # transport/endpoint (ensure_profile() is called with exactly one
        # at a time - boot-time restore, or one Node Manager accept). A
        # plain normalize_radio_record(radio) would know nothing about a
        # DIFFERENT transport this same profile already remembered from an
        # earlier accept - remember_connection() against the EXISTING
        # stored record is what preserves it instead of silently dropping
        # it (the actual fix behind "accept over TCP, later also connect
        # over serial, must remember both, not just avoid a duplicate
        # profile dir" - the dir-per-node_id behavior below already
        # prevented the duplicate; it never preserved the other transport).
        #
        # set_preferred_transport() (not record_success()): this call
        # doesn't know whether a real connect just succeeded (boot-time
        # restore calls this with the already-accepted radio before ever
        # attempting to connect) - only that `radio`'s transport is the
        # one now being treated as current/accepted, matching this
        # function's own pre-existing "last write wins" behavior for the
        # legacy singular transport/endpoint fields. Recording an actual
        # connection SUCCESS is the caller's job, once it knows one
        # occurred (see api/api_meshtastic.py's _persist_choice()).
        incoming = normalize_radio_record(radio)
        if existing_radio:
            merged = remember_connection(existing_radio, incoming["transport"], incoming["endpoint"])
            merged = set_preferred_transport(merged, incoming["transport"])
        else:
            # Brand new profile - nothing to merge WITH yet.
            # remember_connection(existing_radio, ...) would otherwise run
            # normalize_radio_record({}) internally, which synthesizes its
            # own phantom "serial" connections entry (empty
            # existing_radio's own pre-existing default-to-serial
            # behavior - see meshsrv/radio_endpoint.py's
            # normalize_radio_record() docstring) alongside the real one
            # being remembered here - live-caught by this PR's own tests
            # (a first-ever TCP profile ended up with a bogus
            # connections["serial"] = {"endpoint": {"port": ""}} entry
            # that was never real).
            merged = incoming

        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "radio": {
                "node_id": str(radio.get("node_id") or "").strip(),
                "long_name": str(radio.get("long_name") or "").strip(),
                "short_name": str(radio.get("short_name") or "").strip(),
                "hardware": str(radio.get("hardware") or "").strip(),
                "role": str(radio.get("role") or "").strip(),
                "firmware_version": str(radio.get("firmware_version") or "").strip(),
                "port": str(radio.get("port") or "").strip(),
                "transport": merged["transport"],
                "endpoint": merged["endpoint"],
                "connections": merged["connections"],
                "preferred_transport": merged["preferred_transport"],
                "last_successful_transport": merged["last_successful_transport"],
            },
            "created_at": created_at,
            "last_used_at": now_iso(),
            "migration": dict(existing.get("migration") or {}),
        }

    def ensure_profile(self, radio: Mapping[str, Any], migrate_legacy: bool = True) -> dict[str, Any]:
        """Create/update the accepted radio profile and optionally migrate legacy files."""
        with self._lock:
            profile_id = self.profile_id_from_node_id(radio.get("node_id"))
            profile_dir = self._profile_dir(profile_id)
            profile_dir.mkdir(parents=True, exist_ok=True)
            for dirname in PROFILE_DIRECTORIES.values():
                (profile_dir / dirname).mkdir(parents=True, exist_ok=True)

            metadata_path = profile_dir / "profile.json"
            existing = safe_read_json(str(metadata_path), {})
            if not isinstance(existing, dict):
                existing = {}
            metadata = self._profile_metadata(profile_id, radio, existing)

            migration_result = {"performed": False, "migrated": [], "backups": [], "errors": []}
            if migrate_legacy and not bool((metadata.get("migration") or {}).get("legacy_data_migrated")):
                migration_result = self._migrate_legacy(profile_id)
                metadata["migration"] = {
                    "legacy_data_migrated": not migration_result["errors"],
                    "migrated_at": now_iso(),
                    "files": migration_result["migrated"],
                    "errors": migration_result["errors"],
                }

            if not safe_write_json(str(metadata_path), metadata):
                raise RuntimeError(f"Could not save radio profile metadata: {metadata_path}")

            return {
                "profile_id": profile_id,
                "profile_dir": str(profile_dir),
                "metadata": metadata,
                "migration": migration_result,
                "paths": {key: self.path(profile_id, key) for key in PROFILE_FILES},
                "directories": {key: self.directory(profile_id, key) for key in PROFILE_DIRECTORIES},
            }


    def create_clean_profile(self, radio: Mapping[str, Any]) -> dict[str, Any]:
        """Create a new empty profile without copying another radio's data."""
        context = self.ensure_profile(radio, migrate_legacy=False)
        profile_dir = Path(context["profile_dir"])

        empty_json_defaults = {
            "messages.json": [],
            "nodes.json": {},
            "sensors.json": {},
            "chats.json": {},
            "deleted_dm.json": [],
            "telemetry_history.json": [],
        }
        for filename, default in empty_json_defaults.items():
            path = profile_dir / filename
            if not path.exists() and not safe_write_json(str(path), default):
                raise RuntimeError(f"Could not initialize profile file: {path}")

        debug_path = profile_dir / "nodes_debug.log"
        if not debug_path.exists():
            debug_path.touch()

        # waypoints.db is created lazily by WaypointStore after restart.
        return self.get_profile(context["profile_id"])



    def get_profile(self, profile_id: str) -> dict[str, Any]:
        """Return validated profile metadata and paths for an existing profile."""
        with self._lock:
            profile_dir = self._profile_dir(profile_id)
            metadata_path = profile_dir / "profile.json"
            if not profile_dir.is_dir() or not metadata_path.is_file():
                raise FileNotFoundError(f"Radio profile not found: {profile_id}")

            metadata = safe_read_json(str(metadata_path), {})
            if not isinstance(metadata, dict):
                raise RuntimeError(f"Invalid radio profile metadata: {metadata_path}")

            stored_id = str(metadata.get("profile_id") or profile_id).strip().lower()
            if stored_id != str(profile_id).strip().lower():
                raise RuntimeError("Radio profile ID does not match its directory")

            return {
                "profile_id": stored_id,
                "profile_dir": str(profile_dir),
                "metadata": metadata,
                "paths": {key: self.path(stored_id, key) for key in PROFILE_FILES},
                "directories": {key: self.directory(stored_id, key) for key in PROFILE_DIRECTORIES},
            }

    def _migrate_legacy(self, profile_id: str) -> dict[str, Any]:
        """Copy legacy radio state into the profile, verify it, then retain backups."""
        profile_dir = self._profile_dir(profile_id)
        result = {"performed": True, "migrated": [], "backups": [], "errors": []}

        for filename in PROFILE_FILES.values():
            source = self.data_dir / filename
            target = profile_dir / filename
            backup = self.data_dir / f"{filename}.pre_profiles_backup"
            try:
                if source.exists() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    if not target.exists() or target.stat().st_size != source.stat().st_size:
                        raise RuntimeError("copy verification failed")
                    result["migrated"].append(filename)
                if source.exists() and target.exists() and not backup.exists():
                    source.replace(backup)
                    result["backups"].append(backup.name)
            except Exception as error:
                result["errors"].append(f"{filename}: {error}")

        for dirname in PROFILE_DIRECTORIES.values():
            source = self.data_dir / dirname
            target = profile_dir / dirname
            backup = self.data_dir / f"{dirname}.pre_profiles_backup"
            try:
                if source.exists() and not target.exists():
                    shutil.copytree(source, target)
                    result["migrated"].append(dirname + "/")
                elif source.exists() and target.exists():
                    target.mkdir(parents=True, exist_ok=True)
                    for child in source.iterdir():
                        destination = target / child.name
                        if not destination.exists():
                            if child.is_dir():
                                shutil.copytree(child, destination)
                            else:
                                shutil.copy2(child, destination)
                if source.exists() and target.exists() and not backup.exists():
                    source.replace(backup)
                    result["backups"].append(backup.name)
            except Exception as error:
                result["errors"].append(f"{dirname}/: {error}")

        return result
