from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from car_telemetry.outbox import PRIORITY_ROUTINE, SqliteOutbox, serialize_frame
from car_telemetry.publisher import (
    CONTENT_TYPE,
    PAYLOAD_FORMAT_UTF8,
    DrainReport,
    PublishResult,
    drain_once,
    prepare_for_send,
    should_replay,
)

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
DEVICE = "DEV-001"
TOPIC = f"roadnode/v2/devices/{DEVICE}/frame"


def iso(offset_seconds: float) -> str:
    return (BASE + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def frame(sequence: int) -> dict:
    return {
        "schemaVersion": 2,
        "messageId": f"{DEVICE}:boot:{sequence}",
        "messageType": "vehicle_frame",
        "deviceId": DEVICE,
        "bootId": "boot",
        "sequence": sequence,
        "capturedAt": iso(sequence),
        "sentAt": iso(sequence),
        "clock": {"source": "gps", "quality": "locked", "offsetMs": 0},
        "replay": False,
        "dropped": {"messages": 0, "imuSamples": 0},
        "payload": {"imu": {"sampleCount": 20}},
    }


class FakeBroker:
    """In-process stand-in that records exactly what a real broker would see."""

    def __init__(self, *, connected=True, acknowledge=True, fail_after=None):
        self.connected = connected
        self.acknowledge = acknowledge
        self.fail_after = fail_after
        self.received: list[dict] = []
        self.connects = 0
        self.raise_on_publish: Exception | None = None

    def connect(self):
        self.connects += 1
        self.connected = True

    def publish(self, topic, payload, *, qos, retain, content_type,
                payload_format_indicator, message_expiry_interval=None):
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        if self.fail_after is not None and len(self.received) >= self.fail_after:
            return PublishResult(acknowledged=False, reason="simulated timeout")
        self.received.append(
            {
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "retain": retain,
                "contentType": content_type,
                "payloadFormatIndicator": payload_format_indicator,
                "document": json.loads(payload.decode("utf-8")),
            }
        )
        return PublishResult(acknowledged=self.acknowledge)

    def disconnect(self):
        self.connected = False


@pytest.fixture
def outbox(tmp_path):
    store = SqliteOutbox(tmp_path / "outbox.sqlite3")
    yield store
    store.close()


def enqueue(outbox, sequence, *, topic=TOPIC):
    document = frame(sequence)
    outbox.put(
        message_id=document["messageId"],
        topic=topic,
        payload=serialize_frame(document),
        captured_at=document["capturedAt"],
        qos=1,
        retain=False,
        priority=PRIORITY_ROUTINE,
    )
    return document


# --- MQTT-004: QoS-1 publish and PUBACK-gated deletion ----------------------


def test_publishes_with_qos1_and_mqtt5_content_properties(outbox):
    enqueue(outbox, 1)
    broker = FakeBroker()

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert report.published == 1
    sent = broker.received[0]
    assert sent["topic"] == TOPIC
    assert sent["qos"] == 1
    assert sent["retain"] is False
    assert sent["contentType"] == CONTENT_TYPE
    assert sent["payloadFormatIndicator"] == PAYLOAD_FORMAT_UTF8


def test_row_is_deleted_only_after_puback(outbox):
    enqueue(outbox, 1)
    broker = FakeBroker()

    drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert outbox.depth() == 0


def test_missing_puback_keeps_the_row_and_records_an_attempt(outbox):
    enqueue(outbox, 1)
    broker = FakeBroker(acknowledge=False)

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert report.published == 0
    assert report.failed == 1
    assert outbox.depth() == 1, "an unacknowledged message must survive"
    assert outbox.oldest().attempts == 1


def test_transport_exception_keeps_the_row(outbox):
    enqueue(outbox, 1)
    broker = FakeBroker()
    broker.raise_on_publish = ConnectionResetError("broker went away")

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert report.failed == 1
    assert outbox.depth() == 1
    assert outbox.oldest().attempts == 1


def test_drain_publishes_oldest_first(outbox):
    for sequence in (3, 1, 2):
        enqueue(outbox, sequence)
    broker = FakeBroker()

    drain_once(outbox, broker, device_id=DEVICE, now=BASE + timedelta(seconds=100))

    order = [item["document"]["messageId"] for item in broker.received]
    assert order == [f"{DEVICE}:boot:1", f"{DEVICE}:boot:2", f"{DEVICE}:boot:3"]


def test_drain_stops_at_the_first_failure_preserving_order(outbox):
    for sequence in range(1, 5):
        enqueue(outbox, sequence)
    broker = FakeBroker(fail_after=2)

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert report.published == 2
    assert outbox.depth() == 2, "later messages are not skipped past a failure"
    assert outbox.oldest().message_id == f"{DEVICE}:boot:3"


def test_nothing_is_published_while_disconnected(outbox):
    enqueue(outbox, 1)
    broker = FakeBroker(connected=False)

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert report.published == 0
    assert broker.received == []
    assert outbox.depth() == 1


def test_batch_size_bounds_one_drain(outbox):
    for sequence in range(1, 11):
        enqueue(outbox, sequence)
    broker = FakeBroker()

    report = drain_once(outbox, broker, device_id=DEVICE, batch_size=4, now=BASE)

    assert report.published == 4
    assert outbox.depth() == 6


# --- exact-namespace enforcement (SEC-001 x MQTT-004) -----------------------


def test_message_for_another_device_is_never_published(outbox):
    enqueue(outbox, 1, topic="roadnode/v2/devices/DEV-002/frame")
    broker = FakeBroker()

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert broker.received == [], "a foreign-namespace topic must never reach the broker"
    assert report.rejected == 1
    assert outbox.depth() == 0, "it is dropped rather than blocking the queue forever"


def test_malformed_payload_is_dropped_rather_than_blocking(outbox):
    outbox.put(
        message_id="DEV-001:boot:bad",
        topic=TOPIC,
        payload=b"\xff\xfe not json",
        captured_at=iso(1),
    )
    enqueue(outbox, 2)
    broker = FakeBroker()

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE)

    assert report.rejected == 1
    assert report.published == 1, "the queue keeps moving after a poison message"


