from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from car_telemetry.collection_mode import (
    ACTIVE,
    ACTIVE_INTERVAL_SECONDS,
    IDLE,
    IDLE_INTERVAL_SECONDS,
    CollectionModeMachine,
    ModeInputs,
    inputs_from_snapshot,
)
from car_telemetry.observations import ImuSample, ObservationStore, observation_meta

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def parked(**overrides) -> ModeInputs:
    """Positive evidence of a stationary, ignition-off vehicle."""
    values = {
        "engine_on": False,
        "speed_kph": 0.0,
        "accel_magnitude_mps2": 0.05,
        "gyro_magnitude_rad_s": 0.01,
        "obd_connected": True,
    }
    values.update(overrides)
    return ModeInputs(**values)


def settle(machine, *, start=0.0, seconds=31.0):
    """Drive the machine through a sustained quiet period."""
    machine.update(parked(), at(start))
    return machine.update(parked(), at(start + seconds))


# --- active behaviour -------------------------------------------------------


def test_engine_on_is_active_at_one_second():
    machine = CollectionModeMachine()

    decision = machine.update(ModeInputs(engine_on=True, obd_connected=True), at(0))

    assert decision.mode == ACTIVE
    assert decision.reason == "ignition_on"
    assert decision.interval_seconds == ACTIVE_INTERVAL_SECONDS == 1


@pytest.mark.parametrize(
    "inputs,expected_reason",
    [
        (ModeInputs(engine_on=True, obd_connected=True), "ignition_on"),
        (parked(speed_kph=25.0), "vehicle_moving"),
        (parked(accel_magnitude_mps2=2.5), "motion_detected"),
        (parked(gyro_magnitude_rad_s=0.9), "rotation_detected"),
    ],
)
def test_any_single_active_signal_keeps_the_device_active(inputs, expected_reason):
    machine = CollectionModeMachine(initial_mode=IDLE)

    decision = machine.update(inputs, at(0))

    assert decision.mode == ACTIVE
    assert decision.reason == expected_reason


def test_a_moving_vehicle_with_ignition_off_is_still_active():
    """Rolling or being towed must not be mistaken for idle."""
    machine = CollectionModeMachine()

    decision = machine.update(parked(speed_kph=40.0), at(0))

    assert decision.mode == ACTIVE
    assert decision.reason == "vehicle_moving"


# --- idle behaviour ---------------------------------------------------------


def test_sustained_quiet_becomes_idle_at_thirty_seconds():
    machine = CollectionModeMachine(idle_after_seconds=30)

    assert machine.update(parked(), at(0)).mode == ACTIVE, "idle is not immediate"
    assert machine.update(parked(), at(29)).mode == ACTIVE
    decision = machine.update(parked(), at(30))

    assert decision.mode == IDLE
    assert decision.reason == "vehicle_idle"
    assert decision.interval_seconds == IDLE_INTERVAL_SECONDS == 30


def test_idle_publishes_only_every_thirty_seconds():
    machine = CollectionModeMachine(idle_after_seconds=30)
    settle(machine)

    # Immediately after entering idle, intermediate seconds are skipped.
    assert machine.update(parked(), at(35)).publish_now is False
    assert machine.update(parked(), at(50)).publish_now is False
    assert machine.update(parked(), at(61)).publish_now is True


def test_active_publishes_every_second():
    machine = CollectionModeMachine()
    engine = ModeInputs(engine_on=True, obd_connected=True)

    machine.update(engine, at(0))
    assert machine.update(engine, at(1)).publish_now is True
    assert machine.update(engine, at(2)).publish_now is True


# --- transitions are never hidden -------------------------------------------


def test_entering_idle_is_reported_and_publishes_immediately():
    machine = CollectionModeMachine(idle_after_seconds=30)

    decision = settle(machine)

    assert decision.changed is True
    assert decision.publish_now is True, "a transition always emits a frame"


def test_waking_from_idle_publishes_immediately_not_on_the_idle_cadence():
    machine = CollectionModeMachine(idle_after_seconds=30)
    settle(machine)

    # One second later the engine starts: this must not wait for the 30s slot.
    decision = machine.update(ModeInputs(engine_on=True, obd_connected=True), at(32))

    assert decision.mode == ACTIVE
    assert decision.changed is True
    assert decision.publish_now is True
    assert decision.reason == "ignition_on"


def test_dtc_change_forces_a_frame_while_idle():
    machine = CollectionModeMachine(idle_after_seconds=30)
    settle(machine)

    decision = machine.update(parked(dtc_changed=True), at(33))

    assert decision.publish_now is True
    assert decision.reason == "dtc_changed"
    assert decision.mode == IDLE, "a DTC does not by itself wake the vehicle"


def test_connection_change_forces_a_frame_while_idle():
    machine = CollectionModeMachine(idle_after_seconds=30)
    settle(machine)

    decision = machine.update(parked(connection_changed=True), at(33))

    assert decision.publish_now is True
    assert decision.reason == "connection_changed"


def test_first_update_always_publishes():
    machine = CollectionModeMachine()

    assert machine.update(ModeInputs(engine_on=True, obd_connected=True), at(0)).publish_now is True


# --- idle requires positive evidence ----------------------------------------


@pytest.mark.parametrize(
    "inputs,why",
    [
        (parked(obd_connected=False), "a disconnected OBD link proves nothing"),
        (parked(engine_on=None), "unknown ignition is not ignition-off"),
        (parked(speed_kph=None), "unknown speed is not zero speed"),
    ],
)
def test_absent_data_never_becomes_idle(inputs, why):
    machine = CollectionModeMachine(idle_after_seconds=30)

    machine.update(inputs, at(0))
    decision = machine.update(inputs, at(120))

    assert decision.mode == ACTIVE, why
    assert decision.reason == "insufficient_evidence"


