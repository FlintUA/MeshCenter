"""meshsrv/attachments/key_request_retry.py

The retry policy for a KEY_REQUEST the user started. A KEY_REQUEST (or the KEY_ANNOUNCE that answers it) can be lost on
the mesh; without a retry the contact card waits and then offers a manual "Request again". This module is the pure
policy (stdlib only, no clock, no I/O): the persisted series state, the schedule and the decisions. The service
(service.py) owns the clock, the radio and the database; the coordinator (key_exchange.py) persists the two numbers.

A series is two persisted numbers per contact address:

  attempts         how many KEY_REQUESTs of this series were sent (0 = no series)
  next_retry_at    when the service looks at the series again (NULL = no series running)

The schedule is 10 min, 30 min, 2 h, 6 h after each attempt: attempt 1 is the user's request; attempts 2 and 3 are
retries 10 and 30 minutes after the previous one; attempt 4 is the last retry (2 hours after attempt 3) and then the
series waits 6 hours for the answer before it gives up. That is 4 requests in about 8 h 40 min.

  attempts == 0                               no series (idle, or ended by Cancel / an answer)
  0 < attempts and next_retry_at is not None  a series is running (the UI says "attempt N of 4, next in ...")
  attempts == MAX_ATTEMPTS, next_retry_at None  the series ended without an answer: state "no_answer"

No protocol change: every attempt is the ordinary signed KEY_REQUEST, subject to the same per-address minimum interval
(10 min) and the same workspace-wide hourly quota as an automatic request; the schedule never gets closer than 10 min.
"""
from __future__ import annotations

from typing import Optional, Tuple

#: seconds after an attempt: [0] before attempt 2, [1] before attempt 3, [2] before attempt 4, [3] the wait for the
#: answer to attempt 4 before the series gives up
RETRY_DELAYS_S: Tuple[int, ...] = (10 * 60, 30 * 60, 2 * 3600, 6 * 3600)
MAX_ATTEMPTS: int = len(RETRY_DELAYS_S)

#: a retry that could not be sent (radio released or busy, no delivery adapter) is looked at again after this long;
#: it does not count as an attempt and consumes no quota
POSTPONE_UNSENT_S: int = 60

ACTION_NONE = "none"
ACTION_RETRY = "retry"
ACTION_GIVE_UP = "give_up"


def delay_after(attempts: int) -> int:
    """Seconds to wait after attempt number `attempts` (1-based) before the next look at the series."""
    index = max(1, min(int(attempts), MAX_ATTEMPTS)) - 1
    return RETRY_DELAYS_S[index]


def first_attempt(now: float) -> Tuple[int, float]:
    """(attempts, next_retry_at) right after the user's own request was sent."""
    return 1, now + delay_after(1)


def after_retry_sent(attempts: int, now: float) -> Tuple[int, float]:
    """(attempts, next_retry_at) after a retry was sent."""
    count = min(int(attempts) + 1, MAX_ATTEMPTS)
    return count, now + delay_after(count)


def is_running(attempts: int, next_retry_at: Optional[float]) -> bool:
    return int(attempts or 0) > 0 and next_retry_at is not None


def is_no_answer(attempts: int, next_retry_at: Optional[float]) -> bool:
    return int(attempts or 0) >= MAX_ATTEMPTS and next_retry_at is None


def due_action(attempts: int, next_retry_at: Optional[float], now: float) -> str:
    """What the service does with a series right now: nothing, send the next retry, or give up (the wait for the
    answer to the last attempt is over)."""
    if not is_running(attempts, next_retry_at) or now < float(next_retry_at):
        return ACTION_NONE
    return ACTION_GIVE_UP if int(attempts) >= MAX_ATTEMPTS else ACTION_RETRY


def seconds_until_next(next_retry_at: Optional[float], now: float) -> Optional[int]:
    """Whole seconds until the series is looked at again, rounded UP to a multiple of 30 (the UI shows minutes; a raw
    timestamp is never published), or None when no series is running."""
    if next_retry_at is None:
        return None
    left = max(0.0, float(next_retry_at) - now)
    return int(-(-left // 30) * 30)
