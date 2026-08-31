"""Tests for meshsrv/installation_identity.py: Stage A's generator/validator,
plus Stage C's confirmed-UTC-time helpers. Randomness is mocked throughout
per the spec's own requirement (10.1) not to rely on real entropy calls for
test determinism; the time helpers mock meshsrv.time_service.get_status()
directly (that module has no config.py dependency - confirmed during the
Stage C investigation - so it's imported for real here, only its one
NTP-probing entry point is patched) rather than sleeping or touching a
real clock/timedatectl.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from meshsrv.installation_identity import (
    format_utc_iso8601,
    generate_installation_id,
    get_confirmed_utc_time,
    is_valid_installation_id,
)

_FAKE_HEX = "0123456789ABCDEF0123"  # 20 chars = secrets.token_hex(10).upper()


def test_generated_id_matches_format():
    with patch("secrets.token_hex", return_value=_FAKE_HEX.lower()):
        value = generate_installation_id()
    assert value == "MC1-0123-4567-89AB-CDEF-0123"


def test_generated_id_uses_only_hex_characters():
    with patch("secrets.token_hex", return_value=_FAKE_HEX.lower()):
        value = generate_installation_id()
    body = value[len("MC1-"):].replace("-", "")
    assert all(c in "0123456789ABCDEF" for c in body)


def test_generated_id_has_mc1_prefix():
    with patch("secrets.token_hex", return_value=_FAKE_HEX.lower()):
        value = generate_installation_id()
    assert value.startswith("MC1-")


def test_generator_requests_exactly_ten_bytes_of_entropy():
    # Per spec 10.1: verify the call argument itself, not just the output
    # shape - a generator that produced a correctly-shaped ID from a
    # different byte count would still pass a purely output-based check.
    with patch("secrets.token_hex", return_value=_FAKE_HEX.lower()) as mock_token_hex:
        generate_installation_id()
    mock_token_hex.assert_called_once_with(10)


def test_validator_accepts_a_correct_value():
    assert is_valid_installation_id("MC1-0123-4567-89AB-CDEF-0123") is True


def test_validator_rejects_lowercase():
    assert is_valid_installation_id("mc1-0123-4567-89ab-cdef-0123") is False


def test_validator_rejects_wrong_prefix():
    assert is_valid_installation_id("MC2-0123-4567-89AB-CDEF-0123") is False


def test_validator_rejects_wrong_group_count():
    assert is_valid_installation_id("MC1-0123-4567-89AB-CDEF") is False


def test_validator_rejects_invalid_characters():
    assert is_valid_installation_id("MC1-012G-4567-89AB-CDEF-0123") is False


def test_validator_rejects_trailing_newline():
    # re.match()'s `$` anchor (without re.MULTILINE) tolerates a trailing
    # "\n" before end-of-string - fullmatch() is required to actually
    # reject it, matching server.py's is_valid_node_id() convention for
    # the same class of strict whole-string format check.
    assert is_valid_installation_id("MC1-0123-4567-89AB-CDEF-0123\n") is False


def test_format_utc_iso8601_stays_utc_not_local():
    # Regression guard for the meshsrv/radio_identity.py's utc_now_iso()
    # trap: that helper calls .astimezone() and silently converts to the
    # local system timezone despite its name. Feed a non-UTC-aware value
    # and confirm the output is genuinely UTC-offset, not local-converted.
    non_utc = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    result = format_utc_iso8601(non_utc)
    assert result == "2026-09-01T07:00:00+00:00"


def test_get_confirmed_utc_time_returns_none_when_not_synchronized():
    with patch("meshsrv.time_service.get_status", return_value={"synchronized": False}):
        assert get_confirmed_utc_time() is None


def test_get_confirmed_utc_time_returns_a_utc_iso_string_when_synchronized():
    with patch("meshsrv.time_service.get_status", return_value={"synchronized": True}):
        result = get_confirmed_utc_time()
    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.utcoffset() == timedelta(0)
