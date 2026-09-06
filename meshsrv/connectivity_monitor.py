"""meshsrv/connectivity_monitor.py

ADR-0008 (Step 1.6A backend layer): the real, HTTPS-based connectivity
signal `sender.py`/`receiver.py`'s `network_available` parameter and the
Files-tab/Settings connectivity indicators (design spec 17.2, 17.6) both
need. This module answers one specific question - "is a real HTTPS
request to a Relay this workspace depends on going to succeed right now"
- which `api_system.py`'s existing `/api/system/network` (a single
`ping -c 1 -W 1 8.8.8.8`) cannot: ICMP to a fixed IP says nothing about
whether *this* Relay's HTTPS endpoint is reachable, and a Relay can
easily be reachable while general ICMP is filtered, or vice versa.
`/api/system/network` is unchanged and keeps serving the general
Wi-Fi/gateway panel - this module is additive, not a replacement.

One `ConnectivityMonitor` per process, constructed once alongside the
workspace's `ProviderRegistry` at server startup. `snapshot()` is a
cheap, non-blocking read of the last computed result; only `refresh()`
(called from `AttachmentsService`'s own tick loop - never from an HTTP
request handler) performs actual network I/O, so `GET
/api/mca/connectivity` (Step 1.6A) never blocks a Flask worker on a
slow or dead Relay.

`can_attempt_relay()` is advisory, not a hard gate: a real HTTPS attempt
inside `sender.run_step()`/`receiver.run_step()` remains the final
authority on success or failure regardless of what this monitor last
reported. A stale `unreachable` reading must never permanently block an
attempt - it only skips *this* tick's attempt, and the natural
retry/backoff already built into both state machines tries again later.
"""

from __future__ import annotations

import dataclasses
import enum
import time
from typing import Any, Dict, Optional

import requests

from meshsrv.attachments.provider_registry import ProviderProfile, ProviderRegistry, b64url_encode

DEFAULT_TIMEOUT_SECONDS = 5.0

# The fast, frequent per-Relay probe interval, and how far it backs off
# after consecutive failures - named constants so Step 1.9's real Pi
# Zero 2 W hardware pass can tune them with evidence rather than guessing
# (ADR-0008 flags this as an open uncertainty).
RELAY_HEALTH_INTERVAL_SECONDS = 60
RELAY_HEALTH_BACKOFF_CEILING_SECONDS = 300

# The slow, rare /v1/info probe only needs to run when something about
# the profile's identity could have changed - not on every health tick.
RELAY_INFO_MIN_INTERVAL_SECONDS = 3600

# Used only when no provider is registered yet (first-run state, before
# any profile exists to health-check against) - a plain, documented,
# operator-configurable placeholder. Revisit if this needs to be a
# Settings-configurable value instead of a code constant.
FALLBACK_INTERNET_CHECK_URL = "https://www.gstatic.com/generate_204"


class InternetStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    ONLINE = "online"
    OFFLINE = "offline"
    LIMITED = "limited"