# --- MQTT-005: replay semantics ---------------------------------------------


def test_prompt_publication_is_not_a_replay(outbox):
    enqueue(outbox, 1)
    broker = FakeBroker()

    drain_once(outbox, broker, device_id=DEVICE, now=BASE + timedelta(seconds=1))

    assert broker.received[0]["document"]["replay"] is False


def test_reconnect_marks_delayed_messages_as_replay(outbox):
    original = enqueue(outbox, 1)
    broker = FakeBroker()

    drain_once(outbox, broker, device_id=DEVICE, now=BASE + timedelta(minutes=5))

    sent = broker.received[0]["document"]
    assert sent["replay"] is True
    assert sent["messageId"] == original["messageId"], "identity is preserved"
    assert sent["capturedAt"] == original["capturedAt"], "capture time is preserved"
    assert sent["sentAt"] != original["sentAt"], "only send time moves"


def test_retry_after_a_failed_attempt_is_a_replay(outbox):
    enqueue(outbox, 1)
    failing = FakeBroker(acknowledge=False)
    drain_once(outbox, failing, device_id=DEVICE, now=BASE)

    recovered = FakeBroker()
    report = drain_once(outbox, recovered, device_id=DEVICE, now=BASE)

    assert report.replayed == 1
    assert recovered.received[0]["document"]["replay"] is True


def test_replay_preserves_every_field_except_sent_at_and_replay():
    original = frame(7)
    payload = serialize_frame(original)

    replayed = json.loads(
        prepare_for_send(payload, sent_at=iso(999), replay=True).decode("utf-8")
    )

    assert replayed["sentAt"] == iso(999)
    assert replayed["replay"] is True
    for key, value in original.items():
        if key not in {"sentAt", "replay"}:
            assert replayed[key] == value, f"{key} must not change during replay"


def test_should_replay_rules():
    class Item:
        def __init__(self, attempts, captured_at):
            self.attempts = attempts
            self.captured_at = captured_at

    fresh = Item(0, iso(0))
    assert should_replay(fresh, now=BASE + timedelta(seconds=1)) is False
    assert should_replay(fresh, now=BASE + timedelta(seconds=30)) is True
    assert should_replay(Item(1, iso(0)), now=BASE) is True


def test_full_outage_and_recovery_replays_backlog_oldest_first(outbox):
    """End-to-end MQTT-005: an outage builds a backlog that replays in order."""
    for sequence in range(1, 6):
        enqueue(outbox, sequence)

    offline = FakeBroker(connected=False)
    drain_once(outbox, offline, device_id=DEVICE, now=BASE)
    assert outbox.depth() == 5, "the outage loses nothing"

    online = FakeBroker()
    report = drain_once(
        outbox, online, device_id=DEVICE, now=BASE + timedelta(minutes=10)
    )

    assert report.published == 5
    assert report.replayed == 5
    assert outbox.depth() == 0
    documents = [item["document"] for item in online.received]
    assert [d["sequence"] for d in documents] == [1, 2, 3, 4, 5]
    assert all(d["replay"] is True for d in documents)
    assert [d["capturedAt"] for d in documents] == [iso(n) for n in range(1, 6)]
