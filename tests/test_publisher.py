from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from car_telemetry.outbox import PRIORITY_ROUTINE, SqliteOutbox, serialize_frame
from car_telemetry.publisher import (
    CONTENT_TYPE,
    PAYLOAD_FORMAT_UTF8,
    DrainReport,
    PublishResult,
    credential_from_settings,
    drain_once,
    prepare_for_send,
    should_replay,
)
from car_telemetry.device_identity import CredentialError

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
DEVICE = "DEV-001"
TOPIC = f"roadnode/v2/devices/{DEVICE}/frame"


def test_v2_credential_comes_from_single_env_settings_file(tmp_path):
    configured = SimpleNamespace(
        device_id=DEVICE,
        mqtt_username="device-rn-0001",
        mqtt_password="admin-issued-secret",
    )

    credential = credential_from_settings(configured)

    assert credential.device_id == DEVICE
    assert credential.username == "device-rn-0001"
    assert credential.secret == "admin-issued-secret"


def test_v2_credential_requires_both_env_values(tmp_path):
    configured = SimpleNamespace(
        device_id=DEVICE,
        mqtt_username="device-rn-0001",
        mqtt_password="",
    )

    with pytest.raises(CredentialError, match="must both be set"):
        credential_from_settings(configured)


def test_v2_credential_does_not_fall_back_to_another_file():
    configured = SimpleNamespace(
        device_id=DEVICE,
        mqtt_username="",
        mqtt_password="",
    )

    with pytest.raises(CredentialError, match="must both be set"):
        credential_from_settings(configured)


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


def test_worker_reports_the_exact_broker_and_topic_it_publishes_to(outbox):
    import threading
    from dataclasses import replace

    from car_telemetry.config import settings
    from car_telemetry.publisher import worker
    from car_telemetry.state import DeviceState

    configured = replace(
        settings(),
        device_id=DEVICE,
        mqtt_enabled=True,
        mqtt_host="mqtt.obd2.ragnogroup.com",
        mqtt_port=8883,
        mqtt_tls=True,
        mqtt_username="device-rn-0001",
    )
    state = DeviceState(DEVICE, "VEH-001", 1)
    enqueue(outbox, 1)
    stop = threading.Event()
    broker = FakeBroker()
    original = broker.publish

    def publish_then_stop(*args, **kwargs):
        stop.set()
        return original(*args, **kwargs)

    broker.publish = publish_then_stop
    worker(configured, state, stop, outbox=outbox, transport=broker)

    publisher = state.snapshot()["publisher"]
    assert publisher["broker"] == "mqtt.obd2.ragnogroup.com:8883"
    assert publisher["tls"] is True
    assert publisher["clientId"] == DEVICE
    assert publisher["username"] == "device-rn-0001"
    assert publisher["topic"] == TOPIC
    assert publisher["connected"] is True
    assert publisher["published"] == 1
    assert broker.received[0]["topic"] == publisher["topic"]


def test_disabled_worker_says_why(outbox):
    import threading
    from dataclasses import replace

    from car_telemetry.config import settings
    from car_telemetry.publisher import worker
    from car_telemetry.state import DeviceState

    state = DeviceState(DEVICE, "VEH-001", 1)
    worker(replace(settings(), mqtt_enabled=False), state, threading.Event(), outbox=outbox)
    assert state.snapshot()["publisher"] == {
        "enabled": False,
        "connected": False,
        "error": "MQTT_ENABLED is false",
    }


class PipelinedBroker(FakeBroker):
    """Records when each message was sent and when its PUBACK was collected."""

    def __init__(self, *, unacknowledged=(), **kwargs):
        super().__init__(**kwargs)
        self.events: list[tuple[str, int]] = []
        self.unacknowledged = set(unacknowledged)

    def publish_nowait(self, topic, payload, **kwargs):
        document = json.loads(payload.decode("utf-8"))
        self.events.append(("send", document["sequence"]))
        self.received.append({"topic": topic, "document": document, **kwargs})
        return document["sequence"]

    def wait_for_ack(self, pending, timeout):
        self.events.append(("ack", pending))
        return PublishResult(acknowledged=pending not in self.unacknowledged, reason="simulated timeout")


def test_pipelined_drain_sends_the_batch_in_order_before_collecting_pubacks(outbox):
    for sequence in range(1, 6):
        enqueue(outbox, sequence)
    broker = PipelinedBroker()

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE, batch_size=5)

    assert report.published == 5
    assert outbox.depth() == 0
    assert broker.events == [("send", n) for n in range(1, 6)] + [("ack", n) for n in range(1, 6)]
    assert [sent["document"]["sequence"] for sent in broker.received] == [1, 2, 3, 4, 5]
    assert broker.received[0]["qos"] == 1
    assert broker.received[0]["content_type"] == CONTENT_TYPE


def test_pipelined_drain_keeps_only_the_rows_without_puback(outbox):
    for sequence in range(1, 5):
        enqueue(outbox, sequence)
    broker = PipelinedBroker(unacknowledged={2})

    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE, batch_size=4)

    assert report.published == 3
    assert report.failed == 1
    assert outbox.depth() == 1
    remaining = outbox.batch(10)[0]
    assert json.loads(remaining.payload)["sequence"] == 2
    assert remaining.attempts == 1


def test_pipelined_drain_stops_sending_when_the_connection_drops(outbox):
    for sequence in range(1, 4):
        enqueue(outbox, sequence)
    broker = PipelinedBroker()
    original = broker.publish_nowait

    def send_then_disconnect(topic, payload, **kwargs):
        handle = original(topic, payload, **kwargs)
        broker.connected = False
        return handle

    broker.publish_nowait = send_then_disconnect
    report = drain_once(outbox, broker, device_id=DEVICE, now=BASE, batch_size=3)

    assert [event for event in broker.events if event[0] == "send"] == [("send", 1)]
    assert report.published == 1
    assert outbox.depth() == 2


def test_worker_reports_why_the_broker_refused_the_connection(outbox):
    import threading
    from dataclasses import replace

    from car_telemetry.config import settings
    from car_telemetry.publisher import worker
    from car_telemetry.state import DeviceState

    stop = threading.Event()

    class RefusingBroker(FakeBroker):
        refused_reason = "Not authorized"

        def connect(self):
            self.connects += 1
            stop.set()

    configured = replace(settings(), device_id=DEVICE, mqtt_enabled=True, mqtt_host="broker.test")
    state = DeviceState(DEVICE, "VEH-001", 1)
    worker(configured, state, stop, outbox=outbox, transport=RefusingBroker(connected=False))

    publisher = state.snapshot()["publisher"]
    assert publisher["connected"] is False
    assert publisher["error"] == "broker refused connection: Not authorized"
