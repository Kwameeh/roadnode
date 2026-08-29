import json

from car_telemetry.frame_builder import FrameContext, VehicleFrameBuilder
from car_telemetry.observations import ImuSample, ObservationStore, observation_meta
from contracts.mqtt.v2 import validate_vehicle_frame


def populated_store(sample_count=20):
    store = ObservationStore()
    store.update_gps(
        {
            "fix": True,
            "latitude": 5.6037,
            "longitude": -0.187,
            **observation_meta(
                observed_at="2026-08-27T14:25:31.900Z",
                source="gps.nmea",
                quality="valid",
                max_age_ms=3000,
            ),
        }
    )
    store.update_obd_status(connected=True, engine_on=True)
    store.update_obd_signal(
        "RPM",
        {
            "value": 1800,
            "unit": "rpm",
            **observation_meta(
                observed_at="2026-08-27T14:25:31.180Z",
                source="obd.pid",
                quality="valid",
                max_age_ms=2000,
            ),
        },
    )
    store.update_device(
        {
            "temperatureC": 51,
            "softwareVersion": "3.0.0",
            **observation_meta(
                observed_at="2026-08-27T14:25:31.800Z",
                source="device.os",
                quality="valid",
                max_age_ms=10000,
            ),
        }
    )
    store.update_imu_status(
        {
            "sampleRateHz": 20,
            "calibrationVersion": "imu-cal-v1-test",
            "orientation": "x-forward-y-left-z-up",
            "quality": "valid",
            "inactiveReason": None,
        }
    )
    for index in range(sample_count):
        store.append_imu_sample(
            ImuSample(
                observed_at=f"2026-08-27T14:25:31.{index * 50:03d}Z",
                ax=float(index),
                ay=0,
                az=9.80665,
                gx=0,
                gy=0,
                gz=0,
            )
        )
    return store


def context():
    return FrameContext(
        device_id="DEV-001",
        boot_id="550e8400-e29b-41d4-a716-446655440000",
        sequence=42,
        captured_from="2026-08-27T14:25:31.000Z",
        captured_to="2026-08-27T14:25:32.000Z",
        sent_at="2026-08-27T14:25:32.010Z",
        clock_source="gps",
        clock_quality="locked",
        clock_offset_ms=12,
    )


def test_builder_emits_one_valid_aligned_frame_with_twenty_samples():
    store = populated_store()
    snapshot = store.snapshot(context().captured_from, context().captured_to)
    frame = VehicleFrameBuilder().build(context(), snapshot)

    validate_vehicle_frame(
        frame,
        topic="roadnode/v2/devices/DEV-001/frame",
        authenticated_device_id="DEV-001",
    )
    assert frame["payload"]["intervalMs"] == 1000
    assert frame["payload"]["imu"]["sampleCount"] == 20
    assert [row[0] for row in frame["payload"]["imu"]["samples"]] == list(
        range(0, 1000, 50)
    )
    assert frame["payload"]["telemetry"]["obd"]["signals"]["RPM"][
        "observedAt"
    ] == "2026-08-27T14:25:31.180Z"


def test_builder_emits_explicit_empty_imu_state():
    store = populated_store(sample_count=0)
    store.update_imu_status(
        {
            "calibrationVersion": None,
            "quality": "invalid",
            "inactiveReason": "calibration_invalid",
        }
    )
    frame = VehicleFrameBuilder().build(
        context(), store.snapshot(context().captured_from, context().captured_to)
    )
    validate_vehicle_frame(frame)
    assert frame["payload"]["imu"]["samples"] == []
    assert frame["payload"]["imu"]["inactiveReason"] == "calibration_invalid"


def test_builder_requires_device_health_and_complete_drop_accounting():
    empty = ObservationStore().snapshot(
        "2026-08-27T14:25:31.000Z", "2026-08-27T14:25:32.000Z"
    )
    try:
        VehicleFrameBuilder().build(context(), empty)
    except ValueError as exc:
        assert "device health" in str(exc)
    else:
        raise AssertionError("missing device health must fail")

    dropped = FrameContext(**{**context().__dict__, "dropped_imu_samples": 1})
    try:
        VehicleFrameBuilder().build(
            dropped,
            populated_store().snapshot(dropped.captured_from, dropped.captured_to),
        )
    except ValueError as exc:
        assert "drop accounting" in str(exc)
    else:
        raise AssertionError("incomplete drop accounting must fail")



def test_worker_persists_each_frame_to_the_outbox(tmp_path):
    """The 1 Hz worker must durably queue every frame it builds."""
    import threading
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from car_telemetry.frame_builder import frame_topic, worker
    from car_telemetry.outbox import SqliteOutbox
    from car_telemetry.state import DeviceState

    base = datetime(2026, 8, 27, 14, 25, 31, tzinfo=timezone.utc)
    ticks = iter(base + timedelta(seconds=n) for n in range(0, 400))

    settings = SimpleNamespace(
        device_id="DEV-001",
        outbox_file=str(tmp_path / "outbox.sqlite3"),
        outbox_max_bytes=256 * 1024 * 1024,
        outbox_max_age_seconds=24 * 60 * 60,
    )
    state = DeviceState("DEV-001", "VEH-001", 3)
    store = populated_store()
    stop = threading.Event()
    outbox = SqliteOutbox(settings.outbox_file)

    def clock():
        # Always ahead of the next boundary so the worker never blocks.
        return next(ticks)

    def run():
        worker(settings, state, store, stop, now=clock, outbox=outbox)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(200):
        if outbox.depth() >= 3:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=5)

    try:
        assert outbox.depth() >= 3
        item = outbox.oldest()
        assert item.topic == frame_topic("DEV-001")
        assert item.qos == 1
        assert item.retain is False
        assert item.attempts == 0

        frame = json.loads(item.payload.decode("utf-8"))
        validate_vehicle_frame(frame)
        assert frame["messageId"] == item.message_id
        assert frame["capturedAt"] == item.captured_at

        snapshot = state.snapshot()["frame"]
        assert snapshot["error"] is None
        assert snapshot["queueDepth"] == outbox.depth()
    finally:
        outbox.close()
