from datetime import datetime, timezone
import math

import pytest

from car_telemetry.gps import parse_observation
from car_telemetry.observations import (
    ImuSample,
    ObservationStore,
    normalize_obd_value,
    observation_meta,
)


class Quantity:
    def __init__(self, magnitude, units="revolutions_per_minute"):
        self.magnitude = magnitude
        self.units = units

    def to(self, unit):
        return Quantity(self.magnitude, unit)


def test_observation_metadata_requires_utc_and_known_quality():
    meta = observation_meta(
        observed_at="2026-08-27T14:25:31.900Z",
        source="gps.nmea",
        quality="valid",
        max_age_ms=3000,
    )
    assert meta == {
        "observedAt": "2026-08-27T14:25:31.900Z",
        "source": "gps.nmea",
        "quality": "valid",
        "maxAgeMs": 3000,
    }


def test_obd_values_use_canonical_units():
    assert normalize_obd_value("RPM", Quantity(1800)) == (1800.0, "rpm")
    assert normalize_obd_value("SPEED", 61) == (61, "km/h")
    with pytest.raises(ValueError, match="finite"):
        normalize_obd_value("SPEED", math.nan)


def test_gps_rmc_uses_source_time_and_normalized_names():
    line = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
    legacy, normalized = parse_observation(
        line,
        received_at=datetime(2026, 8, 27, 14, 25, tzinfo=timezone.utc),
    )
    assert legacy["validFix"] is True
    assert normalized["fix"] is True
    assert normalized["headingDeg"] == 84.4
    assert normalized["observedAt"] == "1994-03-23T12:35:19Z"
    assert normalized["source"] == "gps.nmea"


def test_observation_store_returns_only_samples_inside_half_open_window():
    store = ObservationStore()
    store.append_imu_sample(ImuSample("2026-08-27T14:25:30.999Z", 0, 0, 9.8, 0, 0, 0))
    store.append_imu_sample(ImuSample("2026-08-27T14:25:31.000Z", 1, 0, 9.8, 0, 0, 0))
    store.append_imu_sample(ImuSample("2026-08-27T14:25:31.950Z", 2, 0, 9.8, 0, 0, 0))
    store.append_imu_sample(ImuSample("2026-08-27T14:25:32.000Z", 3, 0, 9.8, 0, 0, 0))

    snapshot = store.snapshot(
        "2026-08-27T14:25:31.000Z",
        "2026-08-27T14:25:32.000Z",
    )

    assert [sample.ax for sample in snapshot.imu_samples] == [1, 2]


def test_snapshot_excludes_observations_from_after_frame_boundary():
    store = ObservationStore()
    store.update_gps(
        {
            "fix": True,
            "observedAt": "2026-08-27T14:25:33.000Z",
            "source": "gps.nmea",
            "quality": "valid",
            "maxAgeMs": 3000,
        }
    )
    store.update_obd_signal(
        "RPM",
        {
            "value": 1000,
            "unit": "rpm",
            "observedAt": "2026-08-27T14:25:33.000Z",
            "source": "obd.pid",
            "quality": "valid",
            "maxAgeMs": 2000,
        },
    )
    snapshot = store.snapshot(
        "2026-08-27T14:25:31.000Z", "2026-08-27T14:25:32.000Z"
    )
    assert snapshot.gps is None
    assert snapshot.obd["signals"] == {}
