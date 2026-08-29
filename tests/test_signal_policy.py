from __future__ import annotations

from pathlib import Path

import pytest

from car_telemetry.signal_policy import (
    AVAILABLE,
    CORE_SIGNALS,
    DEFAULT_ROUND_TRIP_SECONDS,
    POLLING_BUDGET_SECONDS,
    SELECTED,
    UNAVAILABLE,
    SignalPolicyError,
    definition,
    fast_signals_are_covered,
    plan_selection,
)
from car_telemetry.vehicle_support import load_fixtures

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vehicles"


def state_of(plan, name: str) -> str:
    return next(item.state for item in plan.decisions if item.name == name)


def test_core_is_selected_even_when_the_owner_asks_to_remove_it():
    plan = plan_selection(
        supported=CORE_SIGNALS + ("MAF",),
        deselected=("RPM", "SPEED", "COOLANT_TEMP", "CONTROL_MODULE_VOLTAGE", "MAF"),
    )

    for name in CORE_SIGNALS:
        assert state_of(plan, name) == SELECTED
    assert state_of(plan, "MAF") == AVAILABLE
    assert "MAF" not in plan.selected


def test_default_tier_is_on_until_the_owner_removes_it():
    supported = CORE_SIGNALS + ("ENGINE_LOAD", "THROTTLE_POS")

    assert "ENGINE_LOAD" in plan_selection(supported).selected
    assert "ENGINE_LOAD" not in plan_selection(supported, deselected=("ENGINE_LOAD",)).selected


def test_optional_tier_is_off_until_the_owner_adds_it():
    supported = CORE_SIGNALS + ("OIL_TEMP",)

    assert "OIL_TEMP" not in plan_selection(supported).selected
    assert "OIL_TEMP" in plan_selection(supported, requested=("OIL_TEMP",)).selected


def test_unsupported_core_is_reported_unavailable_and_never_faked():
    plan = plan_selection(supported=("RPM", "SPEED"))

    assert plan.unavailable_core == ("CONTROL_MODULE_VOLTAGE", "COOLANT_TEMP")
    assert "COOLANT_TEMP" not in plan.selected
    assert any("never as zero" in line for line in plan.explanation())


def test_requesting_a_signal_the_vehicle_never_advertises_is_unavailable():
    plan = plan_selection(supported=("RPM", "SPEED"), requested=("ODOMETER",))

    assert state_of(plan, "ODOMETER") == UNAVAILABLE
    assert "ODOMETER" not in plan.selected


def test_selection_is_bounded_by_the_core_freshness_budget():
    extras = tuple(f"CUSTOM_PID_{index:02d}" for index in range(60))
    plan = plan_selection(supported=CORE_SIGNALS + extras, requested=extras)

    assert plan.cycle_seconds <= POLLING_BUDGET_SECONDS
    assert plan.capacity == 0
    assert plan.rejected, "an unbounded request must be trimmed, not accepted"
    assert set(CORE_SIGNALS).issubset(plan.selected)
    assert any("would slow the core readings" in line for line in plan.explanation())


def test_rejection_is_deterministic_regardless_of_request_order():
    extras = tuple(f"CUSTOM_PID_{index:02d}" for index in range(60))
    first = plan_selection(supported=CORE_SIGNALS + extras, requested=extras)
    second = plan_selection(
        supported=tuple(reversed(CORE_SIGNALS + extras)), requested=tuple(reversed(extras))
    )

    assert first.selected == second.selected
    assert first.revision == second.revision


def test_revision_changes_only_when_the_plan_changes():
    supported = CORE_SIGNALS + ("OIL_TEMP", "MAF")
    base = plan_selection(supported)

    assert base.revision == plan_selection(supported).revision
    assert base.revision != plan_selection(supported, requested=("OIL_TEMP",)).revision


def test_capacity_counts_what_still_fits():
    plan = plan_selection(supported=CORE_SIGNALS)
    expected = int(
        (POLLING_BUDGET_SECONDS - len(CORE_SIGNALS) * DEFAULT_ROUND_TRIP_SECONDS)
        // DEFAULT_ROUND_TRIP_SECONDS
    )

    assert plan.capacity == expected
    assert f"add up to {expected}" in " ".join(plan.explanation())


def test_unknown_pids_are_admitted_as_plain_optional_signals():
    spec = definition("SOME_MANUFACTURER_PID")

    assert spec.tier == "optional"
    assert not spec.mandatory
    assert "No RoadNode feature depends on it" in spec.purpose


def test_every_recorded_vehicle_fixture_keeps_fast_signals_covered():
    fixtures = load_fixtures(FIXTURE_DIR)
    assert fixtures, "vehicle fixtures are required to prove this policy"

    for fixture in fixtures:
        plan = plan_selection(fixture.supported_commands)
        assert fast_signals_are_covered(plan), fixture.key
        assert plan.cycle_seconds <= POLLING_BUDGET_SECONDS, fixture.key
        for name in plan.unavailable:
            assert name not in plan.selected


def test_a_budget_smaller_than_one_signal_is_rejected():
    with pytest.raises(SignalPolicyError):
        plan_selection(CORE_SIGNALS, budget_seconds=0.01)
    with pytest.raises(SignalPolicyError):
        plan_selection(CORE_SIGNALS, round_trip_seconds=0)


def test_decisions_explain_every_supported_signal_exactly_once():
    supported = CORE_SIGNALS + ("MAF", "OIL_TEMP")
    plan = plan_selection(supported, requested=("OIL_TEMP",))
    names = [item.name for item in plan.decisions]

    assert len(names) == len(set(names))
    assert set(supported).issubset(names)
    assert all(item.purpose for item in plan.decisions)
    assert all(item.reason for item in plan.decisions)
