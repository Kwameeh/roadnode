from __future__ import annotations

import pytest

from car_telemetry.retained_state import (
    DEGRADED,
    OFFLINE,
    ONLINE,
    SHUTTING_DOWN,
    RetainedStatePublisher,
    build_last_will,
    build_metadata,
    build_status,
    freshness_is_unprovable,
    metadata_revision,
)

DEVICE = "DEV-001"
BODY = {"vin": "WVWZZZ1JZ3W386752", "supportedSignals": ["RPM", "SPEED"]}


def test_metadata_and_status_are_retained_on_their_own_topics():
    metadata = build_metadata(DEVICE, BODY, sent_at="2026-03-01T12:00:00Z")
    status = build_status(DEVICE, ONLINE, sent_at="2026-03-01T12:00:00Z")

    assert metadata.topic == "roadnode/v2/devices/DEV-001/metadata"
    assert status.topic == "roadnode/v2/devices/DEV-001/status"
    assert metadata.retain is True and status.retain is True
    assert metadata.qos == 1


def test_metadata_revision_ignores_key_order():
    a = metadata_revision({"vin": "X", "supportedSignals": ["RPM"]})
    b = metadata_revision({"supportedSignals": ["RPM"], "vin": "X"})

    assert a == b


def test_metadata_revision_changes_with_content():
    assert metadata_revision(BODY) != metadata_revision({**BODY, "vin": "OTHER"})


def test_metadata_publishes_only_on_change():
    publisher = RetainedStatePublisher(DEVICE)

    first = publisher.metadata_if_changed(BODY)
    unchanged = publisher.metadata_if_changed(dict(reversed(list(BODY.items()))))
    changed = publisher.metadata_if_changed({**BODY, "supportedSignals": ["RPM"]})

    assert first is not None
    assert unchanged is None, "republishing unchanged metadata makes 'last changed' meaningless"
    assert changed is not None


def test_status_publishes_on_transition():
    publisher = RetainedStatePublisher(DEVICE)

    assert publisher.status_if_changed(ONLINE) is not None
    assert publisher.status_if_changed(ONLINE) is None
    assert publisher.status_if_changed(DEGRADED) is not None


def test_heartbeat_republishes_unchanged_state():
    publisher = RetainedStatePublisher(DEVICE)
    publisher.status_if_changed(ONLINE)

    beat = publisher.status_if_changed(ONLINE, queue_depth=42, force=True)

    assert beat is not None
    assert beat.document()["queueDepth"] == 42


def test_reset_after_reconnect_republishes_everything():
    publisher = RetainedStatePublisher(DEVICE)
    publisher.metadata_if_changed(BODY)
    publisher.status_if_changed(ONLINE)

    publisher.reset()

    assert publisher.metadata_if_changed(BODY) is not None
    assert publisher.status_if_changed(ONLINE) is not None


def test_last_will_carries_no_device_send_time():
    will = build_last_will(DEVICE)
    document = will.document()

    assert document["state"] == OFFLINE
    assert document["reason"] == "unexpected_disconnect"
    # The device is not sending this; stamping a device time would lie.
    assert document["sentAt"] is None
    assert will.retain is True


def test_retained_status_cannot_prove_freshness():
    will = build_last_will(DEVICE).document()
    live = build_status(DEVICE, ONLINE, sent_at="2026-03-01T12:00:00Z").document()

    assert freshness_is_unprovable(will) is True
    assert freshness_is_unprovable(live) is False


def test_status_carries_queue_depth_and_sensor_health():
    status = build_status(
        DEVICE, DEGRADED, queue_depth=1200, reason="gps_lost",
        sensor_health={"gps": "invalid"}, sent_at="2026-03-01T12:00:00Z",
    ).document()

    assert status["queueDepth"] == 1200
    assert status["reason"] == "gps_lost"
    assert status["sensorHealth"]["gps"] == "invalid"


@pytest.mark.parametrize("state", [ONLINE, OFFLINE, SHUTTING_DOWN, DEGRADED])
def test_all_documented_states_are_publishable(state):
    assert build_status(DEVICE, state).document()["state"] == state


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        build_status(DEVICE, "probably-fine")
    with pytest.raises(ValueError):
        RetainedStatePublisher(DEVICE).status_if_changed("probably-fine")
