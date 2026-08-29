"""Payload, outage, reconnect-storm, and data-cost benchmarks (MQTT-008),
plus edge resource measurement (EDGE-008).

These answer the questions a pilot assumption rests on: how large is a frame on
the wire, how much mobile data does a vehicle consume per day, how long does
catch-up take after an outage, and does a Pi Zero 2 W keep up. Every function
is pure arithmetic over measured inputs, so the numbers can be recomputed and
argued with rather than being one-off notes from a spreadsheet.
"""

from __future__ import annotations

import gzip
import json
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Ghana pilot assumption; override for other tariffs.
DEFAULT_DATA_COST_PER_MB = 0.05
SECONDS_PER_DAY = 86_400

# Documented Pi Zero 2 W limits.
PI_ZERO_CPU_BUDGET = 0.60
PI_ZERO_MEMORY_BUDGET_MB = 200.0
PI_ZERO_MAX_TEMP_C = 75.0


@dataclass(frozen=True)
class PayloadProfile:
    frame_bytes: int
    compressed_bytes: int
    imu_sample_count: int

    @property
    def compression_ratio(self) -> float:
        if self.frame_bytes == 0:
            return 0.0
        return round(self.compressed_bytes / self.frame_bytes, 4)

    @property
    def bytes_per_imu_sample(self) -> float:
        if self.imu_sample_count == 0:
            return 0.0
        return round(self.frame_bytes / self.imu_sample_count, 2)


def measure_payload(frame: dict[str, Any]) -> PayloadProfile:
    """Wire size of one frame, as published and as it would compress."""
    encoded = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    samples = frame.get("payload", {}).get("imu", {}).get("samples", [])
    return PayloadProfile(
        frame_bytes=len(encoded),
        compressed_bytes=len(gzip.compress(encoded, compresslevel=6)),
        imu_sample_count=len(samples) if isinstance(samples, list) else 0,
    )


@dataclass(frozen=True)
class DataCostProjection:
    bytes_per_day: int
    megabytes_per_day: float
    megabytes_per_month: float
    cost_per_month: float
    active_seconds_per_day: int


def project_data_cost(
    profile: PayloadProfile,
    *,
    active_hours_per_day: float,
    mqtt_overhead_bytes: int = 60,
    cost_per_mb: float = DEFAULT_DATA_COST_PER_MB,
) -> DataCostProjection:
    """Monthly mobile-data cost for one vehicle.

    MQTT and TLS framing are counted: at one message per second the per-message
    overhead is a meaningful fraction of the bill, not a rounding error.
    """
    if active_hours_per_day < 0 or active_hours_per_day > 24:
        raise ValueError("active_hours_per_day must be within 0..24")
    active_seconds = int(active_hours_per_day * 3_600)
    per_message = profile.frame_bytes + mqtt_overhead_bytes
    bytes_per_day = per_message * active_seconds
    mb_per_day = bytes_per_day / 1_048_576
    mb_per_month = mb_per_day * 30
    return DataCostProjection(
        bytes_per_day=bytes_per_day,
        megabytes_per_day=round(mb_per_day, 3),
        megabytes_per_month=round(mb_per_month, 2),
        cost_per_month=round(mb_per_month * cost_per_mb, 2),
        active_seconds_per_day=active_seconds,
    )


@dataclass(frozen=True)
class OutageProjection:
    queued_messages: int
    queued_bytes: int
    exceeds_size_bound: bool
    exceeds_age_bound: bool
    catch_up_seconds: float
    """Wall-clock seconds to drain the backlog once connectivity returns."""


def project_outage(
    profile: PayloadProfile,
    *,
    outage_seconds: int,
    max_bytes: int,
    max_age_seconds: int,
    drain_messages_per_second: float,
) -> OutageProjection:
    """What an outage of this length costs, and how long recovery takes.

    Catch-up must be faster than real time or the device never recovers: it
    would still be draining yesterday's backlog while today's frames arrive.
    """
    if drain_messages_per_second <= 0:
        raise ValueError("drain rate must be positive")
    queued = outage_seconds
    queued_bytes = queued * profile.frame_bytes
    return OutageProjection(
        queued_messages=queued,
        queued_bytes=queued_bytes,
        exceeds_size_bound=queued_bytes > max_bytes,
        exceeds_age_bound=outage_seconds > max_age_seconds,
        catch_up_seconds=round(queued / drain_messages_per_second, 2),
    )


