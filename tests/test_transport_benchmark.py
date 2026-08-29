from __future__ import annotations

import pytest

from car_telemetry.transport_benchmark import (
    PI_ZERO_CPU_BUDGET,
    ResourceSample,
    catch_up_is_viable,
    evaluate_resources,
    measure_payload,
    project_data_cost,
    project_outage,
    project_reconnect_storm,
    thermal_throttling_suspected,
)


def frame(sample_count: int = 20) -> dict:
    return {
        "schemaVersion": 2,
        "messageId": "DEV-001:boot:1",
        "messageType": "vehicle_frame",
        "deviceId": "DEV-001",
        "capturedAt": "2026-03-01T12:00:01.000Z",
        "payload": {
            "imu": {
                "samples": [
                    [i * 50, 0.12, -0.03, 9.79, 0.001, 0.002, -0.001]
                    for i in range(sample_count)
                ]
            },
            "telemetry": {"obd": {"signals": {"RPM": {"value": 1850, "unit": "rpm"}}}},
        },
    }


# --- MQTT-008: payload ------------------------------------------------------


def test_measures_wire_size_and_compression():
    profile = measure_payload(frame())

    assert profile.frame_bytes > 0
    assert profile.imu_sample_count == 20
    assert profile.compressed_bytes < profile.frame_bytes
    assert 0 < profile.compression_ratio < 1


def test_payload_scales_with_imu_sample_count():
    small = measure_payload(frame(5))
    large = measure_payload(frame(20))

    assert large.frame_bytes > small.frame_bytes
    assert large.bytes_per_imu_sample < small.bytes_per_imu_sample, (
        "the fixed envelope amortises across more samples"
    )


def test_empty_frame_reports_zero_per_sample():
    profile = measure_payload({"payload": {"imu": {"samples": []}}})

    assert profile.bytes_per_imu_sample == 0.0


# --- MQTT-008: data cost ----------------------------------------------------


def test_projects_monthly_data_cost():
    projection = project_data_cost(measure_payload(frame()), active_hours_per_day=2)

    assert projection.active_seconds_per_day == 7_200
    assert projection.megabytes_per_month > 0
    assert projection.cost_per_month > 0


def test_data_cost_counts_per_message_overhead():
    profile = measure_payload(frame())

    without = project_data_cost(profile, active_hours_per_day=2, mqtt_overhead_bytes=0)
    with_overhead = project_data_cost(profile, active_hours_per_day=2, mqtt_overhead_bytes=60)

    # At one message per second, framing is a real fraction of the bill.
    assert with_overhead.bytes_per_day > without.bytes_per_day


def test_data_cost_scales_with_driving_time():
    profile = measure_payload(frame())

    light = project_data_cost(profile, active_hours_per_day=1)
    heavy = project_data_cost(profile, active_hours_per_day=8)

    assert heavy.cost_per_month > light.cost_per_month * 7


def test_rejects_impossible_driving_hours():
    with pytest.raises(ValueError):
        project_data_cost(measure_payload(frame()), active_hours_per_day=25)


# --- MQTT-008: outage -------------------------------------------------------


def test_short_outage_stays_within_bounds():
    projection = project_outage(
        measure_payload(frame()),
        outage_seconds=3_600,
        max_bytes=256 * 1024 * 1024,
        max_age_seconds=86_400,
        drain_messages_per_second=50,
    )

    assert projection.queued_messages == 3_600
    assert projection.exceeds_size_bound is False
    assert projection.exceeds_age_bound is False


def test_long_outage_breaches_the_age_bound():
    projection = project_outage(
        measure_payload(frame()),
        outage_seconds=2 * 86_400,
        max_bytes=256 * 1024 * 1024,
        max_age_seconds=86_400,
        drain_messages_per_second=50,
    )

    assert projection.exceeds_age_bound is True


def test_catch_up_must_outpace_live_production():
    profile = measure_payload(frame())
    fast = project_outage(
        profile,
        outage_seconds=3_600,
        max_bytes=1 << 30,
        max_age_seconds=86_400,
        drain_messages_per_second=50,
    )
    too_slow = project_outage(
        profile,
        outage_seconds=3_600,
        max_bytes=1 << 30,
        max_age_seconds=86_400,
        drain_messages_per_second=0.5,
    )

    assert catch_up_is_viable(fast, 3_600) is True
    # Draining slower than production means the backlog never clears.
    assert catch_up_is_viable(too_slow, 3_600) is False


