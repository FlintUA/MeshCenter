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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from meshsrv.attachments.provider_registry import ProviderProfile, ProviderRegistry, b64url_encode

DEFAULT_TIMEOUT_SECONDS = 5.0

# PR #231 review (3rd pass): bounded concurrency for Relay health/info
# probes - a slow/unreachable Relay must not delay probing another,
# already-due Relay behind it in the same refresh() pass. Kept at 2 (not
# unbounded) per the review's own explicit ceiling; every actual state
# mutation (self._relay_statuses/_consecutive_failures/_last_info_check,
# ProviderRegistry.record_check_result()'s SQLite write) still happens
# only on the calling thread - AttachmentsService's own worker thread,
# inside its tick_lock - never inside a probe worker thread. See
# _probe_relay()'s own docstring for the pure-function split that makes
# this safe.
MAX_CONCURRENT_RELAY_PROBES = 2

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


class UploadRejectionReason(str, enum.Enum):
    """PR #231 review (3rd pass), "contextual upload readiness": every
    distinct reason `evaluate_upload_decision()` can refuse an upload,
    named so a caller (and its own tests) can branch/assert on *why*,
    not just whether. `PROFILE_NOT_FOUND` doubles as this dataclass's
    definition of "unknown provider_id" - there is no separate "unknown"
    reason, since a caller can't be told to fix a profile that does not
    exist."""

    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_DISABLED = "profile_disabled"
    UPLOAD_NOT_ALLOWED = "upload_not_allowed"
    UPLOAD_TOKEN_MISSING = "upload_token_missing"
    RELAY_NOT_YET_CHECKED = "relay_not_yet_checked"
    RELAY_UNREACHABLE = "relay_unreachable"
    RELAY_IDENTITY_MISMATCH = "relay_identity_mismatch"
    RELAY_INCOMPATIBLE = "relay_incompatible"
    CIPHERTEXT_TOO_LARGE = "ciphertext_too_large"
    TTL_BELOW_MINIMUM = "ttl_below_minimum"
    TTL_ABOVE_MAXIMUM = "ttl_above_maximum"


