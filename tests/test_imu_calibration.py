import json
from datetime import datetime, timezone

import pytest

from car_telemetry import imu
from car_telemetry.config import settings
from car_telemetry.imu_calibration import (
    CalibrationError,
    apply_calibration,
    build_calibration,
    load_calibration,
    save_calibration,
)
from car_telemetry.observations import ObservationStore
from car_telemetry.state import DeviceState
from dataclasses import replace


def stationary_samples(count=20):
    return [
        (0.12, -0.08, 9.90665, 0.01, -0.02, 0.03)
        for _ in range(count)
    ]


def test_calibration_identity_is_repeatable_and_corrects_bias():
    first = build_calibration(
        stationary_samples(),
        orientation="x-forward-y-left-z-up",
        created_at="2026-01-01T00:00:00Z",
    )
    second = build_calibration(
        stationary_samples(),
        orientation="x-forward-y-left-z-up",
        created_at="2026-02-01T00:00:00Z",
    )
    assert first.version == second.version

    acceleration, gyro = apply_calibration(
        (0.12, -0.08, 9.90665),
        (0.01, -0.02, 0.03),
        first,
    )
    assert acceleration == pytest.approx((0, 0, 9.80665))
    assert gyro == pytest.approx((0, 0, 0))


def test_orientation_is_applied_before_bias_calculation():
    calibration = build_calibration(
        [(0.08, 0.12, 9.90665, 0.02, 0.01, 0.03)] * 20,
        orientation="y-forward-x-right-z-up",
    )
    acceleration, _ = apply_calibration(
        (0.08, 1.12, 9.90665),
        (0.02, 0.01, 0.03),
        calibration,
    )
    assert acceleration == pytest.approx((1, 0, 9.80665))


def test_calibration_persists_and_reports_valid_stale_and_invalid(tmp_path):
    path = tmp_path / "imu-calibration.json"
    calibration = build_calibration(
        stationary_samples(),
        orientation="x-forward-y-left-z-up",
        created_at="2026-08-01T00:00:00Z",
    )
    save_calibration(path, calibration)

    loaded, state = load_calibration(
        path,
        orientation="x-forward-y-left-z-up",
        max_age_days=90,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert state == "valid"
    assert loaded == calibration

    loaded, state = load_calibration(
        path,
        orientation="x-forward-y-left-z-up",
        max_age_days=10,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert (loaded, state) == (None, "stale")

    loaded, state = load_calibration(
        path,
        orientation="y-forward-x-right-z-up",
        max_age_days=90,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert (loaded, state) == (None, "invalid")

    tampered = calibration.to_dict()
    tampered["accelerationBiasMps2"][0] = 99
    path.write_text(json.dumps(tampered), encoding="utf-8")
    assert load_calibration(
        path,
        orientation="x-forward-y-left-z-up",
        max_age_days=90,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    ) == (None, "invalid")

    path.write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
    assert load_calibration(
        path,
        orientation="x-forward-y-left-z-up",
        max_age_days=90,
    ) == (None, "invalid")


def test_calibration_rejects_motion_and_unsupported_orientation():
    moving = stationary_samples()
    moving[-1] = (4, 0, 9.8, 0, 0, 0)
    with pytest.raises(CalibrationError, match="moved"):
        build_calibration(moving, orientation="x-forward-y-left-z-up")
    with pytest.raises(CalibrationError, match="unsupported"):
        build_calibration(stationary_samples(), orientation="z-forward")
    rotating = stationary_samples()
    rotating[-1] = (0.12, -0.08, 9.90665, 0.5, 0, 0)
    with pytest.raises(CalibrationError, match="rotated"):
        build_calibration(rotating, orientation="x-forward-y-left-z-up")


def test_imu_worker_reuses_persisted_calibration_and_emits_identity(monkeypatch, tmp_path):
    calibration_path = tmp_path / "imu-calibration.json"
    calibration = build_calibration(
        stationary_samples(), orientation="x-forward-y-left-z-up"
    )
    save_calibration(calibration_path, calibration)

    class Sensor:
        acceleration = (0.12, -0.08, 9.90665)
        gyro = (0.01, -0.02, 0.03)
        temperature = 42.5

    class StopAfterOneSample:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    configured = replace(
        settings(),
        imu_enabled=True,
        imu_calibration_file=str(calibration_path),
        imu_orientation="x-forward-y-left-z-up",
    )
    monkeypatch.setattr(imu, "open_sensor", lambda _settings: Sensor())
    state = DeviceState("DEV-001", "VEH-001", 1)
    observations = ObservationStore()

    imu.worker(configured, state, observations, StopAfterOneSample())

    imu_state = state.snapshot()["imu"]
    assert imu_state["calibrationState"] == "valid"
    assert imu_state["calibrationVersion"] == calibration.version
    snapshot = observations.snapshot(
        "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"
    )
    assert snapshot.imu_status["calibrationVersion"] == calibration.version
    assert len(snapshot.imu_samples) == 1
