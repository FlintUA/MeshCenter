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

# Used whenever no registered Relay is currently attemptable - the
# first-run case (no profile exists yet) and the case every registered
# Relay is down while general internet may be fine (a real, reviewer-
# found defect this module used to conflate - see _check_fallback_
# internet()'s own docstring). A plain, documented, operator-
# configurable placeholder. Revisit if this needs to be a Settings-
# configurable value instead of a code constant.
FALLBACK_INTERNET_CHECK_URL = "https://www.gstatic.com/generate_204"

# PR #231 review, section 6: the MCA wire protocol version(s) this
# client's own codec can speak - meshsrv.attachments.codec.PROTOCOL_VERSION
# is the only one for this MVP (always 1, ADR-0001 section 2), kept as a
# separate, string-keyed set here rather than importing codec.py directly
# (this module has no other dependency on meshsrv.attachments.* beyond
# provider_registry) so a future multi-version client only has to widen
# this one set, not thread an import through connectivity_monitor.py's
# own dependency graph.
SUPPORTED_RELAY_PROTOCOL_VERSIONS = frozenset({"1"})

# PR #231 review, section 6: a distinct sentinel (not None or a
# (state, error_code) tuple) so _check_identity()'s three real outcomes -
# "verified fine", "verified and it's a real mismatch", "the /v1/info
# call itself failed transiently, nothing was actually verified" - can
# never be confused with each other. Only the first two are genuine
# checks worth advancing _last_info_check for; a transient failure must
# not be treated as if identity had been confirmed (the bug this fixes:
# _last_info_check used to be stamped unconditionally, so a single flaky
# request could silently defer the *next real* identity check by up to
# RELAY_INFO_MIN_INTERVAL_SECONDS, ~an hour).
_INFO_CHECK_INCONCLUSIVE = object()


class InternetStatus(str, enum.Enum):
    """PR #231 review, section 6: CHECKING was removed - it was never
    actually set anywhere (refresh() runs a probe to completion within
    one tick(), synchronously; there is no caller today that reads
    snapshot() concurrently, mid-probe, from a different thread - the
    one thing CHECKING could ever have meant). Making it real would mean
    adding thread-safety machinery for a consumer that does not exist
    yet (Step 1.6A's REST layer, explicitly out of scope for this
    review); removing a dead, never-set enum member is the honest
    choice over shipping a status value snapshot() can never actually
    return."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    LIMITED = "limited"


class RelayState(str, enum.Enum):
    """See InternetStatus's own docstring - CHECKING removed for the same
    reason, same audit (grepped, confirmed unused anywhere in the
    codebase before removing)."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    IDENTITY_MISMATCH = "identity_mismatch"
    INCOMPATIBLE = "incompatible"
    DISABLED = "disabled"


# States an attempt is worth making for (ADR-0008 decision 2).
_ATTEMPTABLE_RELAY_STATES = frozenset({RelayState.ONLINE, RelayState.DEGRADED})


class UploadReadiness(str, enum.Enum):
    """PR #231 review, section 10: LIMIT_EXCEEDED was removed - nothing
    in this codebase computes a per-Relay upload quota/limit today (no
    such tracking exists anywhere), so this state could never actually
    be returned; a state a caller could branch on but that
    evaluate_upload_readiness() can never produce is worse than not
    having it, since it invites dead code at every call site that
    handles it "for completeness". Revisit if/when a real per-Relay
    upload quota is implemented - add it back then, with the logic that
    actually computes it, not before."""

    READY = "ready"
    UPLOAD_TOKEN_MISSING = "upload_token_missing"
    UPLOAD_DISABLED = "upload_disabled"


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


