from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .common import write_json_atomic
from .observations import parse_utc, utc_now

GRAVITY_MPS2 = 9.80665
CALIBRATION_SCHEMA_VERSION = 1
SUPPORTED_ORIENTATIONS = {
    "x-forward-y-left-z-up": ((0, 1), (1, 1), (2, 1)),
    "x-backward-y-right-z-up": ((0, -1), (1, -1), (2, 1)),
    "y-forward-x-right-z-up": ((1, 1), (0, -1), (2, 1)),
    "y-backward-x-left-z-up": ((1, -1), (0, 1), (2, 1)),
}


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class Calibration:
    version: str
    created_at: str
    orientation: str
    acceleration_bias: tuple[float, float, float]
    gyro_bias: tuple[float, float, float]
    sample_count: int

    def to_dict(self) -> dict:
        return {
            "schemaVersion": CALIBRATION_SCHEMA_VERSION,
            "calibrationVersion": self.version,
            "createdAt": self.created_at,
            "orientation": self.orientation,
            "accelerationBiasMps2": list(self.acceleration_bias),
            "gyroBiasRadPerSec": list(self.gyro_bias),
            "sampleCount": self.sample_count,
        }


def _calibration_version(
    *,
    orientation: str,
    acceleration_bias: tuple[float, float, float],
    gyro_bias: tuple[float, float, float],
    sample_count: int,
) -> str:
    identity_input = json.dumps(
        {
            "schemaVersion": CALIBRATION_SCHEMA_VERSION,
            "orientation": orientation,
            "accelerationBiasMps2": [round(value, 6) for value in acceleration_bias],
            "gyroBiasRadPerSec": [round(value, 6) for value in gyro_bias],
            "sampleCount": sample_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"imu-cal-v1-{hashlib.sha256(identity_input.encode()).hexdigest()[:12]}"


def orient_vector(
    values: tuple[float, float, float],
    orientation: str,
) -> tuple[float, float, float]:
    try:
        mapping = SUPPORTED_ORIENTATIONS[orientation]
    except KeyError as exc:
        raise CalibrationError(f"unsupported IMU orientation: {orientation}") from exc
    return tuple(values[index] * sign for index, sign in mapping)


def apply_calibration(
    acceleration: tuple[float, float, float],
    gyro: tuple[float, float, float],
    calibration: Calibration,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    oriented_accel = orient_vector(acceleration, calibration.orientation)
    oriented_gyro = orient_vector(gyro, calibration.orientation)
    linear = tuple(
        value - bias for value, bias in zip(oriented_accel, calibration.acceleration_bias)
    )
    corrected_gyro = tuple(
        value - bias for value, bias in zip(oriented_gyro, calibration.gyro_bias)
    )
    return linear, corrected_gyro


def build_calibration(
    samples: Iterable[tuple[float, float, float, float, float, float]],
    *,
    orientation: str,
    created_at: str | None = None,
) -> Calibration:
    rows = [tuple(float(value) for value in row) for row in samples]
    if not rows:
        raise CalibrationError("calibration requires samples")
    if orientation not in SUPPORTED_ORIENTATIONS:
        raise CalibrationError(f"unsupported IMU orientation: {orientation}")

    oriented_accel = [orient_vector(row[:3], orientation) for row in rows]
    oriented_gyro = [orient_vector(row[3:], orientation) for row in rows]
    accel_means = tuple(
        sum(row[index] for row in oriented_accel) / len(rows) for index in range(3)
    )
    gyro_means = tuple(
        sum(row[index] for row in oriented_gyro) / len(rows) for index in range(3)
    )
    gravity = math.sqrt(sum(value * value for value in accel_means))
    if not 8.0 <= gravity <= 11.5:
        raise CalibrationError("device must be stationary with gravity near 1 g")

    max_deviation = max(
        math.sqrt(sum((row[index] - accel_means[index]) ** 2 for index in range(3)))
        for row in oriented_accel
    )
    if max_deviation > 0.75:
        raise CalibrationError("device moved during calibration")
    max_gyro = max(
        math.sqrt(sum(value * value for value in row)) for row in oriented_gyro
    )
    if max_gyro > 0.15:
        raise CalibrationError("device rotated during calibration")

    acceleration_bias = (
        accel_means[0],
        accel_means[1],
        accel_means[2] - GRAVITY_MPS2,
    )
    version = _calibration_version(
        orientation=orientation,
        acceleration_bias=acceleration_bias,
        gyro_bias=gyro_means,
        sample_count=len(rows),
    )
    return Calibration(
        version=version,
        created_at=created_at or utc_now(),
        orientation=orientation,
        acceleration_bias=acceleration_bias,
        gyro_bias=gyro_means,
        sample_count=len(rows),
    )


def save_calibration(path: str | Path, calibration: Calibration) -> None:
    write_json_atomic(str(Path(path).expanduser()), calibration.to_dict())


def load_calibration(
    path: str | Path,
    *,
    orientation: str,
    max_age_days: int,
    now: datetime | None = None,
) -> tuple[Calibration | None, str]:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return None, "missing"
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        if raw.get("schemaVersion") != CALIBRATION_SCHEMA_VERSION:
            return None, "invalid"
        calibration = Calibration(
            version=str(raw["calibrationVersion"]),
            created_at=str(raw["createdAt"]),
            orientation=str(raw["orientation"]),
            acceleration_bias=tuple(float(value) for value in raw["accelerationBiasMps2"]),
            gyro_bias=tuple(float(value) for value in raw["gyroBiasRadPerSec"]),
            sample_count=int(raw["sampleCount"]),
        )
        if len(calibration.acceleration_bias) != 3 or len(calibration.gyro_bias) != 3:
            return None, "invalid"
        if calibration.sample_count <= 0:
            return None, "invalid"
        if not all(
            math.isfinite(value)
            for value in (*calibration.acceleration_bias, *calibration.gyro_bias)
        ):
            return None, "invalid"
        if calibration.orientation != orientation:
            return None, "invalid"
        expected_version = _calibration_version(
            orientation=calibration.orientation,
            acceleration_bias=calibration.acceleration_bias,
            gyro_bias=calibration.gyro_bias,
            sample_count=calibration.sample_count,
        )
        if calibration.version != expected_version:
            return None, "invalid"
        created = parse_utc(calibration.created_at)
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if created - current > timedelta(minutes=5):
            return None, "invalid"
        if current - created > timedelta(days=max(0, max_age_days)):
            return None, "stale"
        return calibration, "valid"
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid"