class RelayState(str, enum.Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    ONLINE = "online"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    IDENTITY_MISMATCH = "identity_mismatch"
    INCOMPATIBLE = "incompatible"
    DISABLED = "disabled"


# States an attempt is worth making for (ADR-0008 decision 2).
_ATTEMPTABLE_RELAY_STATES = frozenset({RelayState.ONLINE, RelayState.DEGRADED})


class UploadReadiness(str, enum.Enum):
    READY = "ready"
    UPLOAD_TOKEN_MISSING = "upload_token_missing"
    UPLOAD_DISABLED = "upload_disabled"
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclasses.dataclass(frozen=True)
class RelayStatus:
    provider_id: str
    state: RelayState
    upload_readiness: UploadReadiness
    checked_at: Optional[float]
    latency_ms: Optional[int]
    error_code: Optional[str]


@dataclasses.dataclass(frozen=True)
class ConnectivitySnapshot:
    internet: InternetStatus
    relays: Dict[str, RelayStatus]


def _upload_readiness_for(profile: ProviderProfile) -> UploadReadiness:
    if not profile.upload_allowed:
        return UploadReadiness.UPLOAD_DISABLED
    if not profile.upload_token_configured:
        return UploadReadiness.UPLOAD_TOKEN_MISSING
    return UploadReadiness.READY


class ConnectivityMonitor:
    """One instance per workspace, held for the process's lifetime -
    same "one long-lived instance" shape as `ProviderRegistry`/
    `KeyExchangeCoordinator`."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        *,
        session: Optional[Any] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now_fn=time.time,
    ):
        self._provider_registry = provider_registry
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._now = now_fn
        self._relay_statuses: Dict[str, RelayStatus] = {}
        self._internet_status = InternetStatus.UNKNOWN
        self._consecutive_failures: Dict[str, int] = {}
        self._last_info_check: Dict[str, float] = {}

    # ---- cheap, non-blocking read -----------------------------------

    def snapshot(self) -> ConnectivitySnapshot:
        return ConnectivitySnapshot(internet=self._internet_status, relays=dict(self._relay_statuses))

    def can_attempt_relay(self, provider_id: str) -> bool:
        """Advisory only (module docstring) - a miss (never checked yet)
        returns True rather than False, since refusing every attempt
        before the first probe has even run would make a freshly
        registered provider unusable until the next tick for no reason;
        the real HTTP attempt inside run_step() is always the ultimate
        authority anyway."""
        status = self._relay_statuses.get(provider_id)
        if status is None:
            return True
        return status.state in _ATTEMPTABLE_RELAY_STATES

    # ---- the only method that performs network I/O -------------------

    def refresh(self, *, force: bool = False) -> ConnectivitySnapshot:
        """Probes every enabled provider profile's `/health` (and,
        rarely, `/v1/info`) and recomputes the derived internet status.
        Intended caller: `AttachmentsService`'s own tick loop - never an
        HTTP request handler (module docstring)."""
        now = self._now()
        # All profiles, not just enabled ones: a disabled profile must
        # still surface as a `DISABLED` RelayStatus in the snapshot
        # (design spec's per-Relay state list includes "disabled" as an
        # observable state, e.g. for a Settings page) - `_check_relay()`
        # already short-circuits before any network call for a disabled
        # profile, so including it here costs nothing.
        profiles = self._provider_registry.list_providers()
        any_online = False

        for profile in profiles:
            if not force and not self._due_for_health_check(profile.provider_id, now):
                if profile.provider_id in self._relay_statuses:
                    any_online = any_online or self._relay_statuses[profile.provider_id].state in _ATTEMPTABLE_RELAY_STATES
                continue
            status = self._check_relay(profile, now, force_info=force)
            self._relay_statuses[profile.provider_id] = status
            self._provider_registry.record_check_result(
                profile.provider_id, result=status.state.value, latency_ms=status.latency_ms,
                error_code=status.error_code, now=now,
            )
            if status.state in _ATTEMPTABLE_RELAY_STATES:
                any_online = True

        if any_online:
            self._internet_status = InternetStatus.ONLINE
        elif profiles:
            self._internet_status = InternetStatus.OFFLINE
        else:
            self._internet_status = self._check_fallback_internet(now)

        return self.snapshot()

    def _due_for_health_check(self, provider_id: str, now: float) -> bool:
        status = self._relay_statuses.get(provider_id)
        if status is None or status.checked_at is None:
            return True
        failures = self._consecutive_failures.get(provider_id, 0)
        interval = min(
            RELAY_HEALTH_INTERVAL_SECONDS * (2 ** failures) if failures else RELAY_HEALTH_INTERVAL_SECONDS,
            RELAY_HEALTH_BACKOFF_CEILING_SECONDS,
        )
        return now - status.checked_at >= interval

    def _check_relay(self, profile: ProviderProfile, now: float, *, force_info: bool) -> RelayStatus:
        if not profile.enabled:
            return RelayStatus(
                provider_id=profile.provider_id, state=RelayState.DISABLED,
                upload_readiness=UploadReadiness.UPLOAD_DISABLED, checked_at=now, latency_ms=None, error_code=None,
            )

        start = time.monotonic()
        try:
            response = self._session.request("GET", f"{profile.origin}/health", timeout=self._timeout)
            latency_ms = int((time.monotonic() - start) * 1000)
        except requests.RequestException as exc:
            self._consecutive_failures[profile.provider_id] = self._consecutive_failures.get(profile.provider_id, 0) + 1
            return RelayStatus(
                provider_id=profile.provider_id, state=RelayState.UNREACHABLE,
                upload_readiness=_upload_readiness_for(profile), checked_at=now, latency_ms=None,
                error_code=type(exc).__name__,
            )

        if response.status_code != 200:
            self._consecutive_failures[profile.provider_id] = self._consecutive_failures.get(profile.provider_id, 0) + 1
            return RelayStatus(
                provider_id=profile.provider_id, state=RelayState.DEGRADED,
                upload_readiness=_upload_readiness_for(profile), checked_at=now, latency_ms=latency_ms,
                error_code=f"http_{response.status_code}",
            )

        self._consecutive_failures[profile.provider_id] = 0

        info_due = force_info or (now - self._last_info_check.get(profile.provider_id, 0)) >= RELAY_INFO_MIN_INTERVAL_SECONDS
        if info_due:
            mismatch = self._check_identity(profile)
            self._last_info_check[profile.provider_id] = now
            if mismatch is not None:
                return RelayStatus(
                    provider_id=profile.provider_id, state=RelayState.IDENTITY_MISMATCH,
                    upload_readiness=_upload_readiness_for(profile), checked_at=now, latency_ms=latency_ms,
                    error_code=mismatch,
                )

        return RelayStatus(
            provider_id=profile.provider_id, state=RelayState.ONLINE,
            upload_readiness=_upload_readiness_for(profile), checked_at=now, latency_ms=latency_ms, error_code=None,
        )

    def _check_identity(self, profile: ProviderProfile) -> Optional[str]:
        """Returns an error code string if `/v1/info` disagrees with the
        pinned profile, else None. This is a plausibility/early-warning
        check only - it never substitutes for the real signature
        verification `relay_client.verify_descriptor_signature()` (Step
        1.5/ADR-0007) already performs on every actual object download;
        it exists so a re-keyed or misconfigured Relay shows up as a
        distinct, more severe `identity_mismatch` state well before any
        transfer is attempted against it."""
        try:
            response = self._session.request("GET", f"{profile.origin}/v1/info", timeout=self._timeout)
        except requests.RequestException:
            return None  # health already succeeded; treat a flaky info call as inconclusive, not a mismatch
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
            reported_provider_id = payload["provider_id"]
            reported_public_key = payload["service_key"]["public_key"]
        except (ValueError, KeyError, TypeError):
            return "info_malformed"
        if reported_provider_id != profile.provider_id:
            return "provider_id_mismatch"
        if reported_public_key != b64url_encode(profile.service_public_key):
            return "service_public_key_mismatch"
        return None

    def _check_fallback_internet(self, now: float) -> InternetStatus:
        """Only reached when no provider is registered at all yet (first
        run) - once at least one Relay exists, its own health check is
        always the more meaningful signal (module docstring)."""
        try:
            response = self._session.request("HEAD", FALLBACK_INTERNET_CHECK_URL, timeout=self._timeout)
        except requests.RequestException:
            return InternetStatus.OFFLINE
        return InternetStatus.ONLINE if response.status_code < 500 else InternetStatus.LIMITED