def evaluate_upload_readiness(profile: ProviderProfile) -> UploadReadiness:
    """The only sanctioned way to compute a profile's `UploadReadiness` -
    a real function (PR #231 review, section 10: the old `_upload_
    readiness_for()` name/leading-underscore made this look like a
    private implementation detail of this module rather than the one
    place that decision is actually made; a caller reasoning about
    whether a Relay is upload-ready should call this, not re-derive the
    same two-field check itself).

    Deliberately independent of `RelayState`/live connectivity (module
    docstring, and see the `RelayStatus` fields it feeds into: `state`
    and `upload_readiness` are reported side by side, never merged) -
    this only reflects local configuration (is uploading allowed for
    this profile at all, is a credential actually on file), not whether
    the Relay is currently reachable. A Relay can be perfectly upload-
    READY while UNREACHABLE right now, or vice versa."""
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
        self._last_fallback_check_at: Optional[float] = None

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
        HTTP request handler (module docstring).

        PR #231 review, section 6 asked for concurrent probes bounded at
        <=2: this loop already probes strictly one profile at a time
        (concurrency of 1, well within that bound) - AttachmentsService
        calls this from its own single worker thread, holding its tick
        lock, the same "real concurrency ceiling is 1 by construction"
        shape the rest of that module's docstring already describes.
        Nothing here spawns a thread/task per profile, so there is no
        actual concurrency to bound."""
        now = self._now()
        # All profiles, not just enabled ones: a disabled profile must
        # still surface as a `DISABLED` RelayStatus in the snapshot
        # (design spec's per-Relay state list includes "disabled" as an
        # observable state, e.g. for a Settings page) - `_check_relay()`
        # already short-circuits before any network call for a disabled
        # profile, so including it here costs nothing.
        profiles = self._provider_registry.list_providers()

        # PR #231 review, section 6: prune every per-provider dict of an
        # entry for a provider_id that no longer exists in this
        # workspace's registry at all (deleted via remove_or_disable()) -
        # otherwise this monitor's own state grows without bound over a
        # long-running instance's lifetime as Relays are added and
        # removed, and a stale entry could (harmlessly, but confusingly)
        # keep answering can_attempt_relay() for a provider_id nothing
        # references any more.
        current_ids = {profile.provider_id for profile in profiles}
        for stale_dict in (self._relay_statuses, self._consecutive_failures, self._last_info_check):
            for stale_id in [pid for pid in stale_dict if pid not in current_ids]:
                del stale_dict[stale_id]

        any_online = False
        # A DISABLED relay is a deliberate user choice, not a failure -
        # it must never by itself trigger the fallback probe below (a
        # workspace with only disabled Relays configured gets the
        # zero-network-calls guarantee `_check_relay()` already gives a
        # disabled profile, same as before this fix). any_failing tracks
        # only genuine failure states (unreachable/degraded/incompatible/
        # identity_mismatch), which is what actually needs disambiguating
        # from a real internet outage.
        any_failing = False

        for profile in profiles:
            if not profile.enabled:
                # PR #231 review, section 6: checked *before* the due-for-
                # health-check gate below, and unconditionally - a
                # disabled Relay must be reflected immediately, never
                # served from a stale cached ONLINE/DEGRADED reading left
                # over from before the user disabled it (the old code let
                # `_due_for_health_check()`'s own backoff/interval logic
                # decide when a just-disabled profile's status next got
                # recomputed, which could be minutes away). No network
                # call either way - matches _check_relay()'s own
                # short-circuit for a disabled profile.
                status = RelayStatus(
                    provider_id=profile.provider_id, state=RelayState.DISABLED,
                    upload_readiness=UploadReadiness.UPLOAD_DISABLED, checked_at=now, latency_ms=None, error_code=None,
                )
                self._relay_statuses[profile.provider_id] = status
                self._consecutive_failures.pop(profile.provider_id, None)
                continue
            if not force and not self._due_for_health_check(profile.provider_id, now):
                if profile.provider_id in self._relay_statuses:
                    cached_state = self._relay_statuses[profile.provider_id].state
                    any_online = any_online or cached_state in _ATTEMPTABLE_RELAY_STATES
                    any_failing = any_failing or cached_state not in _ATTEMPTABLE_RELAY_STATES | {RelayState.DISABLED}
                continue
            status = self._check_relay(profile, now, force_info=force)
            self._relay_statuses[profile.provider_id] = status
            self._provider_registry.record_check_result(
                profile.provider_id, result=status.state.value, latency_ms=status.latency_ms,
                error_code=status.error_code, now=now,
            )
            if status.state in _ATTEMPTABLE_RELAY_STATES:
                any_online = True
            elif status.state != RelayState.DISABLED:
                any_failing = True

        if any_online:
            self._internet_status = InternetStatus.ONLINE
        elif (not profiles or any_failing) and (force or self._due_for_fallback_check(now)):
            # Reviewer-found defect, confirmed against the code: the old
            # `elif profiles: OFFLINE` branch here meant "every registered
            # Relay is down" was reported as the *internet itself* being
            # down - indistinguishable from a genuinely dead connection,
            # even with exactly one Relay registered and general internet
            # completely fine. The independent fallback probe (originally
            # written for the zero-providers first-run case only) is the
            # one signal this module has that doesn't depend on any
            # particular Relay's health, so it's now the authority
            # whenever no relay evidence says otherwise - not only when
            # none are registered yet.
            self._internet_status = self._check_fallback_internet(now)
            self._last_fallback_check_at = now
        # else: no relay is attemptable, but the fallback probe isn't due
        # yet - keep the last computed internet status rather than
        # guessing or hammering the fallback URL every tick.

        return self.snapshot()

    def _due_for_fallback_check(self, now: float) -> bool:
        """Same interval discipline as `_due_for_health_check()`, without
        per-target backoff - there's exactly one fallback target, and a
        transient failure here just means the next tick tries again at
        the normal cadence, same as any other health probe's baseline
        interval."""
        if self._last_fallback_check_at is None:
            return True
        return now - self._last_fallback_check_at >= RELAY_HEALTH_INTERVAL_SECONDS

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
                upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=None,
                error_code=type(exc).__name__,
            )

        if response.status_code != 200:
            self._consecutive_failures[profile.provider_id] = self._consecutive_failures.get(profile.provider_id, 0) + 1
            return RelayStatus(
                provider_id=profile.provider_id, state=RelayState.DEGRADED,
                upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms,
                error_code=f"http_{response.status_code}",
            )

        self._consecutive_failures[profile.provider_id] = 0

        info_due = force_info or (now - self._last_info_check.get(profile.provider_id, 0)) >= RELAY_INFO_MIN_INTERVAL_SECONDS
        if info_due:
            result = self._check_identity(profile)
            if result is not _INFO_CHECK_INCONCLUSIVE:
                # PR #231 review, section 6: only stamped when the check
                # actually completed (succeeded or found a real mismatch)
                # - a transient failure (result is _INFO_CHECK_INCONCLUSIVE)
                # must not advance this, or the *next* real attempt could
                # be silently deferred by up to RELAY_INFO_MIN_INTERVAL_
                # SECONDS (an hour) because of one flaky request.
                self._last_info_check[profile.provider_id] = now
            if result is not None and result is not _INFO_CHECK_INCONCLUSIVE:
                mismatch_state, error_code = result
                return RelayStatus(
                    provider_id=profile.provider_id, state=mismatch_state,
                    upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms,
                    error_code=error_code,
                )

        return RelayStatus(
            provider_id=profile.provider_id, state=RelayState.ONLINE,
            upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms, error_code=None,
        )

    def _check_identity(self, profile: ProviderProfile):
        """Returns `None` if `/v1/info` agrees with the pinned profile
        and speaks a protocol version this client supports; a
        `(RelayState, error_code)` pair for a genuine, confirmed problem
        (identity mismatch or an unsupported protocol version); or the
        `_INFO_CHECK_INCONCLUSIVE` sentinel if the check itself could not
        be completed (network error, non-200, malformed... no, malformed
        JSON *is* a confirmed problem - see below) - callers must not
        treat the sentinel as either a pass or a real finding (module-
        level docstring: `_INFO_CHECK_INCONCLUSIVE`).

        This is a plausibility/early-warning check only - it never
        substitutes for the real signature verification
        `relay_client.verify_descriptor_signature()` (Step 1.5/ADR-0007)
        already performs on every actual object download; it exists so a
        re-keyed, misconfigured, or protocol-incompatible Relay shows up
        as a distinct, more severe state well before any transfer is
        attempted against it."""
        try:
            response = self._session.request("GET", f"{profile.origin}/v1/info", timeout=self._timeout)
        except requests.RequestException:
            # health already succeeded; treat a flaky info call as
            # inconclusive, not a mismatch - and, critically, not as a
            # completed check either (see _check_relay()'s own comment on
            # why this must not advance _last_info_check).
            return _INFO_CHECK_INCONCLUSIVE
        if response.status_code != 200:
            return _INFO_CHECK_INCONCLUSIVE
        try:
            payload = response.json()
            reported_provider_id = payload["provider_id"]
            reported_public_key = payload["service_key"]["public_key"]
        except (ValueError, KeyError, TypeError):
            # Unlike a network error/non-200, a 200 response that fails to
            # parse into the expected shape is a real, confirmed problem
            # with this Relay - not a transient blip - so this counts as
            # a completed check (advances _last_info_check) with a
            # genuine finding, not _INFO_CHECK_INCONCLUSIVE.
            return (RelayState.IDENTITY_MISMATCH, "info_malformed")
        if reported_provider_id != profile.provider_id:
            return (RelayState.IDENTITY_MISMATCH, "provider_id_mismatch")
        if reported_public_key != b64url_encode(profile.service_public_key):
            return (RelayState.IDENTITY_MISMATCH, "service_public_key_mismatch")
        reported_protocol_version = payload.get("protocol_version")
        if reported_protocol_version is not None and str(reported_protocol_version) not in SUPPORTED_RELAY_PROTOCOL_VERSIONS:
            # PR #231 review, section 6: a Relay's own reported protocol
            # version must actually be checked against what this client
            # can speak - a mismatch here is a distinct, more actionable
            # problem (INCOMPATIBLE) than a spoofed/re-keyed Relay
            # (IDENTITY_MISMATCH): the Relay is who it claims to be, this
            # client just cannot talk to it.
            return (RelayState.INCOMPATIBLE, f"unsupported_protocol_version:{reported_protocol_version}")
        return None

    def _check_fallback_internet(self, now: float) -> InternetStatus:
        """Reached whenever no registered Relay is currently attemptable
        (online/degraded) - including the first-run case (no Relay
        registered at all) and the case a single (or every) registered
        Relay is down while general internet may be completely fine.
        `now` is accepted for the same signature shape as every other
        `_check_*` method here, even though this probe has no per-target
        state keyed by it."""
        try:
            response = self._session.request("HEAD", FALLBACK_INTERNET_CHECK_URL, timeout=self._timeout)
        except requests.RequestException:
            return InternetStatus.OFFLINE
        return InternetStatus.ONLINE if response.status_code < 500 else InternetStatus.LIMITED
