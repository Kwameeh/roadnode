from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from dataclasses import replace as _replace

from .collection_mode import CollectionModeMachine, inputs_from_snapshot
from .config import Settings
from .observations import ObservationReader, ObservationSnapshot, parse_utc, utc_iso
from .outbox import PRIORITY_ROUTINE, SqliteOutbox, serialize_frame
from .state import DeviceState


def frame_topic(device_id: str) -> str:
    return f"roadnode/v2/devices/{device_id}/frame"


@dataclass(frozen=True)
class FrameContext:
    device_id: str
    boot_id: str
    sequence: int
    captured_from: str
    captured_to: str
    sent_at: str
    clock_source: str
    clock_quality: str
    clock_offset_ms: float | None
    replay: bool = False
    dropped_messages: int = 0
    dropped_imu_samples: int = 0
    first_missing_captured_at: str | None = None
    last_missing_captured_at: str | None = None


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


class VehicleFrameBuilder:
    """Pure projection from normalized observations to a v2 vehicle frame."""

    def build(
        self,
        context: FrameContext,
        observations: ObservationSnapshot,
    ) -> dict[str, Any]:
        captured_from = parse_utc(context.captured_from)
        captured_to = parse_utc(context.captured_to)
        interval_ms = round((captured_to - captured_from).total_seconds() * 1000)
        if interval_ms <= 0:
            raise ValueError("frame interval must be positive")

        telemetry: dict[str, Any] = {}
        if observations.gps is not None:
            telemetry["gps"] = _without_none(observations.gps)
        telemetry["obd"] = {
            "connected": observations.obd["connected"],
            "engineOn": observations.obd["engineOn"],
            "signals": {
                name: _without_none(signal)
                for name, signal in observations.obd.get("signals", {}).items()
            },
        }
        if observations.device is None:
            raise ValueError("device health observation is required to build a frame")
        telemetry["device"] = _without_none(observations.device)

        samples = []
        for sample in observations.imu_samples:
            offset_ms = round(
                (parse_utc(sample.observed_at) - captured_from).total_seconds() * 1000,
                3,
            )
            samples.append(
                [offset_ms, sample.ax, sample.ay, sample.az, sample.gx, sample.gy, sample.gz]
            )
        samples.sort(key=lambda row: row[0])

        imu_status = observations.imu_status
        imu = {
            "sampleRateHz": imu_status["sampleRateHz"],
            "sampleCount": len(samples),
            "calibrationVersion": imu_status.get("calibrationVersion"),
            "orientation": imu_status["orientation"],
            "fields": ["offsetMs", "ax", "ay", "az", "gx", "gy", "gz"],
            "units": {"acceleration": "m/s2", "angularVelocity": "rad/s"},
            "samples": samples,
            "observedAt": (
                max(
                    observations.imu_samples,
                    key=lambda sample: parse_utc(sample.observed_at),
                ).observed_at
                if samples
                else context.captured_to
            ),
            "source": imu_status.get("source", "imu.mpu6050"),
            "quality": imu_status.get("quality", "unknown"),
            "maxAgeMs": imu_status.get("maxAgeMs", 1000),
        }
        if not samples:
            imu["inactiveReason"] = imu_status.get("inactiveReason") or "no_samples"

        dropped = {
            "messages": context.dropped_messages,
            "imuSamples": context.dropped_imu_samples,
        }
        if context.dropped_messages or context.dropped_imu_samples:
            if not context.first_missing_captured_at or not context.last_missing_captured_at:
                raise ValueError("drop accounting requires first and last missing timestamps")
            dropped["firstMissingCapturedAt"] = context.first_missing_captured_at
            dropped["lastMissingCapturedAt"] = context.last_missing_captured_at

        return {
            "schemaVersion": 2,
            "messageId": f"{context.device_id}:{context.boot_id}:{context.sequence}",
            "messageType": "vehicle_frame",
            "deviceId": context.device_id,
            "bootId": context.boot_id,
            "sequence": context.sequence,
            "capturedAt": context.captured_to,
            "sentAt": context.sent_at,
            "clock": {
                "source": context.clock_source,
                "quality": context.clock_quality,
                "offsetMs": context.clock_offset_ms,
            },
            "replay": context.replay,
            "dropped": dropped,
            "payload": {
                "capturedFrom": context.captured_from,
                "capturedTo": context.captured_to,
                "intervalMs": interval_ms,
                "telemetry": telemetry,
                "imu": imu,
            },
        }


