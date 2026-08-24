"""Tests for meshsrv/radio_transport.py's TransportError.__str__().

Regression test for a bug caught live in prod during Task 44 verification:
TransportError is both a frozen dataclass and an Exception - the
dataclass-generated __init__ never calls Exception.__init__(code, message),
but BaseException.__new__ still captures the constructor's positional args
into self.args, so the inherited __str__ formatted that raw args tuple,
repr()-ing the enum in the process. Actual prod log line before the fix:
"[TIME SYNC] Attempt failed: (<TransportErrorCode.TIMEOUT: 'timeout'>,
'set_device_time() exceeded 10s')" - not a crash, but useless in logs.
"""
from meshsrv.radio_transport import TransportError, TransportErrorCode


def test_str_is_readable_not_a_raw_args_tuple():
    error = TransportError(TransportErrorCode.TIMEOUT, "connect() exceeded 30.0s")
    assert str(error) == "timeout: connect() exceeded 30.0s"


def test_str_works_for_every_error_code():
    for code in TransportErrorCode:
        error = TransportError(code, "some message")
        assert str(error) == f"{code.value}: some message"
        assert "TransportErrorCode" not in str(error)
