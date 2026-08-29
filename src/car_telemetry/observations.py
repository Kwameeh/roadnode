from __future__ import annotations

import copy
import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

GPS_MAX_AGE_MS = 3_000
DEVICE_MAX_AGE_MS = 10_000
IMU_MAX_AGE_MS = 1_000
FAST_OBD_MAX_AGE_MS = 2_000
SLOW_OBD_MAX_AGE_MS = 10_000

FAST_OBD_SIGNALS = {"RPM", "SPEED"}
CANONICAL_OBD_UNITS = {
    "RPM": "rpm",
    "SPEED": "km/h",
    "COOLANT_TEMP": "degC",
    "INTAKE_TEMP": "degC",
    "OIL_TEMP": "degC",
    "ENGINE_LOAD": "%",
    "THROTTLE_POS": "%",
    "FUEL_LEVEL": "%",
    "CONTROL_MODULE_VOLTAGE": "V",
    "MAF": "g/s",
    "ODOMETER": "km",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("observation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def observation_meta(
    *,
    observed_at: str,
    source: str,
    quality: str,
    max_age_ms: int,
) -> dict[str, Any]:
    if quality not in {"valid", "estimated", "stale", "invalid", "unknown"}:
        raise ValueError(f"unsupported observation quality: {quality}")
    if not source.strip():
        raise ValueError("observation source must not be empty")
    if max_age_ms < 0:
        raise ValueError("max_age_ms must not be negative")
    parse_utc(observed_at)
    return {
        "observedAt": observed_at,
        "source": source,
        "quality": quality,
        "maxAgeMs": int(max_age_ms),
    }


def normalize_obd_value(name: str, value: Any) -> tuple[Any, str]:
    unit = CANONICAL_OBD_UNITS.get(name)
    if hasattr(value, "magnitude"):
        quantity = value
        if unit and hasattr(quantity, "to"):
            try:
                quantity = quantity.to(unit)
            except Exception:
                pass
        magnitude = quantity.magnitude
        try:
            normalized_value: Any = float(magnitude)
        except (TypeError, ValueError):
            normalized_value = str(magnitude)
        if isinstance(normalized_value, float) and not math.isfinite(normalized_value):
            raise ValueError(f"{name} measurement must be finite")
        normalized_unit = unit or str(getattr(quantity, "units", "1"))
        return normalized_value, normalized_unit
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} measurement must be finite")
        return value, unit or "1"
    return str(value), unit or "1"


class ObservationReader(Protocol):
    def snapshot(self, captured_from: str, captured_to: str) -> "ObservationSnapshot": ...


class ObservationWriter(Protocol):
    def update_gps(self, observation: dict[str, Any]) -> None: ...

    def update_obd_status(self, *, connected: bool, engine_on: bool | None) -> None: ...

    def update_obd_signal(self, name: str, observation: dict[str, Any]) -> None: ...

    def update_device(self, observation: dict[str, Any]) -> None: ...

    def update_imu_status(self, status: dict[str, Any]) -> None: ...

    def append_imu_sample(self, sample: "ImuSample") -> None: ...


@dataclass(frozen=True)
class ImuSample:
    observed_at: str
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


@dataclass(frozen=True)
class ObservationSnapshot:
    gps: dict[str, Any] | None
    obd: dict[str, Any]
    device: dict[str, Any] | None
    imu_status: dict[str, Any]
    imu_samples: tuple[ImuSample, ...]


class ObservationStore:
    """Thread-safe normalized state, independent of local UI and transport."""

    def __init__(self, *, imu_buffer_size: int = 2_600):
        self._lock = threading.RLock()
        self._gps: dict[str, Any] | None = None
        self._obd: dict[str, Any] = {
            "connected": False,
            "engineOn": None,
            "signals": {},
        }
        self._device: dict[str, Any] | None = None
        self._imu_status: dict[str, Any] = {
            "sampleRateHz": 20,
            "calibrationVersion": None,
            "orientation": "x-forward-y-left-z-up",
            "source": "imu.mpu6050",
            "quality": "unknown",
            "maxAgeMs": IMU_MAX_AGE_MS,
            "inactiveReason": "calibration_missing",
        }
        self._imu_samples: deque[ImuSample] = deque(maxlen=max(20, imu_buffer_size))

    def update_gps(self, observation: dict[str, Any]) -> None:
        with self._lock:
            self._gps = copy.deepcopy(observation)

    def update_obd_status(self, *, connected: bool, engine_on: bool | None) -> None:
        with self._lock:
            self._obd["connected"] = bool(connected)
            self._obd["engineOn"] = engine_on

    def update_obd_signal(self, name: str, observation: dict[str, Any]) -> None:
        with self._lock:
            self._obd["signals"][name] = copy.deepcopy(observation)

    def update_device(self, observation: dict[str, Any]) -> None:
        with self._lock:
            self._device = copy.deepcopy(observation)

    def update_imu_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            self._imu_status.update(copy.deepcopy(status))

    def append_imu_sample(self, sample: ImuSample) -> None:
        parse_utc(sample.observed_at)
        with self._lock:
            self._imu_samples.append(sample)

    def snapshot(self, captured_from: str, captured_to: str) -> ObservationSnapshot:
        start = parse_utc(captured_from)
        end = parse_utc(captured_to)
        if start >= end:
            raise ValueError("captured_from must be earlier than captured_to")
        with self._lock:
            gps = self._gps
            if gps is not None and parse_utc(gps["observedAt"]) > end:
                gps = None
            device = self._device
            if device is not None and parse_utc(device["observedAt"]) > end:
                device = None
            obd = copy.deepcopy(self._obd)
            obd["signals"] = {
                name: signal
                for name, signal in obd["signals"].items()
                if parse_utc(signal["observedAt"]) <= end
            }
            samples = tuple(
                sample
                for sample in self._imu_samples
                if start <= parse_utc(sample.observed_at) < end
            )
            return ObservationSnapshot(
                gps=copy.deepcopy(gps),
                obd=obd,
                device=copy.deepcopy(device),
                imu_status=copy.deepcopy(self._imu_status),
                imu_samples=samples,
            )
