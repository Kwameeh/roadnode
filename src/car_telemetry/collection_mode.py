from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

ACTIVE = "active"
IDLE = "idle"

ACTIVE_INTERVAL_SECONDS = 1
IDLE_INTERVAL_SECONDS = 30

# A vehicle is only "confidently idle" after everything has been quiet for a
# sustained period. Any single active signal ends idle immediately.
DEFAULT_IDLE_AFTER_SECONDS = 30.0
MOVING_SPEED_KPH = 3.0
MOTION_ACCEL_MPS2 = 0.6
MOTION_GYRO_RAD_S = 0.15

IDLE_REASON = "vehicle_idle"


@dataclass(frozen=True)
class ModeDecision:
    mode: str
    changed: bool
    reason: str
    publish_now: bool

    @property
    def interval_seconds(self) -> int:
        return ACTIVE_INTERVAL_SECONDS if self.mode == ACTIVE else IDLE_INTERVAL_SECONDS

    @property
    def idle(self) -> bool:
        return self.mode == IDLE


@dataclass(frozen=True)
class ModeInputs:
    """Everything the machine is allowed to consider, all optional."""

    engine_on: bool | None = None
    speed_kph: float | None = None
    accel_magnitude_mps2: float | None = None
    gyro_magnitude_rad_s: float | None = None
    obd_connected: bool = False
    dtc_changed: bool = False
    connection_changed: bool = False


def _active_signal(inputs: ModeInputs) -> str | None:
    """Name the first signal proving the vehicle is not idle, if any."""
    if inputs.engine_on is True:
        return "ignition_on"
    if inputs.speed_kph is not None and inputs.speed_kph > MOVING_SPEED_KPH:
        return "vehicle_moving"
    if (
        inputs.accel_magnitude_mps2 is not None
        and inputs.accel_magnitude_mps2 > MOTION_ACCEL_MPS2
    ):
        return "motion_detected"
    if (
        inputs.gyro_magnitude_rad_s is not None
        and inputs.gyro_magnitude_rad_s > MOTION_GYRO_RAD_S
    ):
        return "rotation_detected"
    return None


def _idle_confident(inputs: ModeInputs) -> bool:
    """Idle requires positive evidence, never merely absent data.

    Without a connected OBD link reporting ignition off, the device cannot
    distinguish a parked car from a failed sensor, so it stays active.
    """
    if not inputs.obd_connected or inputs.engine_on is not False:
        return False
    if inputs.speed_kph is None or inputs.speed_kph > MOVING_SPEED_KPH:
        return False
    return True


class CollectionModeMachine:
    """Decides the active/idle collection mode and when a frame is due.

    Transitions are always reported and always publish an immediate frame, so
    reducing the idle cadence can never hide a state change.
    """

    def __init__(
        self,
        *,
        idle_after_seconds: float = DEFAULT_IDLE_AFTER_SECONDS,
        initial_mode: str = ACTIVE,
    ):
        if initial_mode not in {ACTIVE, IDLE}:
            raise ValueError(f"unsupported collection mode: {initial_mode}")
        self._idle_after = idle_after_seconds
        self._mode = initial_mode
        self._quiet_since: datetime | None = None
        self._last_published: datetime | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def update(self, inputs: ModeInputs, now: datetime) -> ModeDecision:
        previous = self._mode
        signal = _active_signal(inputs)

        if signal is not None:
            self._mode = ACTIVE
            self._quiet_since = None
            reason = signal
        elif _idle_confident(inputs):
            if self._quiet_since is None:
                self._quiet_since = now
            quiet_for = (now - self._quiet_since).total_seconds()
            if quiet_for >= self._idle_after:
                self._mode = IDLE
                reason = IDLE_REASON
            else:
                self._mode = ACTIVE
                reason = "settling"
        else:
            # Not provably idle: stay active rather than risk losing data.
            self._mode = ACTIVE
            self._quiet_since = None
            reason = "insufficient_evidence"

        changed = self._mode != previous
        forced = inputs.dtc_changed or inputs.connection_changed
        if forced:
            reason = "dtc_changed" if inputs.dtc_changed else "connection_changed"

        due = self._due(now)
        publish_now = changed or forced or due
        if publish_now:
            self._last_published = now

        return ModeDecision(
            mode=self._mode,
            changed=changed,
            reason=reason,
            publish_now=publish_now,
        )

    def _due(self, now: datetime) -> bool:
        if self._last_published is None:
            return True
        interval = (
            ACTIVE_INTERVAL_SECONDS if self._mode == ACTIVE else IDLE_INTERVAL_SECONDS
        )
        return now - self._last_published >= timedelta(seconds=interval)


def inputs_from_snapshot(
    snapshot: Any,
    *,
    dtc_changed: bool = False,
    connection_changed: bool = False,
) -> ModeInputs:
    """Project an ObservationSnapshot onto the mode machine's inputs."""
    speed = None
    signals = snapshot.obd.get("signals", {}) if snapshot.obd else {}
    speed_signal = signals.get("SPEED")
    if isinstance(speed_signal, dict):
        value = speed_signal.get("value")
        if isinstance(value, (int, float)):
            speed = float(value)

    accel = gyro = None
    if snapshot.imu_samples:
        accel = max(
            (sample.ax**2 + sample.ay**2 + sample.az**2) ** 0.5
            for sample in snapshot.imu_samples
        )
        gyro = max(
            (sample.gx**2 + sample.gy**2 + sample.gz**2) ** 0.5
            for sample in snapshot.imu_samples
        )

    return ModeInputs(
        engine_on=snapshot.obd.get("engineOn") if snapshot.obd else None,
        speed_kph=speed,
        accel_magnitude_mps2=accel,
        gyro_magnitude_rad_s=gyro,
        obd_connected=bool(snapshot.obd.get("connected")) if snapshot.obd else False,
        dtc_changed=dtc_changed,
        connection_changed=connection_changed,
    )
