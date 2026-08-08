"""Registry + active-provider dispatcher for weather backends.

Mirrors the shape of the planned camera device-driver registry (see the
project's usb-camera-plan notes) at the interface level - a dict of
providers keyed by stable id, one of them "active" at a time - without
sharing any code with that camera work; the two are structurally similar,
not related.
"""

from __future__ import annotations

from .providers.base import WeatherProvider


class WeatherManager:
    def __init__(self, providers: dict[str, WeatherProvider], active_id: str):
        if active_id not in providers:
            raise ValueError(f"Unknown weather provider id: {active_id!r}")
        self._providers = providers
        self._active_id = active_id

    @property
    def active_id(self) -> str:
        return self._active_id

    def active(self) -> WeatherProvider:
        return self._providers[self._active_id]

    def set_active(self, provider_id: str) -> None:
        if provider_id not in self._providers:
            raise ValueError(f"Unknown weather provider id: {provider_id!r}")
        self._active_id = provider_id