def worker(
    settings: Settings,
    state: DeviceState,
    observations: ObservationReader,
    stop: threading.Event,
    *,
    now: Callable[[], datetime] | None = None,
    outbox: SqliteOutbox | None = None,
) -> None:
    clock = now or (lambda: datetime.now(timezone.utc))
    boot_id = str(uuid.uuid4())
    sequence = 0
    builder = VehicleFrameBuilder()
    queue = outbox if outbox is not None else SqliteOutbox(settings.outbox_file)
    owns_queue = outbox is None
    topic = frame_topic(settings.device_id)
    modes = CollectionModeMachine()
    state.merge(
        "frame",
        {"enabled": True, "bootId": boot_id, "built": 0, "mode": modes.mode},
    )

    current = clock().astimezone(timezone.utc)
    next_boundary = current.replace(microsecond=0) + timedelta(seconds=1)
    previous_boundary = next_boundary - timedelta(seconds=1)
    while not stop.is_set():
        wait_seconds = max(
            0.0,
            (next_boundary - clock().astimezone(timezone.utc)).total_seconds(),
        )
        if stop.wait(wait_seconds):
            break
        captured_from = utc_iso(previous_boundary)
        captured_to = utc_iso(next_boundary)
        try:
            snapshot = observations.snapshot(captured_from, captured_to)

            # EDGE-007: idle reduces cadence, but a transition always emits.
            decision = modes.update(
                inputs_from_snapshot(snapshot), next_boundary
            )
            if not decision.publish_now:
                # Hold previous_boundary so the next frame spans the whole
                # skipped gap rather than losing the intervening seconds.
                next_boundary += timedelta(seconds=1)
                continue
            if decision.idle:
                # An idle frame carries no IMU batch, only an explicit reason.
                idle_status = dict(snapshot.imu_status)
                idle_status["inactiveReason"] = decision.reason
                snapshot = _replace(
                    snapshot, imu_samples=(), imu_status=idle_status
                )

            gps_quality = snapshot.gps and snapshot.gps.get("quality")
            sequence += 1
            # Cumulative eviction accounting travels on the next frame so the
            # server learns which capture range it will never receive.
            dropped = queue.stats()
            frame = builder.build(
                FrameContext(
                    device_id=settings.device_id,
                    boot_id=boot_id,
                    sequence=sequence,
                    captured_from=captured_from,
                    captured_to=captured_to,
                    sent_at=utc_iso(clock().astimezone(timezone.utc)),
                    clock_source="gps" if gps_quality == "valid" else "system",
                    clock_quality="locked" if gps_quality == "valid" else "estimated",
                    clock_offset_ms=None,
                    dropped_messages=dropped.dropped_messages,
                    dropped_imu_samples=dropped.dropped_imu_samples,
                    first_missing_captured_at=dropped.first_missing_captured_at,
                    last_missing_captured_at=dropped.last_missing_captured_at,
                ),
                snapshot,
            )
            queue.put(
                message_id=frame["messageId"],
                topic=topic,
                payload=serialize_frame(frame),
                captured_at=frame["capturedAt"],
                qos=1,
                retain=False,
                priority=PRIORITY_ROUTINE,
            )
            queue.evict(
                max_bytes=settings.outbox_max_bytes,
                max_age_seconds=settings.outbox_max_age_seconds,
                now=clock().astimezone(timezone.utc),
            )
            stats = queue.stats()
            state.merge(
                "frame",
                {
                    "built": sequence,
                    "lastBuiltAt": frame["sentAt"],
                    "lastMessageId": frame["messageId"],
                    "lastFrame": frame,
                    "mode": decision.mode,
                    "modeReason": decision.reason,
                    "modeChanged": decision.changed,
                    "queueDepth": stats.depth,
                    "queueBytes": stats.bytes_used,
                    "droppedMessages": stats.dropped_messages,
                    "error": None,
                },
            )
        except Exception as exc:
            state.merge("frame", {"error": str(exc)})
        previous_boundary = next_boundary
        next_boundary += timedelta(seconds=1)

    if owns_queue:
        queue.close()
