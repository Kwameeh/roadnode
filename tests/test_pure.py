from car_telemetry.vehicle_profiles import safe_id
from car_telemetry.benchmark import _grade


def test_safe_id():
    assert safe_id("VIN A/B") == "VIN_A_B"


def test_benchmark_grade_pass():
    report = {
        "metrics": {
            "minSystemAvailableMb": 120,
            "averageSystemCpuPercent": 40,
            "processRssGrowthMb": 2,
            "throttledEnd": "throttled=0x0",
        },
        "workers": {
            "web-stream": {"deadlineMissPercent": 0.0, "p95WorkMs": 5.0},
            "imu": {"deadlineMissPercent": 0.0, "p95WorkMs": 1.0},
        },
    }
    result, _ = _grade(report)
    assert result == "PASS"