def test_quiet_timer_resets_on_any_activity():
    machine = CollectionModeMachine(idle_after_seconds=30)
    machine.update(parked(), at(0))
    machine.update(parked(speed_kph=50.0), at(20))  # interruption

    assert machine.update(parked(), at(45)).mode == ACTIVE, "the timer restarted at 20s"
    assert machine.update(parked(), at(76)).mode == IDLE


def test_rejects_unknown_initial_mode():
    with pytest.raises(ValueError):
        CollectionModeMachine(initial_mode="hibernating")


# --- snapshot projection ----------------------------------------------------


def build_snapshot(*, speed=None, engine_on=None, connected=True, samples=()):
    store = ObservationStore()
    store.update_obd_status(connected=connected, engine_on=engine_on)
    if speed is not None:
        store.update_obd_signal(
            "SPEED",
            {
                "value": speed,
                "unit": "km/h",
                **observation_meta(
                    observed_at="2026-03-01T12:00:00.500Z",
                    source="obd.pid",
                    quality="valid",
                    max_age_ms=2000,
                ),
            },
        )
    for index, (ax, gx) in enumerate(samples):
        store.append_imu_sample(
            ImuSample(
                observed_at=f"2026-03-01T12:00:00.{index * 50:03d}Z",
                ax=ax, ay=0.0, az=0.0, gx=gx, gy=0.0, gz=0.0,
            )
        )
    return store.snapshot("2026-03-01T12:00:00.000Z", "2026-03-01T12:00:01.000Z")


def test_snapshot_projection_reads_speed_engine_and_motion():
    snapshot = build_snapshot(
        speed=42.0, engine_on=True, samples=[(0.1, 0.01), (3.4, 0.02)]
    )

    inputs = inputs_from_snapshot(snapshot)

    assert inputs.speed_kph == 42.0
    assert inputs.engine_on is True
    assert inputs.obd_connected is True
    assert inputs.accel_magnitude_mps2 == pytest.approx(3.4), "peak, not mean"


def test_snapshot_projection_handles_absent_signals():
    inputs = inputs_from_snapshot(build_snapshot(connected=False))

    assert inputs.speed_kph is None
    assert inputs.engine_on is None
    assert inputs.accel_magnitude_mps2 is None
    assert inputs.obd_connected is False


# --- worker integration (EDGE-007 x MQTT-002) -------------------------------


def test_idle_worker_emits_a_wide_frame_with_no_imu_batch(tmp_path):
    """An idle frame spans the skipped gap and carries an explicit reason."""
    import json
    import threading
    from types import SimpleNamespace

    from car_telemetry.frame_builder import worker
    from car_telemetry.outbox import SqliteOutbox
    from car_telemetry.state import DeviceState
    from contracts.mqtt.v2 import validate_vehicle_frame

    start = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    ticks = iter(start + timedelta(seconds=n) for n in range(0, 4000))

    store = ObservationStore()
    store.update_obd_status(connected=True, engine_on=False)
    store.update_obd_signal(
        "SPEED",
        {
            "value": 0.0,
            "unit": "km/h",
            **observation_meta(
                observed_at="2026-03-01T12:00:00.500Z",
                source="obd.pid",
                quality="valid",
                max_age_ms=2000,
            ),
        },
    )
    store.update_device(
        {
            "temperatureC": 44,
            "softwareVersion": "3.0.0",
            **observation_meta(
                observed_at="2026-03-01T12:00:00.700Z",
                source="device.os",
                quality="valid",
                max_age_ms=10000,
            ),
        }
    )
    store.update_imu_status(
        {"sampleRateHz": 20, "orientation": "x-forward-y-left-z-up", "quality": "valid"}
    )

    settings = SimpleNamespace(
        device_id="DEV-001",
        outbox_file=str(tmp_path / "outbox.sqlite3"),
        outbox_max_bytes=256 * 1024 * 1024,
        outbox_max_age_seconds=86400,
    )
    state = DeviceState("DEV-001", "VEH-001", 3)
    stop = threading.Event()
    outbox = SqliteOutbox(settings.outbox_file)

    thread = threading.Thread(
        target=worker,
        args=(settings, state, store, stop),
        kwargs={"now": lambda: next(ticks), "outbox": outbox},
        daemon=True,
    )
    thread.start()
    # Active runs at 1 Hz, so idle only begins after ~30 simulated seconds.
    for _ in range(2000):
        if outbox.depth() >= 35:
            break
        threading.Event().wait(0.005)
    stop.set()
    thread.join(timeout=5)

    try:
        frames = [
            json.loads(item.payload.decode("utf-8")) for item in outbox.batch(50)
        ]
        assert frames, "the worker produced no frames"
        for frame in frames:
            validate_vehicle_frame(frame)

        idle_frames = [
            f for f in frames if f["payload"]["imu"].get("inactiveReason") == "vehicle_idle"
        ]
        assert idle_frames, "a parked vehicle must eventually emit idle frames"
        for idle in idle_frames:
            assert idle["payload"]["imu"]["samples"] == []
            assert idle["payload"]["imu"]["sampleCount"] == 0

        # The transition frame is emitted immediately and still covers one
        # second; the frames after it span the whole skipped gap.
        assert idle_frames[0]["payload"]["intervalMs"] == 1000
        assert any(f["payload"]["intervalMs"] > 1000 for f in idle_frames[1:]), (
            "steady-state idle frames must span the skipped seconds"
        )
        assert len(idle_frames) < len(frames), "idle emits fewer frames than active"

        assert state.snapshot()["frame"]["mode"] in {"idle", "active"}
    finally:
        outbox.close()