def catch_up_is_viable(projection: OutageProjection, outage_seconds: int) -> bool:
    """Recovery must outpace live production, or the backlog never clears."""
    return projection.catch_up_seconds < outage_seconds


@dataclass(frozen=True)
class ReconnectStormResult:
    devices: int
    peak_connections_per_second: float
    total_backlog_messages: int
    estimated_drain_seconds: float
    within_broker_capacity: bool


def project_reconnect_storm(
    *,
    devices: int,
    outage_seconds: int,
    broker_max_connections: int,
    broker_messages_per_second: float,
    reconnect_jitter_seconds: float,
) -> ReconnectStormResult:
    """A whole fleet reconnecting at once after a regional outage.

    Jitter is what keeps this survivable: without it every device reconnects in
    the same second and the broker refuses most of them.
    """
    if reconnect_jitter_seconds <= 0:
        raise ValueError("jitter must be positive; a synchronised storm is not survivable")
    peak = devices / reconnect_jitter_seconds
    backlog = devices * outage_seconds
    return ReconnectStormResult(
        devices=devices,
        peak_connections_per_second=round(peak, 2),
        total_backlog_messages=backlog,
        estimated_drain_seconds=round(backlog / broker_messages_per_second, 2),
        within_broker_capacity=devices <= broker_max_connections
        and peak <= broker_messages_per_second,
    )


# --- EDGE-008: device resource measurement ----------------------------------


@dataclass(frozen=True)
class ResourceSample:
    cpu_fraction: float
    memory_mb: float
    temperature_c: float


@dataclass(frozen=True)
class ResourceVerdict:
    passed: bool
    failures: tuple[str, ...]
    cpu_p95: float
    memory_p95_mb: float
    temperature_max_c: float
    frame_loss_fraction: float


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def evaluate_resources(
    samples: Iterable[ResourceSample],
    *,
    frames_expected: int,
    frames_built: int,
    cpu_budget: float = PI_ZERO_CPU_BUDGET,
    memory_budget_mb: float = PI_ZERO_MEMORY_BUDGET_MB,
    max_temperature_c: float = PI_ZERO_MAX_TEMP_C,
) -> ResourceVerdict:
    """Pass or fail a Pi Zero 2 W run against the documented budgets.

    Frame loss is included because a device that stays cool by silently
    skipping frames has not passed; it has changed the workload.
    """
    collected = list(samples)
    if not collected:
        raise ValueError("resource evaluation requires at least one sample")

    cpu_p95 = round(_percentile([s.cpu_fraction for s in collected], 0.95), 4)
    memory_p95 = round(_percentile([s.memory_mb for s in collected], 0.95), 2)
    temperature_max = round(max(s.temperature_c for s in collected), 2)
    lost = max(0, frames_expected - frames_built)
    loss = 0.0 if frames_expected == 0 else round(lost / frames_expected, 6)

    failures: list[str] = []
    if cpu_p95 > cpu_budget:
        failures.append(f"cpu p95 {cpu_p95} exceeds budget {cpu_budget}")
    if memory_p95 > memory_budget_mb:
        failures.append(f"memory p95 {memory_p95} MB exceeds budget {memory_budget_mb} MB")
    if temperature_max > max_temperature_c:
        failures.append(
            f"peak temperature {temperature_max} C exceeds {max_temperature_c} C"
        )
    if loss > 0:
        failures.append(f"{lost} of {frames_expected} frames were never built")

    return ResourceVerdict(
        passed=not failures,
        failures=tuple(failures),
        cpu_p95=cpu_p95,
        memory_p95_mb=memory_p95,
        temperature_max_c=temperature_max,
        frame_loss_fraction=loss,
    )


def thermal_throttling_suspected(samples: Sequence[ResourceSample]) -> bool:
    """Rising temperature with falling CPU is the signature of throttling.

    Reporting it matters because a throttled device looks like it is comfortably
    under its CPU budget while actually failing to keep up.
    """
    if len(samples) < 6:
        return False
    half = len(samples) // 2
    early, late = samples[:half], samples[half:]
    temp_rose = statistics.mean(s.temperature_c for s in late) > statistics.mean(
        s.temperature_c for s in early
    ) + 5
    cpu_fell = statistics.mean(s.cpu_fraction for s in late) < statistics.mean(
        s.cpu_fraction for s in early
    ) * 0.85
    return temp_rose and cpu_fell
