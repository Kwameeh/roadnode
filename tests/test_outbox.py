from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from car_telemetry.outbox import (
    PRIORITY_CRITICAL,
    PRIORITY_ROUTINE,
    OutboxError,
    SqliteOutbox,
    serialize_frame,
)

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(offset_seconds: float) -> str:
    value = BASE + timedelta(seconds=offset_seconds)
    return value.isoformat().replace("+00:00", "Z")


def frame_payload(sample_count: int = 20) -> bytes:
    return serialize_frame(
        {
            "schemaVersion": 2,
            "messageType": "vehicle_frame",
            "payload": {"imu": {"samples": [[0, 1, 2, 3, 4, 5, 6]] * sample_count}},
        }
    )


def put(outbox: SqliteOutbox, index: int, **overrides) -> bool:
    params = {
        "message_id": f"DEV-001:boot:{index}",
        "topic": "roadnode/v2/devices/DEV-001/frame",
        "payload": frame_payload(),
        "captured_at": iso(index),
        "qos": 1,
        "retain": False,
        "priority": PRIORITY_ROUTINE,
    }
    params.update(overrides)
    return outbox.put(**params)


@pytest.fixture
def outbox(tmp_path):
    store = SqliteOutbox(tmp_path / "outbox.sqlite3")
    yield store
    store.close()


def test_put_and_oldest_returns_first_captured(outbox):
    put(outbox, 3)
    put(outbox, 1)
    put(outbox, 2)

    item = outbox.oldest()

    assert item is not None
    assert item.message_id == "DEV-001:boot:1"
    assert item.captured_at == iso(1)
    assert item.qos == 1
    assert item.retain is False
    assert item.attempts == 0
    assert outbox.depth() == 3


def test_duplicate_message_id_is_ignored(outbox):
    assert put(outbox, 1) is True
    assert put(outbox, 1) is False
    assert outbox.depth() == 1


def test_delete_removes_only_the_acknowledged_message(outbox):
    put(outbox, 1)
    put(outbox, 2)

    assert outbox.delete("DEV-001:boot:1") is True
    assert outbox.delete("DEV-001:boot:1") is False

    assert outbox.depth() == 1
    assert outbox.oldest().message_id == "DEV-001:boot:2"


def test_failed_publish_records_attempt_and_keeps_the_row(outbox):
    put(outbox, 1)

    outbox.record_attempt("DEV-001:boot:1", at=iso(5))
    outbox.record_attempt("DEV-001:boot:1", at=iso(6))

    item = outbox.oldest()
    assert item.attempts == 2
    assert item.last_attempt_at == iso(6)
    assert outbox.depth() == 1, "an unacknowledged message must never be dropped"