def test_rejects_a_zero_drain_rate():
    with pytest.raises(ValueError):
        project_outage(
            measure_payload(frame()),
            outage_seconds=60,
            max_bytes=1 << 30,
            max_age_seconds=86_400,
            drain_messages_per_second=0,
        )


# --- MQTT-008: reconnect storm ----------------------------------------------


def test_jittered_reconnect_stays_within_broker_capacity():
    result = project_reconnect_storm(
        devices=100,
        outage_seconds=600,
        broker_max_connections=1024,
        broker_messages_per_second=500,
        reconnect_jitter_seconds=60,
    )

    assert result.within_broker_capacity is True
    assert result.peak_connections_per_second < 500


def test_synchronised_reconnect_overwhelms_the_broker():
    result = project_reconnect_storm(
        devices=1000,
        outage_seconds=600,
        broker_max_connections=1024,
        broker_messages_per_second=100,
        reconnect_jitter_seconds=1,
    )

    assert result.within_broker_capacity is False


def test_zero_jitter_is_refused_as_unsurvivable():
    with pytest.raises(ValueError, match="jitter"):
        project_reconnect_storm(
            devices=100,
            outage_seconds=600,
            broker_max_connections=1024,
            broker_messages_per_second=500,
            reconnect_jitter_seconds=0,
        )


# --- EDGE-008: device resources ---------------------------------------------


def samples(cpu=0.3, memory=120.0, temp=55.0, count=20):
    return [ResourceSample(cpu, memory, temp) for _ in range(count)]


def test_a_healthy_run_passes():
    verdict = evaluate_resources(samples(), frames_expected=1000, frames_built=1000)

    assert verdict.passed is True
    assert verdict.failures == ()
    assert verdict.frame_loss_fraction == 0


def test_cpu_over_budget_fails():
    verdict = evaluate_resources(samples(cpu=0.9), frames_expected=100, frames_built=100)

    assert verdict.passed is False
    assert any("cpu" in f for f in verdict.failures)


def test_memory_over_budget_fails():
    verdict = evaluate_resources(samples(memory=300.0), frames_expected=100, frames_built=100)

    assert any("memory" in f for f in verdict.failures)


def test_overheating_fails():
    verdict = evaluate_resources(samples(temp=85.0), frames_expected=100, frames_built=100)

    assert any("temperature" in f for f in verdict.failures)


def test_dropped_frames_fail_even_when_resources_look_fine():
    """Staying cool by skipping frames is not a pass."""
    verdict = evaluate_resources(samples(), frames_expected=1000, frames_built=940)

    assert verdict.passed is False
    assert verdict.frame_loss_fraction == 0.06
    assert any("never built" in f for f in verdict.failures)


def test_requires_at_least_one_sample():
    with pytest.raises(ValueError):
        evaluate_resources([], frames_expected=1, frames_built=1)


def test_uses_p95_not_peak_for_cpu():
    # One brief spike must not fail an otherwise healthy run.
    mixed = samples(cpu=0.2, count=99) + [ResourceSample(0.99, 120.0, 55.0)]
    verdict = evaluate_resources(mixed, frames_expected=100, frames_built=100)

    assert verdict.cpu_p95 <= PI_ZERO_CPU_BUDGET
    assert verdict.passed is True


def test_detects_thermal_throttling():
    """Rising heat with falling CPU looks like headroom but is not."""
    throttling = [ResourceSample(0.55, 120.0, 60.0) for _ in range(6)] + [
        ResourceSample(0.30, 120.0, 74.0) for _ in range(6)
    ]

    assert thermal_throttling_suspected(throttling) is True


def test_steady_load_is_not_throttling():
    assert thermal_throttling_suspected(samples(count=12)) is False


def test_too_few_samples_to_judge_throttling():
    assert thermal_throttling_suspected(samples(count=3)) is False
