from __future__ import annotations

import math
import threading
import time

from .config import Settings
from .imu_calibration import (
    Calibration,
    apply_calibration,
    build_calibration,
    load_calibration,
    save_calibration,
)
from .observations import IMU_MAX_AGE_MS, ImuSample, ObservationWriter, utc_now
from .state import DeviceState


def open_sensor(settings: Settings):
    import adafruit_mpu6050
    import board

    return adafruit_mpu6050.MPU6050(board.I2C(), address=settings.imu_address)


def _calibrate(sensor, settings: Settings, state: DeviceState) -> Calibration:
    rows = []
    state.merge(
        "imu",
        {
            "calibrating": True,
            "calibrated": False,
            "calibrationState": "running",
            "calibrationPercent": 0,
        },
    )
    for index in range(settings.imu_calibration_samples):
        acceleration = sensor.acceleration
        gyro = sensor.gyro
        rows.append((*acceleration, *gyro))
        if index % 10 == 0 or index == settings.imu_calibration_samples - 1:
            state.merge(
                "imu",
                {
                    "calibrationPercent": int(
                        (index + 1) * 100 / settings.imu_calibration_samples
                    )
                },
            )
        time.sleep(0.02)
    calibration = build_calibration(rows, orientation=settings.imu_orientation)
    save_calibration(settings.imu_calibration_file, calibration)
    return calibration


def worker(
    settings: Settings,
    state: DeviceState,
    observations: ObservationWriter,
    stop: threading.Event,
) -> None:
    state.merge(
        "imu",
        {
            "enabled": settings.imu_enabled,
            "address": f"0x{settings.imu_address:02X}",
            "orientation": settings.imu_orientation,
        },
    )
    if not settings.imu_enabled:
        observations.update_imu_status(
            {"quality": "unknown", "inactiveReason": "sensor_disabled"}
        )
        return

    try:
        sensor = open_sensor(settings)
        calibration, calibration_state = load_calibration(
            settings.imu_calibration_file,
            orientation=settings.imu_orientation,
            max_age_days=settings.imu_calibration_max_age_days,
        )
        state.merge("imu", {"calibrationState": calibration_state})
    except Exception as exc:
        state.merge(
            "imu",
            {
                "calibrating": False,
                "calibrated": False,
                "calibrationState": "invalid",
                "error": str(exc),
            },
        )
        observations.update_imu_status(
            {
                "calibrationVersion": None,
                "orientation": settings.imu_orientation,
                "quality": "invalid",
                "inactiveReason": "sensor_unavailable",
            }
        )
        return

    # A Pi may be bumped while its services start. Failed stationary
    # calibration must not disable the IMU until the next reboot: keep retrying
    # after a short settling period until the vehicle is still or we shut down.
    retry_count = 0
    while calibration is None and not stop.is_set():
        state.merge(
            "imu",
            {
                "calibrating": False,
                "calibrated": False,
                "calibrationState": calibration_state,
                "calibrationRetryCount": retry_count,
                "calibrationRetryInSeconds": settings.imu_calibration_settle_seconds,
            },
        )
        if stop.wait(settings.imu_calibration_settle_seconds):
            return
        try:
            calibration = _calibrate(sensor, settings, state)
            calibration_state = "valid"
        except Exception as exc:
            retry_count += 1
            calibration_state = "invalid"
            state.merge(
                "imu",
                {
                    "calibrating": False,
                    "calibrated": False,
                    "calibrationState": "invalid",
                    "calibrationRetryCount": retry_count,
                    "calibrationRetryInSeconds": settings.imu_calibration_retry_seconds,
                    "error": str(exc),
                },
            )
            observations.update_imu_status(
                {
                    "calibrationVersion": None,
                    "orientation": settings.imu_orientation,
                    "quality": "invalid",
                    "inactiveReason": "calibration_invalid",
                }
            )
            if stop.wait(settings.imu_calibration_retry_seconds):
                return

    if calibration is None:
        return

    try:
        state.merge(
            "imu",
            {
                "calibrating": False,
                "calibrated": True,
                "calibrationState": calibration_state,
                "calibrationVersion": calibration.version,
                "calibrationCreatedAt": calibration.created_at,
                "calibrationRetryCount": retry_count,
                "calibrationRetryInSeconds": None,
                "error": None,
            },
        )
        observations.update_imu_status(
            {
                "sampleRateHz": settings.imu_rate_hz,
                "calibrationVersion": calibration.version,
                "orientation": calibration.orientation,
                "source": "imu.mpu6050",
                "quality": "valid",
                "maxAgeMs": IMU_MAX_AGE_MS,
                "inactiveReason": None,
            }
        )
    except Exception as exc:
        state.merge(
            "imu",
            {
                "calibrating": False,
                "calibrated": False,
                "calibrationState": "invalid",
                "error": str(exc),
            },
        )
        observations.update_imu_status(
            {
                "calibrationVersion": None,
                "orientation": settings.imu_orientation,
                "quality": "invalid",
                "inactiveReason": "calibration_invalid",
            }
        )
        return

    interval = 1 / max(settings.imu_rate_hz, 1)
    while not stop.is_set():
        try:
            observed_at = utc_now()
            acceleration = tuple(float(value) for value in sensor.acceleration)
            gyro = tuple(float(value) for value in sensor.gyro)
            calibrated_accel, calibrated_gyro = apply_calibration(
                acceleration, gyro, calibration
            )
            ax, ay, az = calibrated_accel
            gx, gy, gz = calibrated_gyro
            resultant_g = math.sqrt(ax * ax + ay * ay + az * az) / 9.80665
            state.merge(
                "imu",
                {
                    "linearAccelerationMps2": {
                        "x": round(ax, 3),
                        "y": round(ay, 3),
                        "z": round(az, 3),
                    },
                    "gyroRadPerSec": {
                        "x": round(gx, 3),
                        "y": round(gy, 3),
                        "z": round(gz, 3),
                    },
                    "resultantG": round(resultant_g, 3),
                    "temperatureC": round(float(sensor.temperature), 2),
                    "observedAt": observed_at,
                    "source": "imu.mpu6050",
                    "quality": "valid",
                    "maxAgeMs": IMU_MAX_AGE_MS,
                    "error": None,
                },
            )
            observations.append_imu_sample(
                ImuSample(
                    observed_at=observed_at,
                    ax=round(ax, 6),
                    ay=round(ay, 6),
                    az=round(az, 6),
                    gx=round(gx, 6),
                    gy=round(gy, 6),
                    gz=round(gz, 6),
                )
            )
            state.merge(
                "events",
                {
                    "harshAcceleration": ax >= settings.harsh_accel_mps2,
                    "harshBraking": ax <= settings.harsh_brake_mps2,
                    "harshCornering": abs(ay) >= settings.harsh_corner_mps2,
                    "possibleImpact": resultant_g >= settings.impact_g,
                },
            )
        except Exception as exc:
            state.merge("imu", {"quality": "invalid", "error": str(exc)})
            observations.update_imu_status(
                {"quality": "invalid", "inactiveReason": "sensor_read_error"}
            )
        stop.wait(interval)