def test_queue_survives_restart(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    first = SqliteOutbox(path)
    put(first, 1)
    put(first, 2)
    first.record_attempt("DEV-001:boot:1")
    first.close()

    second = SqliteOutbox(path)
    try:
        assert second.depth() == 2
        item = second.oldest()
        assert item.message_id == "DEV-001:boot:1"
        assert item.attempts == 1
        assert item.payload == frame_payload()
    finally:
        second.close()


def test_unclosed_queue_survives_simulated_power_loss(tmp_path):
    """A row committed without close() must still be present on reopen."""
    path = tmp_path / "outbox.sqlite3"
    first = SqliteOutbox(path)
    put(first, 1)
    # No close(): emulate abrupt power loss after the commit.

    second = SqliteOutbox(path)
    try:
        assert second.depth() == 1
    finally:
        second.close()
        first.close()


def test_batch_returns_oldest_first_page(outbox):
    for index in range(5):
        put(outbox, index)

    batch = outbox.batch(3)

    assert [item.message_id for item in batch] == [
        "DEV-001:boot:0",
        "DEV-001:boot:1",
        "DEV-001:boot:2",
    ]
    assert outbox.batch(0) == ()


def test_evict_drops_oldest_routine_frames_when_over_size(outbox):
    for index in range(5):
        put(outbox, index)
    size = outbox.bytes_used()

    evicted = outbox.evict(max_bytes=size // 2, max_age_seconds=0, now=BASE)

    assert evicted > 0
    remaining = [item.message_id for item in outbox.batch(10)]
    assert "DEV-001:boot:0" not in remaining
    assert "DEV-001:boot:4" in remaining, "newest frames are retained"


def test_evict_prefers_routine_over_critical(outbox):
    put(outbox, 0, priority=PRIORITY_CRITICAL)
    put(outbox, 1)
    put(outbox, 2)

    outbox.evict(max_bytes=outbox.bytes_used() - 1, max_age_seconds=0, now=BASE)

    remaining = {item.message_id for item in outbox.batch(10)}
    assert "DEV-001:boot:0" in remaining, "critical messages are evicted last"
    assert "DEV-001:boot:1" not in remaining


def test_evict_drops_messages_older_than_max_age(outbox):
    put(outbox, 0)
    put(outbox, 1)

    evicted = outbox.evict(
        max_bytes=0, max_age_seconds=30, now=BASE + timedelta(seconds=60)
    )

    assert evicted == 2
    assert outbox.depth() == 0


def test_eviction_reports_dropped_counts_and_missing_range(outbox):
    for index in range(4):
        put(outbox, index)

    outbox.evict(max_bytes=0, max_age_seconds=1, now=BASE + timedelta(seconds=10))

    stats = outbox.stats()
    assert stats.depth == 0
    assert stats.dropped_messages == 4
    assert stats.dropped_imu_samples == 80, "20 IMU samples per evicted frame"
    assert stats.first_missing_captured_at == iso(0)
    assert stats.last_missing_captured_at == iso(3)


def test_stats_reports_depth_and_capture_bounds(outbox):
    put(outbox, 1)
    put(outbox, 7)

    stats = outbox.stats()

    assert stats.depth == 2
    assert stats.bytes_used == len(frame_payload()) * 2
    assert stats.oldest_captured_at == iso(1)
    assert stats.newest_captured_at == iso(7)
    assert stats.dropped_messages == 0
    assert stats.first_missing_captured_at is None


def test_put_rejects_invalid_input(outbox):
    with pytest.raises(ValueError):
        put(outbox, 1, message_id="  ")
    with pytest.raises(ValueError):
        put(outbox, 1, topic="")
    with pytest.raises(ValueError):
        put(outbox, 1, qos=3)
    with pytest.raises(ValueError):
        put(outbox, 1, priority="whenever")
    with pytest.raises(ValueError):
        put(outbox, 1, captured_at="2026-03-01T12:00:00")
    with pytest.raises(TypeError):
        put(outbox, 1, payload="not-bytes")


def test_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    SqliteOutbox(path).close()
    connection = sqlite3.connect(str(path))
    connection.execute("UPDATE outbox_meta SET value='99' WHERE key='schemaVersion'")
    connection.commit()
    connection.close()

    with pytest.raises(OutboxError):
        SqliteOutbox(path)


def test_concurrent_writers_persist_every_message(outbox):
    def writer(start: int) -> None:
        for index in range(start, start + 25):
            put(outbox, index)

    threads = [threading.Thread(target=writer, args=(base,)) for base in (0, 25, 50, 75)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outbox.depth() == 100


def test_payload_round_trips_as_compact_json(outbox):
    put(outbox, 1)

    payload = outbox.oldest().payload

    assert b" " not in payload, "insignificant whitespace is removed before storage"
    assert json.loads(payload.decode("utf-8"))["schemaVersion"] == 2


# --- MQTT-006: deterministic bounds, priority, and drop accounting -----------


def test_eviction_is_deterministic_across_identical_queues(tmp_path):
    """Same inputs must evict exactly the same messages every time."""
    outcomes = []
    for run in range(3):
        store = SqliteOutbox(tmp_path / f"run-{run}.sqlite3")
        for index in range(20):
            put(store, index, priority=PRIORITY_CRITICAL if index % 5 == 0 else PRIORITY_ROUTINE)
        store.evict(max_bytes=len(frame_payload()) * 8, max_age_seconds=0, now=BASE)
        outcomes.append(
            (
                [item.message_id for item in store.batch(50)],
                store.stats().dropped_messages,
                store.stats().first_missing_captured_at,
                store.stats().last_missing_captured_at,
            )
        )
        store.close()

    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_size_bound_is_enforced_exactly(outbox):
    for index in range(10):
        put(outbox, index)
    one = len(frame_payload())

    outbox.evict(max_bytes=one * 4, max_age_seconds=0, now=BASE)

    assert outbox.bytes_used() <= one * 4
    assert outbox.depth() == 4


def test_message_exactly_at_the_age_bound_is_retained(outbox):
    put(outbox, 0)

    # Exactly 24h old: not yet older than the bound.
    outbox.evict(max_bytes=0, max_age_seconds=86400, now=BASE + timedelta(seconds=86400))
    assert outbox.depth() == 1

    outbox.evict(max_bytes=0, max_age_seconds=86400, now=BASE + timedelta(seconds=86401))
    assert outbox.depth() == 0


def test_critical_messages_survive_until_no_routine_frame_remains(outbox):
    put(outbox, 0, priority=PRIORITY_CRITICAL)
    put(outbox, 1, priority=PRIORITY_CRITICAL)
    for index in range(2, 8):
        put(outbox, index)

    outbox.evict(max_bytes=len(frame_payload()) * 2, max_age_seconds=0, now=BASE)

    remaining = {item.message_id for item in outbox.batch(50)}
    assert remaining == {"DEV-001:boot:0", "DEV-001:boot:1"}, (
        "every routine frame is evicted before any critical message"
    )


def test_drop_counters_accumulate_across_successive_evictions(outbox):
    for index in range(3):
        put(outbox, index)
    outbox.evict(max_bytes=0, max_age_seconds=1, now=BASE + timedelta(seconds=10))
    first = outbox.stats()

    for index in range(10, 13):
        put(outbox, index)
    outbox.evict(max_bytes=0, max_age_seconds=1, now=BASE + timedelta(seconds=100))
    second = outbox.stats()

    assert first.dropped_messages == 3
    assert second.dropped_messages == 6, "counters are cumulative, never reset"
    assert second.first_missing_captured_at == iso(0), "earliest gap is retained"
    assert second.last_missing_captured_at == iso(12), "latest gap advances"


def test_age_eviction_ignores_priority_because_the_data_is_stale(outbox):
    put(outbox, 0, priority=PRIORITY_CRITICAL)
    put(outbox, 1)

    outbox.evict(max_bytes=0, max_age_seconds=1, now=BASE + timedelta(seconds=60))

    assert outbox.depth() == 0
    assert outbox.stats().dropped_messages == 2


def test_evict_is_a_noop_when_within_bounds(outbox):
    for index in range(3):
        put(outbox, index)

    evicted = outbox.evict(max_bytes=10 * 1024 * 1024, max_age_seconds=86400, now=BASE)

    assert evicted == 0
    assert outbox.depth() == 3
    assert outbox.stats().dropped_messages == 0
    assert outbox.stats().first_missing_captured_at is None
