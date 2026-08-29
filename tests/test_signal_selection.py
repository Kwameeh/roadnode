from __future__ import annotations

import json

import pytest

from car_telemetry.retained_state import RetainedStatePublisher
from car_telemetry.signal_policy import CORE_SIGNALS
from car_telemetry.signal_selection import (
    SignalSelectionError,
    SignalSelectionService,
    SignalSelectionStore,
)

SUPPORTED = CORE_SIGNALS + ("ENGINE_LOAD", "THROTTLE_POS", "MAF", "OIL_TEMP", "ODOMETER")


def service(tmp_path, vehicle_key="VIN-ABC", **kwargs) -> SignalSelectionService:
    return SignalSelectionService(SignalSelectionStore(tmp_path), vehicle_key, **kwargs)


def connected(tmp_path, vehicle_key="VIN-ABC", supported=SUPPORTED, **kwargs):
    instance = service(tmp_path, vehicle_key, **kwargs)
    instance.observe_supported(supported)
    return instance


def test_core_cannot_be_removed_from_the_ui(tmp_path):
    signals = connected(tmp_path)

    for name in CORE_SIGNALS:
        with pytest.raises(SignalSelectionError, match="core signal"):
            signals.select(name, False)
        assert name in signals.plan.selected


def test_core_cannot_be_removed_by_editing_the_stored_file(tmp_path):
    store = SignalSelectionStore(tmp_path)
    path = store.path("VIN-ABC")
    path.write_text(
        json.dumps({"signalSelection": {"deselected": list(CORE_SIGNALS)}}),
        encoding="utf-8",
    )

    signals = SignalSelectionService(store, "VIN-ABC")
    signals.observe_supported(SUPPORTED)

    assert set(CORE_SIGNALS).issubset(signals.plan.selected)


def test_adding_an_unsupported_signal_is_refused_with_a_reason(tmp_path):
    signals = connected(tmp_path, supported=("RPM", "SPEED"))

    with pytest.raises(SignalSelectionError, match="not advertised"):
        signals.select("ODOMETER", True)


def test_changes_are_refused_while_disconnected(tmp_path):
    signals = service(tmp_path)

    with pytest.raises(SignalSelectionError, match="connected"):
        signals.select("OIL_TEMP", True)


def test_a_selection_survives_reconnect(tmp_path):
    first = connected(tmp_path)
    first.select("OIL_TEMP", True)
    first.select("MAF", False)

    reopened = connected(tmp_path)

    assert "OIL_TEMP" in reopened.plan.selected
    assert "MAF" not in reopened.plan.selected
    assert reopened.revision == first.revision


def test_a_choice_for_another_vehicle_is_kept_not_deleted(tmp_path):
    signals = connected(tmp_path, "VIN-WITH-OIL")
    signals.select("OIL_TEMP", True)

    signals.use_vehicle("VIN-WITHOUT-OIL")
    signals.observe_supported(("RPM", "SPEED"))
    assert "OIL_TEMP" not in signals.plan.selected

    signals.use_vehicle("VIN-WITH-OIL")
    signals.observe_supported(SUPPORTED)
    assert "OIL_TEMP" in signals.plan.selected


def test_a_flaky_reconnect_does_not_prune_stored_intent(tmp_path):
    signals = connected(tmp_path)
    signals.select("OIL_TEMP", True)

    signals.observe_supported(("RPM", "SPEED"))  # mid-discovery, incomplete
    assert "OIL_TEMP" not in signals.plan.selected

    signals.observe_supported(SUPPORTED)
    assert "OIL_TEMP" in signals.plan.selected


def test_revision_changes_on_a_real_change_and_only_then(tmp_path):
    signals = connected(tmp_path)
    before = signals.revision

    signals.observe_supported(SUPPORTED)
    assert signals.revision == before

    signals.select("OIL_TEMP", True)
    after = signals.revision
    assert after != before

    signals.select("OIL_TEMP", True)
    assert signals.revision == after


def test_metadata_publishes_the_revision_and_only_resolved_facts(tmp_path):
    signals = connected(tmp_path)
    signals.select("ODOMETER", True)
    publisher = RetainedStatePublisher("DEV-1")

    body = signals.metadata_body({"deviceId": "DEV-1"})
    first = publisher.metadata_if_changed(body)

    assert first is not None
    assert body["signals"]["revision"] == signals.revision
    assert "requested" not in body["signals"]
    assert "deselected" not in body["signals"]
    assert publisher.metadata_if_changed(body) is None, "unchanged metadata must not resend"

    signals.select("ODOMETER", False)
    assert publisher.metadata_if_changed(signals.metadata_body({"deviceId": "DEV-1"})) is not None


def test_the_budget_refuses_an_extra_signal_instead_of_dropping_one(tmp_path):
    extras = tuple(f"CUSTOM_PID_{index:02d}" for index in range(40))
    signals = connected(tmp_path, supported=CORE_SIGNALS + extras, budget_seconds=0.8)

    added = [name for name in extras if _try_select(signals, name)]

    assert added, "some optional signals must fit"
    assert len(added) < len(extras), "the budget must eventually refuse"
    assert set(CORE_SIGNALS).issubset(signals.plan.selected)
    assert signals.plan.cycle_seconds <= 0.8


def _try_select(signals: SignalSelectionService, name: str) -> bool:
    try:
        signals.select(name, True)
    except SignalSelectionError:
        return False
    return True


def test_a_pre_edge004_profile_keeps_its_extras(tmp_path):
    store = SignalSelectionStore(tmp_path)
    store.path("VIN-OLD").write_text(
        json.dumps({"vehicleKey": "VIN-OLD", "selectedSignals": ["OIL_TEMP"]}),
        encoding="utf-8",
    )

    signals = SignalSelectionService(store, "VIN-OLD")
    signals.observe_supported(SUPPORTED)

    assert "OIL_TEMP" in signals.plan.selected
    assert set(CORE_SIGNALS).issubset(signals.plan.selected)


def test_a_corrupt_preferences_file_falls_back_to_policy_defaults(tmp_path):
    store = SignalSelectionStore(tmp_path)
    store.path("VIN-BAD").write_text("{not json", encoding="utf-8")

    signals = SignalSelectionService(store, "VIN-BAD")
    signals.observe_supported(SUPPORTED)

    assert set(CORE_SIGNALS).issubset(signals.plan.selected)
    assert "OIL_TEMP" not in signals.plan.selected


def test_saving_never_leaves_a_truncated_file(tmp_path):
    signals = connected(tmp_path)
    signals.select("OIL_TEMP", True)

    stored = json.loads(SignalSelectionStore(tmp_path).path("VIN-ABC").read_text(encoding="utf-8"))

    assert stored["signalSelection"]["requested"] == ["OIL_TEMP"]
    assert stored["signalsRevision"] == signals.revision
    assert not list(tmp_path.glob("*.tmp"))


def test_the_ui_document_explains_every_offered_signal(tmp_path):
    signals = connected(tmp_path)
    document = signals.ui_document()

    assert document["connected"] is True
    assert document["revision"] == signals.revision
    assert set(document["supported"]) == set(SUPPORTED)
    named = {item["name"] for item in document["decisions"]}
    assert set(SUPPORTED).issubset(named)
    assert all(item["purpose"] for item in document["decisions"])
    assert document["explanation"]
