from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from car_telemetry.fleet_inventory import (
    FleetInventory,
    InventoryError,
    installation_checklist,
    provision_device,
    stale_installations,
)

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(offset_days: float = 0) -> str:
    return (BASE + timedelta(days=offset_days)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def inventory():
    fleet = FleetInventory()
    fleet.register("DEV-001", hardware_revision="rev-c", at=iso(0))
    return fleet


def installed(fleet: FleetInventory, device_id: str, vehicle_id: str = "VEH-A"):
    provision_device(fleet, device_id)
    return fleet.install(
        device_id, vehicle_id=vehicle_id, calibration_version="imu-cal-v1-abc", at=iso(1)
    )


# --- lifecycle --------------------------------------------------------------


def test_registration_records_hardware(inventory):
    record = inventory.get("DEV-001")

    assert record.state == "registered"
    assert record.hardware_revision == "rev-c"
    assert record.active is True


def test_cannot_register_the_same_device_twice(inventory):
    with pytest.raises(InventoryError, match="already registered"):
        inventory.register("DEV-001", hardware_revision="rev-c")


def test_provisioning_records_the_credential_version(inventory):
    record, credential = provision_device(inventory, "DEV-001")

    assert record.state == "provisioned"
    assert record.credential_version == credential.credential_version


def test_installation_records_vehicle_and_calibration(inventory):
    record = installed(inventory, "DEV-001")

    assert record.state == "installed"
    assert record.vehicle_id == "VEH-A"
    assert record.calibration_version == "imu-cal-v1-abc"
    assert record.installed_at == iso(1)


def test_installation_requires_a_calibration_version(inventory):
    """An installed device without calibration produces unscoreable IMU data."""
    provision_device(inventory, "DEV-001")

    with pytest.raises(InventoryError, match="calibration"):
        inventory.install("DEV-001", vehicle_id="VEH-A", calibration_version="  ")


def test_cannot_install_a_device_that_was_never_provisioned(inventory):
    with pytest.raises(InventoryError, match="cannot move"):
        inventory.install("DEV-001", vehicle_id="VEH-A", calibration_version="cal-1")


def test_a_vehicle_holds_only_one_installed_device(inventory):
    installed(inventory, "DEV-001")
    inventory.register("DEV-002", hardware_revision="rev-c")
    provision_device(inventory, "DEV-002")

    with pytest.raises(InventoryError, match="already has device"):
        inventory.install("DEV-002", vehicle_id="VEH-A", calibration_version="cal-2")


def test_uninstall_frees_the_vehicle_without_retiring_hardware(inventory):
    installed(inventory, "DEV-001")

    record = inventory.uninstall("DEV-001")

    assert record.state == "provisioned"
    assert record.vehicle_id is None
    assert inventory.vehicle_occupant("VEH-A") is None


def test_a_freed_vehicle_accepts_a_replacement_device(inventory):
    installed(inventory, "DEV-001")
    inventory.uninstall("DEV-001")
    inventory.register("DEV-002", hardware_revision="rev-d")

    record = installed(inventory, "DEV-002")

    assert record.vehicle_id == "VEH-A"


def test_retiring_clears_the_vehicle_assignment(inventory):
    installed(inventory, "DEV-001")

    record = inventory.retire("DEV-001", reason="hardware fault")

    assert record.state == "retired"
    assert record.vehicle_id is None
    assert record.active is False


def test_a_terminal_device_cannot_return_to_service(inventory):
    inventory.retire("DEV-001")

    with pytest.raises(InventoryError, match="cannot move"):
        provision_device(inventory, "DEV-001")


@pytest.mark.parametrize("terminal", ["retire", "report_lost"])
def test_terminal_states_are_final(inventory, terminal):
    getattr(inventory, terminal)("DEV-001")

    with pytest.raises(InventoryError):
        inventory.install("DEV-001", vehicle_id="VEH-A", calibration_version="cal")


# --- revocation support -----------------------------------------------------


def test_lost_devices_appear_on_the_revocation_list(inventory):
    installed(inventory, "DEV-001")
    inventory.register("DEV-002", hardware_revision="rev-c")
    installed(inventory, "DEV-002", vehicle_id="VEH-B")

    inventory.report_lost("DEV-001", reason="stolen from vehicle")

    # This list is what revocation actually works from.
    assert inventory.revocation_list() == ("DEV-001",)


def test_retired_devices_are_also_revoked(inventory):
    inventory.retire("DEV-001")

    assert "DEV-001" in inventory.revocation_list()


def test_active_devices_are_never_on_the_revocation_list(inventory):
    installed(inventory, "DEV-001")

    assert inventory.revocation_list() == ()


# --- operational views ------------------------------------------------------


def test_summary_counts_every_state(inventory):
    inventory.register("DEV-002", hardware_revision="rev-c")
    installed(inventory, "DEV-002")

    summary = inventory.summary()

    assert summary["registered"] == 1
    assert summary["installed"] == 1


def test_checklist_names_what_still_blocks_a_device(inventory):
    outstanding = installation_checklist(inventory.get("DEV-001"))

    assert any("provision" in item for item in outstanding)
    assert any("calibration" in item for item in outstanding)


def test_checklist_is_empty_for_a_working_install(inventory):
    record = installed(inventory, "DEV-001")

    assert installation_checklist(record) == []


def test_checklist_reports_a_terminal_device(inventory):
    inventory.retire("DEV-001")

    assert any("retired" in item for item in installation_checklist(inventory.get("DEV-001")))


def test_stale_installations_are_surfaced(inventory):
    installed(inventory, "DEV-001")

    fresh = stale_installations(inventory, max_age_days=90, now=BASE + timedelta(days=30))
    stale = stale_installations(inventory, max_age_days=90, now=BASE + timedelta(days=200))

    assert fresh == ()
    assert [r.device_id for r in stale] == ["DEV-001"]


def test_uninstalled_devices_are_not_stale(inventory):
    installed(inventory, "DEV-001")
    inventory.uninstall("DEV-001")

    assert stale_installations(inventory, max_age_days=1, now=BASE + timedelta(days=999)) == ()


# --- persistence ------------------------------------------------------------


def test_inventory_round_trips_through_disk(tmp_path, inventory):
    installed(inventory, "DEV-001")
    path = tmp_path / "inventory.json"

    inventory.save(path)
    loaded = FleetInventory.load(path)

    assert len(loaded) == 1
    assert loaded.get("DEV-001").vehicle_id == "VEH-A"
    assert loaded.get("DEV-001").state == "installed"


def test_loading_an_absent_inventory_returns_an_empty_one(tmp_path):
    assert len(FleetInventory.load(tmp_path / "absent.json")) == 0


def test_loading_rejects_an_unknown_schema_version(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"schemaVersion": 99, "devices": []}), encoding="utf-8")

    with pytest.raises(InventoryError, match="schema version"):
        FleetInventory.load(path)


def test_loading_rejects_a_malformed_record(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"schemaVersion": 1, "devices": [{"deviceId": "DEV-001"}]}),
        encoding="utf-8",
    )

    with pytest.raises(InventoryError):
        FleetInventory.load(path)
