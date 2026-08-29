from __future__ import annotations

import json
from pathlib import Path

import pytest

from car_telemetry.observations import ObservationStore, observation_meta
from car_telemetry.vehicle_support import (
    NO_DATA,
    SUPPORTED,
    UNSUPPORTED,
    VehicleFixture,
    VehicleFixtureError,
    classify_signal,
    discovery_report,
    load_fixtures,
    publishable_signals,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vehicles"

CORE_SIGNALS = (
    "RPM",
    "SPEED",
    "COOLANT_TEMP",
    "INTAKE_TEMP",
    "ENGINE_LOAD",
    "THROTTLE_POS",
    "FUEL_LEVEL",
    "CONTROL_MODULE_VOLTAGE",
    "MAF",
    "OIL_TEMP",
)

ALL_FIXTURES = load_fixtures(FIXTURE_DIR)
FIXTURE_IDS = [fixture.key for fixture in ALL_FIXTURES]


def fixture_by_key(key: str) -> VehicleFixture:
    return next(f for f in ALL_FIXTURES if f.key == key)


def test_the_fixture_set_covers_multiple_distinct_combinations():
    assert len(ALL_FIXTURES) >= 5, "several vehicle/support combinations are required"
    support_sets = {fixture.supported_commands for fixture in ALL_FIXTURES}
    assert len(support_sets) == len(ALL_FIXTURES), "every fixture is a distinct case"
    assert any(fixture.null_responses for fixture in ALL_FIXTURES), (
        "at least one vehicle must advertise a PID and then return null"
    )


# --- the central guarantee --------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
def test_unsupported_signals_are_absent_never_zero(fixture):
    published = publishable_signals(CORE_SIGNALS, fixture)

    for name in CORE_SIGNALS:
        support = classify_signal(name, fixture)
        if support.publishable:
            continue
        assert name not in published, (
            f"{fixture.key}: {name} is {support.state} and must be omitted entirely, "
            f"never published as a value"
        )


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
def test_no_signal_is_invented_beyond_what_the_vehicle_advertises(fixture):
    published = publishable_signals(CORE_SIGNALS, fixture)

    assert set(published) <= set(fixture.supported_commands)


def test_a_missing_fuel_level_is_omitted_rather_than_reported_empty():
    """The failure this issue exists to prevent: absence read as an empty tank."""
    fixture = fixture_by_key("no-fuel-level")

    published = publishable_signals(CORE_SIGNALS, fixture)

    assert "FUEL_LEVEL" not in published
    assert classify_signal("FUEL_LEVEL", fixture).state == UNSUPPORTED


def test_an_advertised_but_null_signal_is_also_omitted():
    fixture = fixture_by_key("advertised-but-null")

    published = publishable_signals(CORE_SIGNALS, fixture)

    assert classify_signal("FUEL_LEVEL", fixture).state == NO_DATA
    assert "FUEL_LEVEL" not in published, "advertised is not the same as available"
    assert "INTAKE_TEMP" not in published
    assert "RPM" in published, "working signals are unaffected"


def test_a_genuine_zero_is_still_published():
    """Absence and zero must stay distinguishable in both directions."""
    fixture = fixture_by_key("stationary-engine-off")

    published = publishable_signals(CORE_SIGNALS, fixture)

    assert published["RPM"]["value"] == 0
    assert published["SPEED"]["value"] == 0
    assert published["FUEL_LEVEL"]["value"] == 0.0, (
        "a real empty tank is a measurement, not an absence"
    )


def test_minimal_vehicle_publishes_only_what_it_has():
    fixture = fixture_by_key("minimal-legacy")

    published = publishable_signals(CORE_SIGNALS, fixture)

    assert set(published) == {"RPM", "SPEED"}


# --- discovery reporting ----------------------------------------------------


def test_discovery_report_separates_the_three_states():
    report = discovery_report(CORE_SIGNALS, fixture_by_key("advertised-but-null"))

    assert report[SUPPORTED] == ["CONTROL_MODULE_VOLTAGE", "COOLANT_TEMP", "RPM", "SPEED"]
    assert report[NO_DATA] == ["FUEL_LEVEL", "INTAKE_TEMP"]
    assert "MAF" in report[UNSUPPORTED]
    assert "OIL_TEMP" in report[UNSUPPORTED]


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
def test_every_core_signal_is_classified_exactly_once(fixture):
    report = discovery_report(CORE_SIGNALS, fixture)

    classified = report[SUPPORTED] + report[NO_DATA] + report[UNSUPPORTED]
    assert sorted(classified) == sorted(CORE_SIGNALS)
    assert len(classified) == len(set(classified))


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
def test_published_signals_carry_canonical_units(fixture):
    published = publishable_signals(CORE_SIGNALS, fixture)

    expected = {
        "RPM": "rpm", "SPEED": "km/h", "COOLANT_TEMP": "degC",
        "INTAKE_TEMP": "degC", "OIL_TEMP": "degC", "ENGINE_LOAD": "%",
        "THROTTLE_POS": "%", "FUEL_LEVEL": "%",
        "CONTROL_MODULE_VOLTAGE": "V", "MAF": "g/s",
    }
    for name, signal in published.items():
        assert signal["unit"] == expected[name]


# --- observation store integration ------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
def test_unsupported_signals_never_reach_the_observation_store(fixture):
    """End-to-end: what a vehicle cannot measure never enters a frame."""
    store = ObservationStore()
    store.update_obd_status(connected=True, engine_on=True)
    for name, signal in publishable_signals(CORE_SIGNALS, fixture).items():
        store.update_obd_signal(
            name,
            {
                "value": signal["value"],
                "unit": signal["unit"],
                **observation_meta(
                    observed_at="2026-03-01T12:00:00.500Z",
                    source="obd.pid",
                    quality="valid",
                    max_age_ms=2000,
                ),
            },
        )

    snapshot = store.snapshot(
        "2026-03-01T12:00:00.000Z", "2026-03-01T12:00:01.000Z"
    )
    signals = snapshot.obd["signals"]

    for name in CORE_SIGNALS:
        if classify_signal(name, fixture).publishable:
            continue
        assert name not in signals, f"{name} leaked into the frame for {fixture.key}"


# --- fixture integrity ------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=FIXTURE_IDS)
def test_fixtures_are_internally_consistent(fixture):
    assert fixture.description.strip(), "each fixture explains the case it covers"
    assert fixture.protocol_name.strip()
    assert set(fixture.responses) <= set(fixture.supported_commands)
    assert not set(fixture.responses) & set(fixture.null_responses), (
        "a null response cannot also carry a value"
    )


def test_malformed_fixtures_are_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"vehicleKey": "x"}), encoding="utf-8")
    with pytest.raises(VehicleFixtureError):
        VehicleFixture.load(bad)

    inconsistent = tmp_path / "inconsistent.json"
    inconsistent.write_text(
        json.dumps(
            {
                "vehicleKey": "x",
                "description": "d",
                "protocolName": "p",
                "supportedCommands": ["RPM"],
                "nullResponses": ["SPEED"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(VehicleFixtureError, match="null responses for unsupported"):
        VehicleFixture.load(inconsistent)

    with pytest.raises(VehicleFixtureError):
        VehicleFixture.load(tmp_path / "absent.json")