@dataclasses.dataclass(frozen=True)
class UploadDecision:
    """The structured result of `evaluate_upload_decision()` - PR #231
    review (3rd pass) explicitly asked for "a structured decision/
    reason, not an unexplained boolean". `ready=True` iff `reason is
    None`; kept as two fields rather than one so a caller can check
    `decision.ready` without an `is None` comparison, while still having
    `reason`/`detail` available for logging/UI display when it is not."""

    ready: bool
    reason: Optional[UploadRejectionReason]
    detail: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class _RelayProbeResult:
    """Everything one call to `_probe_relay()` learned about one profile -
    the pure-function output `refresh()` applies to `self` state itself,
    on its own (calling) thread, after every dispatched probe has
    returned. Kept as a single bundle rather than returning `RelayStatus`
    alone so `refresh()` doesn't have to re-derive `new_consecutive_
    failures`/`info_check_completed` from the status after the fact."""

    status: RelayStatus
    new_consecutive_failures: int
    info_check_completed: bool


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
        session_factory: Optional[Callable[[], Any]] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now_fn=time.time,
    ):
        """PR #231 review (4th pass), "concurrent HTTP client safety":
        `requests.Session` is **not** documented as thread-safe for
        concurrent `.request()` calls from multiple threads at once - an
        earlier pass of this module's own docstring claimed otherwise,
        which was false (`requests`' own docs only ever claim the
        underlying `urllib3` connection pool is safe to *share*; the
        `Session` object itself carries mutable state - `cookies`, hooks,
        `adapters` - that concurrent callers can race on). Sharing one
        `Session` instance across `_probe_due_relays_concurrently()`'s
        concurrent probe workers would reintroduce exactly the kind of
        subtle race this project's own review process exists to catch.

        `session_factory` (new) is called fresh for **every** probe
        (`_probe_relay()`/`_check_fallback_internet()`) - real production
        use gets a brand-new `requests.Session()` per probe (the default,
        `requests.Session` itself as the factory), never shared across
        concurrently-running probes, and each one is closed
        deterministically once that probe returns (`_close_session()`).
        `session` (kept for backward compatibility with existing test
        call sites) wraps a single caller-supplied object in a factory
        that always returns that same instance - explicit, single-shared-
        session semantics a caller opts into knowingly; safe for a test
        double with no real mutable per-request HTTP state, not a
        substitute for `session_factory` against a real `requests.Session`
        under real concurrent I/O. Passing both is a caller error."""
        if session is not None and session_factory is not None:
            raise ValueError("ConnectivityMonitor accepts at most one of session=/session_factory=")
        if session_factory is not None:
            self._session_factory: Callable[[], Any] = session_factory
        elif session is not None:
            self._session_factory = lambda: session
        else:
            self._session_factory = requests.Session
        self._provider_registry = provider_registry
        self._timeout = timeout
        self._now = now_fn
        self._relay_statuses: Dict[str, RelayStatus] = {}
        self._internet_status = InternetStatus.UNKNOWN
        self._consecutive_failures: Dict[str, int] = {}
        self._last_info_check: Dict[str, float] = {}
        self._last_fallback_check_at: Optional[float] = None
        # PR #231 review (4th pass), "preserve the single-owner SQLite
        # model": an atomically-published snapshot of every registered
        # profile, so `evaluate_upload_decision()`/`can_upload_to()` can
        # answer without touching SQLite themselves (see those methods'
        # own docstrings, and `_refresh_profile_snapshot()` below for why
        # this construction-time call is safe). Built as a whole new dict
        # and swapped in with one reference assignment (never mutated in
        # place) - a single `dict` reference assignment is atomic under
        # the GIL, so a concurrent reader always sees either the complete
        # old snapshot or the complete new one, never a partial update.
        self._profile_snapshot: Dict[str, ProviderProfile] = {}
        self._refresh_profile_snapshot()

    def _refresh_profile_snapshot(self) -> None:
        """The one place this class calls `ProviderRegistry.
        list_providers()` (a SQLite read). Safe to call from `__init__`
        (construction always happens on the startup thread - `mca_runtime.
        py`'s own `_MCARuntimeState.__init__`, never a Flask REST thread -
        see ADR-0008's PR #231 amendment) and from `refresh()` (the
        `AttachmentsService` worker thread, inside its `tick_lock`).
        **Never** called from `evaluate_upload_decision()`/
        `can_upload_to()`/anywhere else - those must only ever read the
        already-published `self._profile_snapshot`, to preserve the
        single-owner SQLite model (only the worker thread - or, for this
        one eager call, the startup thread - ever touches `conn`)."""
        profiles = self._provider_registry.list_providers()
        self._profile_snapshot = {profile.provider_id: profile for profile in profiles}

    @staticmethod
    def _close_session(session: Any) -> None:
        """PR #231 review (4th pass): every session `_session_factory()`
        produces is closed deterministically once the probe that acquired
        it is done - a real `requests.Session()` always has `.close()`
        (releases its connection pool); a test double may not, since
        session-closing is optional best-effort cleanup, not part of a
        formal contract this module defines (unlike `DeliveryAdapter`'s
        `connector_profile_id` elsewhere in this review pass, which *is*
        a required contract field) - `hasattr` here is a genuine "does
        this optional capability exist" check, not a mask over a missing
        required one."""
        close = getattr(session, "close", None)
        if callable(close):
            close()

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

    def evaluate_upload_decision(
        self,
        provider_id: str,
        *,
        ciphertext_bytes: Optional[int] = None,
        requested_ttl_seconds: Optional[int] = None,
    ) -> UploadDecision:
        """PR #231 review (3rd pass), "contextual upload readiness" -
        REPLACES the earlier `can_upload_to()` (2nd pass), which only
        combined local config with live reachability into an unexplained
        `bool`. This is the full, structured decision a caller
        (eventually Step 1.6A's REST layer, via `AttachmentsService.
        evaluate_upload_readiness()` below - not built yet, this is the
        function it will call into) needs before actually starting an
        upload: profile existence, `enabled`, `upload_allowed`, a
        credential on file, the Relay's *current* live state (distinct
        reasons for `UNKNOWN`/`UNREACHABLE`/`IDENTITY_MISMATCH`/
        `INCOMPATIBLE`, not folded into one generic "not ready"), and -
        when the caller already knows them - the actual ciphertext size
        against `max_ciphertext_bytes` and a requested TTL against
        `min_ttl_seconds`/`max_ttl_seconds`.

        PR #231 review (4th pass), "preserve the single-owner SQLite
        model": this method must be safe to call from any thread,
        including a hypothetical future Step 1.6A REST request thread -
        it therefore never touches `self._provider_registry`/SQLite
        directly. It reads `self._profile_snapshot`, an atomically-
        published, in-memory-only dict built once eagerly at construction
        time (on the startup thread) and refreshed on every `refresh()`
        call thereafter (the worker thread) - see `_refresh_profile_
        snapshot()`'s own docstring. This is also what still lets step 2
        below answer correctly even before the first real `refresh()` has
        run: the constructor's own eager snapshot build already covers
        it, so "disabled" is known from the moment this object exists,
        not only after the first tick.

        Checked in this fixed order, returning on the first failure
        (fail closed, cheapest/most-fundamental checks first):
        1. the profile exists at all (`PROFILE_NOT_FOUND`);
        2. it is enabled (`PROFILE_DISABLED`) - a disabled profile is
           never upload-ready regardless of what (if anything)
           `ConnectivityMonitor` has otherwise observed about it;
        3. `upload_allowed`/a token is configured (`UPLOAD_NOT_ALLOWED`/
           `UPLOAD_TOKEN_MISSING` - `evaluate_upload_readiness()`'s own
           two checks, reused here rather than re-implemented);
        4. the Relay's current `RelayState` - only `ONLINE`/`DEGRADED`
           (the existing `_ATTEMPTABLE_RELAY_STATES` set, unchanged) are
           upload-ready; every other state gets its own named reason
           (`RELAY_NOT_YET_CHECKED` for `UNKNOWN`/never-probed -
           deliberately NOT fail-open here, unlike `can_attempt_relay()`:
           that method's fail-open behavior exists so a scheduling tick
           isn't stuck refusing a freshly-registered profile for no
           reason, but a human-facing "can I upload here" decision should
           not claim readiness this module has not actually confirmed
           yet; `RELAY_UNREACHABLE`/`RELAY_IDENTITY_MISMATCH`/
           `RELAY_INCOMPATIBLE` map directly from the matching
           `RelayState`; `PROFILE_DISABLED` is unreachable here since
           step 2 already returned for a disabled profile - `DEGRADED`
           counts as upload-ready, matching `_ATTEMPTABLE_RELAY_STATES`'s
           existing "worth attempting" meaning elsewhere in this module,
           not a new, stricter standard invented just for this check);
        5. `ciphertext_bytes` (when given) against `profile.
           max_ciphertext_bytes` (`CIPHERTEXT_TOO_LARGE`);
        6. `requested_ttl_seconds` (when given) against `profile.
           min_ttl_seconds`/`max_ttl_seconds`, whichever bound is
           actually configured (`TTL_BELOW_MINIMUM`/`TTL_ABOVE_MAXIMUM`).

        `ciphertext_bytes`/`requested_ttl_seconds` are optional:
        omitting either skips that one check (a caller asking "is this
        Relay even usable in principle" before a file has been
        encrypted, and therefore before its real ciphertext size is
        known, has nothing to check step 5 against yet)."""
        profile = self._profile_snapshot.get(provider_id)
        if profile is None:
            return UploadDecision(ready=False, reason=UploadRejectionReason.PROFILE_NOT_FOUND)
        if not profile.enabled:
            return UploadDecision(ready=False, reason=UploadRejectionReason.PROFILE_DISABLED)
        if not profile.upload_allowed:
            return UploadDecision(ready=False, reason=UploadRejectionReason.UPLOAD_NOT_ALLOWED)
        if not profile.upload_token_configured:
            return UploadDecision(ready=False, reason=UploadRejectionReason.UPLOAD_TOKEN_MISSING)

        relay_status = self._relay_statuses.get(provider_id)
        relay_state = relay_status.state if relay_status is not None else RelayState.UNKNOWN
        if relay_state == RelayState.UNKNOWN:
            return UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_NOT_YET_CHECKED)
        if relay_state == RelayState.UNREACHABLE:
            return UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_UNREACHABLE)
        if relay_state == RelayState.IDENTITY_MISMATCH:
            return UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_IDENTITY_MISMATCH)
        if relay_state == RelayState.INCOMPATIBLE:
            return UploadDecision(ready=False, reason=UploadRejectionReason.RELAY_INCOMPATIBLE)
        if relay_state not in _ATTEMPTABLE_RELAY_STATES:
            # DISABLED is unreachable here (step 2 above already returned
            # for that case) - this is a defensive catch-all for any
            # future RelayState this function has not been explicitly
            # taught about yet, so a new state defaults to "not ready"
            # rather than silently falling through to READY.
            return UploadDecision(
                ready=False, reason=UploadRejectionReason.RELAY_UNREACHABLE,
                detail=f"unrecognized_relay_state:{relay_state.value}",
            )

        if ciphertext_bytes is not None and ciphertext_bytes > profile.max_ciphertext_bytes:
            return UploadDecision(
                ready=False, reason=UploadRejectionReason.CIPHERTEXT_TOO_LARGE,
                detail=f"{ciphertext_bytes} > {profile.max_ciphertext_bytes}",
            )
        if requested_ttl_seconds is not None:
            if profile.min_ttl_seconds is not None and requested_ttl_seconds < profile.min_ttl_seconds:
                return UploadDecision(
                    ready=False, reason=UploadRejectionReason.TTL_BELOW_MINIMUM,
                    detail=f"{requested_ttl_seconds} < {profile.min_ttl_seconds}",
                )
            if profile.max_ttl_seconds is not None and requested_ttl_seconds > profile.max_ttl_seconds:
                return UploadDecision(
                    ready=False, reason=UploadRejectionReason.TTL_ABOVE_MAXIMUM,
                    detail=f"{requested_ttl_seconds} > {profile.max_ttl_seconds}",
                )

        return UploadDecision(ready=True, reason=None)

    def can_upload_to(self, provider_id: str) -> bool:
        """Thin boolean convenience wrapper around
        `evaluate_upload_decision()` for callers that only need a
        yes/no answer (e.g. a quick internal gate) and don't need to
        report *why* - prefer calling `evaluate_upload_decision()`
        directly wherever the reason matters, which is most real
        callers (module-level "contextual upload readiness" note)."""
        return self.evaluate_upload_decision(provider_id).ready

    # ---- the only method that performs network I/O -------------------

    def refresh(self, *, force: bool = False) -> ConnectivitySnapshot:
        """Probes every enabled provider profile's `/health` (and,
        rarely, `/v1/info`) and recomputes the derived internet status.
        Intended caller: `AttachmentsService`'s own tick loop - never an
        HTTP request handler (module docstring).

        PR #231 review (3rd pass): due profiles are now probed with
        bounded concurrency (`MAX_CONCURRENT_RELAY_PROBES=2`, via
        `_probe_due_relays_concurrently()`) - a slow/unreachable Relay no
        longer delays probing another, already-due Relay behind it in
        the same pass. Every state mutation this method makes
        (`self._relay_statuses`/`_consecutive_failures`/
        `_last_info_check`, `ProviderRegistry.record_check_result()`'s
        SQLite write) still happens only here, on the calling thread
        (`AttachmentsService`'s own worker thread, inside its
        `tick_lock`) - never inside a probe worker thread. See
        `_probe_relay()`'s own docstring for the pure-function split
        that makes concurrent dispatch safe without any additional
        locking inside this class."""
        now = self._now()
        # All profiles, not just enabled ones: a disabled profile must
        # still surface as a `DISABLED` RelayStatus in the snapshot
        # (design spec's per-Relay state list includes "disabled" as an
        # observable state, e.g. for a Settings page) - `_probe_relay()`
        # already short-circuits before any network call for a disabled
        # profile, so including it here costs nothing.
        #
        # PR #231 review (4th pass): also republishes self._profile_snapshot
        # (the same SQLite read this line already needed) - see
        # `_refresh_profile_snapshot()`'s own docstring for why this is
        # the only other place (besides __init__) that reads the registry.
        self._refresh_profile_snapshot()
        profiles = list(self._profile_snapshot.values())

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

        # PR #231 review (2nd pass): only `any_online` is tracked now -
        # `any_failing` (a DISABLED-excluding "did anything actually go
        # wrong" flag) was removed along with the `elif` condition that
        # used to read it (see below): the only thing that should ever
        # suppress the fallback internet probe is already-confirmed
        # evidence that the internet is fine, which `any_online` alone
        # already expresses. A DISABLED relay was never evidence of a
        # failure anyway (a deliberate user choice) - the zero-network-
        # calls guarantee a disabled profile gets is unaffected either
        # way, disabled profiles still short-circuit below before any
        # HTTP call.
        any_online = False
        due_profiles: List[ProviderProfile] = []

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
                # call either way - matches `_probe_relay()`'s own
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
                continue
            due_profiles.append(profile)

        for profile, probe_result in self._probe_due_relays_concurrently(due_profiles, now, force_info=force):
            self._relay_statuses[profile.provider_id] = probe_result.status
            self._consecutive_failures[profile.provider_id] = probe_result.new_consecutive_failures
            if probe_result.info_check_completed:
                self._last_info_check[profile.provider_id] = now
            self._provider_registry.record_check_result(
                profile.provider_id, result=probe_result.status.state.value, latency_ms=probe_result.status.latency_ms,
                error_code=probe_result.status.error_code, now=now,
            )
            if probe_result.status.state in _ATTEMPTABLE_RELAY_STATES:
                any_online = True

        if any_online:
            self._internet_status = InternetStatus.ONLINE
        elif force or self._due_for_fallback_check(now):
            # Reviewer-found defect, confirmed against the code: the old
            # `elif profiles: OFFLINE` branch here meant "every registered
            # Relay is down" was reported as the *internet itself* being
            # down - indistinguishable from a genuinely dead connection,
            # even with exactly one Relay registered and general internet
            # completely fine. The independent fallback probe (originally
            # written for the zero-providers first-run case only) is the
            # one signal this module has that doesn't depend on any
            # particular Relay's health, so it's now the authority
            # whenever no relay evidence says otherwise.
            #
            # PR #231 review (2nd pass): the condition used to also
            # require `(not profiles or any_failing)` - meaning a
            # workspace where every registered Relay is *disabled* (not
            # failing - `any_failing` deliberately excludes DISABLED)
            # never reached this branch at all, so `_internet_status`
            # stayed at whatever it last was (typically UNKNOWN forever,
            # on an instance whose only registered Relays have always
            # been disabled) instead of reflecting general internet
            # reachability independently of Relay-specific state. The
            # only thing that should ever suppress this probe is already-
            # confirmed evidence that internet is fine (any_online, above)
            # - there is no other case worth special-casing out of it.
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

    def _probe_due_relays_concurrently(
        self, due_profiles: List[ProviderProfile], now: float, *, force_info: bool
    ) -> List[Tuple[ProviderProfile, "_RelayProbeResult"]]:
        """PR #231 review (3rd pass): dispatches up to
        `MAX_CONCURRENT_RELAY_PROBES` (2) `_probe_relay()` calls at once -
        a slow/unreachable Relay's `/health` (or `/v1/info`) call must
        not delay probing another, already-due Relay behind it in the
        same `refresh()` pass. `ThreadPoolExecutor.map()` preserves
        input order in its output, so the returned list lines up 1:1
        with `due_profiles` without needing to track futures by hand.

        Every argument `_probe_relay()` needs (`current_failures`,
        `last_info_check_at`) is read from `self` here, on the calling
        thread, *before* dispatch - the worker threads themselves never
        read or write any `self.` dict, only `self._timeout` (read-only)
        and `self._session_factory()` (PR #231 review, 4th pass: called
        fresh inside `_probe_relay()` for *each* probe - `requests.
        Session` is not documented as safe for concurrent use from
        multiple threads, so no session object is ever shared between
        concurrently-running probes; see `__init__`'s own docstring).
        This is what makes concurrent dispatch safe without introducing a
        new lock inside this class: every actual mutation happens back on
        the calling thread, in `refresh()`, after this method returns."""
        if not due_profiles:
            return []

        def _run_one(profile: ProviderProfile) -> "_RelayProbeResult":
            return self._probe_relay(
                profile, now, force_info=force_info,
                current_failures=self._consecutive_failures.get(profile.provider_id, 0),
                last_info_check_at=self._last_info_check.get(profile.provider_id),
            )

        max_workers = min(MAX_CONCURRENT_RELAY_PROBES, len(due_profiles))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_run_one, due_profiles))
        return list(zip(due_profiles, results))

    def _probe_relay(
        self, profile: ProviderProfile, now: float, *, force_info: bool,
        current_failures: int, last_info_check_at: Optional[float],
    ) -> "_RelayProbeResult":
        """The actual network I/O for one profile - deliberately a pure
        function with respect to `self`'s *mutable* state (PR #231
        review, 3rd pass, tightened in the 4th): it reads only
        `self._timeout` (read-only) and the caller-supplied
        `current_failures`/`last_info_check_at` values, and returns a
        `_RelayProbeResult` for the caller to apply - it never writes
        `self._relay_statuses`/`_consecutive_failures`/`_last_info_check`,
        and never calls `self._provider_registry.record_check_result()`
        (a SQLite write). Its own HTTP session is acquired fresh from
        `self._session_factory()` and closed deterministically before
        returning (4th pass - see `__init__`'s own docstring for why a
        shared `requests.Session` is not safe here). This is what makes
        it safe to call from a worker thread via `_probe_due_relays_
        concurrently()`: nothing here touches anything another
        concurrently-running call to this same method could also be
        touching, including the HTTP session itself."""
        if not profile.enabled:
            return _RelayProbeResult(
                status=RelayStatus(
                    provider_id=profile.provider_id, state=RelayState.DISABLED,
                    upload_readiness=UploadReadiness.UPLOAD_DISABLED, checked_at=now, latency_ms=None, error_code=None,
                ),
                new_consecutive_failures=0, info_check_completed=False,
            )

        session = self._session_factory()
        try:
            return self._probe_relay_with_session(
                profile, now, session, force_info=force_info,
                current_failures=current_failures, last_info_check_at=last_info_check_at,
            )
        finally:
            self._close_session(session)

    def _probe_relay_with_session(
        self, profile: ProviderProfile, now: float, session: Any, *, force_info: bool,
        current_failures: int, last_info_check_at: Optional[float],
    ) -> "_RelayProbeResult":
        """The actual `/health` (and, rarely, `/v1/info`) requests, using
        the one `session` `_probe_relay()` acquired for this call and
        will close once this returns - split out only so `_probe_relay()`
        itself stays a short acquire/use/close wrapper."""
        start = time.monotonic()
        try:
            response = session.request("GET", f"{profile.origin}/health", timeout=self._timeout)
            latency_ms = int((time.monotonic() - start) * 1000)
        except requests.RequestException as exc:
            return _RelayProbeResult(
                status=RelayStatus(
                    provider_id=profile.provider_id, state=RelayState.UNREACHABLE,
                    upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=None,
                    error_code=type(exc).__name__,
                ),
                new_consecutive_failures=current_failures + 1, info_check_completed=False,
            )

        if response.status_code != 200:
            return _RelayProbeResult(
                status=RelayStatus(
                    provider_id=profile.provider_id, state=RelayState.DEGRADED,
                    upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms,
                    error_code=f"http_{response.status_code}",
                ),
                new_consecutive_failures=current_failures + 1, info_check_completed=False,
            )

        info_due = force_info or (now - (last_info_check_at or 0)) >= RELAY_INFO_MIN_INTERVAL_SECONDS
        if info_due:
            result = self._check_identity(profile, session)
            if result is _INFO_CHECK_INCONCLUSIVE:
                # PR #231 review, section 6: a transient /v1/info failure
                # must not be treated as a completed check - the caller
                # must not advance _last_info_check, or the *next* real
                # attempt could be silently deferred by up to
                # RELAY_INFO_MIN_INTERVAL_SECONDS (an hour) because of one
                # flaky request.
                return _RelayProbeResult(
                    status=RelayStatus(
                        provider_id=profile.provider_id, state=RelayState.ONLINE,
                        upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms,
                        error_code=None,
                    ),
                    new_consecutive_failures=0, info_check_completed=False,
                )
            if result is not None:
                mismatch_state, error_code = result
                return _RelayProbeResult(
                    status=RelayStatus(
                        provider_id=profile.provider_id, state=mismatch_state,
                        upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms,
                        error_code=error_code,
                    ),
                    new_consecutive_failures=0, info_check_completed=True,
                )
            return _RelayProbeResult(
                status=RelayStatus(
                    provider_id=profile.provider_id, state=RelayState.ONLINE,
                    upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms,
                    error_code=None,
                ),
                new_consecutive_failures=0, info_check_completed=True,
            )

        return _RelayProbeResult(
            status=RelayStatus(
                provider_id=profile.provider_id, state=RelayState.ONLINE,
                upload_readiness=evaluate_upload_readiness(profile), checked_at=now, latency_ms=latency_ms, error_code=None,
            ),
            new_consecutive_failures=0, info_check_completed=False,
        )

    def _check_identity(self, profile: ProviderProfile, session: Any):
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
        attempted against it.

        `session` (PR #231 review, 4th pass) is the *same* session
        `_probe_relay()` already acquired for this one profile's `/health`
        call - reused here for `/v1/info` (both are part of one probe),
        never a second, independently-acquired session, and never shared
        with a concurrently-running probe for a different profile."""
        try:
            response = session.request("GET", f"{profile.origin}/v1/info", timeout=self._timeout)
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
        state keyed by it. Always called from `refresh()` itself (never
        concurrently with the per-Relay probes, which have already
        completed by the time this runs), but still acquires its own
        session via `self._session_factory()` and closes it
        deterministically - the same discipline every other network call
        in this class follows now (PR #231 review, 4th pass), rather than
        this one call site being the sole exception."""
        session = self._session_factory()
        try:
            response = session.request("HEAD", FALLBACK_INTERNET_CHECK_URL, timeout=self._timeout)
        except requests.RequestException:
            return InternetStatus.OFFLINE
        finally:
            self._close_session(session)
        return InternetStatus.ONLINE if response.status_code < 500 else InternetStatus.LIMITED
