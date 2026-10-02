"""H1-B2 (F6): /api/telemetry/history used to always answer a plain
`limit` (chat-telemetry.js sent limit=5000 then filtered client-side) - a
30-day chart was limited to whatever the last 5000 records (across ALL
nodes sharing one history list) happened to be, which could be well
under 30 days of actual coverage on an active mesh. A `since` query now
downsamples server-side into at most `max_points` fixed-width buckets
spanning the full requested range, independent of any record-count cap.
"""

import time

import pytest


LOCAL_NODE_ID = "!aabbccdd"  # matches tests/conftest.py's synthetic config


@pytest.fixture
def srv(server_module):
    server_module.telemetry.telemetry_history.clear()
    yield server_module
    server_module.telemetry.telemetry_history.clear()


def _seed_local_history(srv, days, interval_seconds, now_ts):
    start = now_ts - days * 86400
    ts = start
    count = 0
    while ts <= now_ts:
        srv.telemetry.telemetry_history.append({
            "time": "x", "timestamp": ts,
            "temperature": 20.0 + (count % 5), "humidity": 50.0, "pressure": None,
            "voltage": 4.0, "current": None, "power": None, "source": "local",
        })
        ts += interval_seconds
        count += 1
    return count


def test_since_query_spans_the_full_requested_range_within_max_points(srv):
    now_ts = time.time()
    total_records = _seed_local_history(srv, days=30, interval_seconds=120, now_ts=now_ts)
    assert total_records > 5000  # the exact old flat cap this replaces

    since = now_ts - 30 * 86400
    client = srv.app.test_client()
    resp = client.get(f"/api/telemetry/history?since={since}&max_points=1000")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_in_range"] == total_records
    assert len(data["history"]) <= 1000
    assert data["downsampled"] is True
    # Buckets span from `since` to "now", not just to the oldest/newest
    # actual record - the first and last bucket timestamps must bracket
    # close to the full requested range.
    assert data["history"][0]["timestamp"] >= since - 1
    assert data["history"][-1]["timestamp"] <= now_ts + 1
    assert data["history"][0]["timestamp"] < data["history"][-1]["timestamp"]


def test_since_query_averages_numeric_fields_ignoring_none(srv):
    now_ts = time.time()
    since = now_ts - 100
    # Two records in the same bucket (max_points=1 forces exactly one
    # bucket spanning the whole 100s range).
    srv.telemetry.telemetry_history.append({
        "timestamp": since + 10, "temperature": 10.0, "humidity": None, "voltage": 4.0,
    })
    srv.telemetry.telemetry_history.append({
        "timestamp": since + 20, "temperature": 20.0, "humidity": 60.0, "voltage": None,
    })

    client = srv.app.test_client()
    resp = client.get(f"/api/telemetry/history?since={since}&max_points=1")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["history"]) == 1
    bucket = data["history"][0]
    assert bucket["temperature"] == pytest.approx(15.0)  # (10+20)/2
    assert bucket["humidity"] == 60.0  # only one non-None value - not averaged with a false 0
    assert bucket["voltage"] == 4.0  # only one non-None value


def test_since_query_bucket_with_no_records_reports_none_not_zero(srv):
    now_ts = time.time()
    since = now_ts - 1000
    # One record near the end of the range only - most buckets are empty.
    srv.telemetry.telemetry_history.append({"timestamp": now_ts - 1, "temperature": 25.0})

    client = srv.app.test_client()
    resp = client.get(f"/api/telemetry/history?since={since}&max_points=10")

    assert resp.status_code == 200
    data = resp.get_json()
    # Every bucket that got no records is simply absent (never a false
    # 0-valued entry) - only the one bucket with the actual record exists.
    assert len(data["history"]) == 1
    assert data["history"][0]["temperature"] == 25.0


def test_max_points_is_capped_at_5000(srv):
    now_ts = time.time()
    since = now_ts - 10000
    # One record every second across the whole range - enough distinct
    # buckets to actually distinguish "capped at 5000" from "not capped"
    # (an uncapped max_points=999999 would let up to ~10000 of these land
    # in their own one-second bucket).
    for i in range(0, 10000, 1):
        srv.telemetry.telemetry_history.append({"timestamp": since + i, "temperature": float(i)})

    client = srv.app.test_client()
    resp = client.get(f"/api/telemetry/history?since={since}&max_points=999999")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["history"]) <= 5000


def test_plain_limit_without_since_is_unchanged(srv):
    now_ts = time.time()
    for i in range(10):
        srv.telemetry.telemetry_history.append({
            "timestamp": now_ts - (10 - i), "temperature": float(i), "source": "local",
        })

    client = srv.app.test_client()
    resp = client.get("/api/telemetry/history?limit=3")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "total" in data and "total_in_range" not in data
    assert len(data["history"]) == 3
    assert [r["temperature"] for r in data["history"]] == [7.0, 8.0, 9.0]


def test_remote_records_do_not_evict_local_history_in_the_history_route(srv):
    """Companion to tests/test_telemetry_per_node_cap.py's storage-layer
    coverage - confirms the ROUTE's own local-only filtering (used when no
    node_id is given) still only ever sees local records, regardless of
    how much remote history exists alongside it."""
    now_ts = time.time()
    srv.telemetry.telemetry_history.append({"timestamp": now_ts - 1, "temperature": 20.0, "source": "local"})
    for i in range(20):
        srv.telemetry.telemetry_history.append({
            "node_id": "!11223344", "timestamp": now_ts - i, "voltage": 4.0,
        })

    client = srv.app.test_client()
    resp = client.get("/api/telemetry/history?limit=100")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1
    assert data["history"][0]["temperature"] == 20.0


@pytest.mark.parametrize("since_value", ["nan", "inf", "-inf"])
def test_since_nan_or_infinity_is_rejected_with_400_not_a_crash(srv, since_value):
    """H1-B2 review fix: request.args.get(type=float) happily parses the
    literal strings "nan"/"inf"/"-inf" (Python's float() accepts them) -
    math.ceil(NaN) used to raise inside _downsample_telemetry_history(),
    a 500 from a single malformed query string."""
    client = srv.app.test_client()
    resp = client.get(f"/api/telemetry/history?since={since_value}&max_points=10")

    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_since"


def test_since_in_the_future_returns_an_empty_result_not_an_error(srv):
    now_ts = time.time()
    srv.telemetry.telemetry_history.append({"timestamp": now_ts - 1, "temperature": 20.0, "source": "local"})

    client = srv.app.test_client()
    resp = client.get(f"/api/telemetry/history?since={now_ts + 10000}&max_points=10")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["history"] == []
    assert data["total_in_range"] == 0
