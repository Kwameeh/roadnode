from dataclasses import replace
from contextlib import nullcontext
from types import SimpleNamespace

from car_telemetry.config import settings
from car_telemetry.obd_service import OBDService
from car_telemetry.observations import ObservationStore
from car_telemetry.state import DeviceState


class Quantity:
    def __init__(self, magnitude, units="revolutions_per_minute"):
        self.magnitude = magnitude
        self.units = units

    def to(self, unit):
        return Quantity(self.magnitude, unit)


class Response:
    def __init__(self, value=None, null=False):
        self.value = value
        self.null = null

    def is_null(self):
        return self.null


def test_dtc_scan_publishes_successful_scopes_and_never_claims_no_data_is_a_clear(tmp_path, monkeypatch):
    class Commands(dict):
        FREEZE_DTC = "freeze"

    fake_obd = SimpleNamespace(commands=Commands(GET_DTC="stored", GET_CURRENT_DTC="pending"),
        OBD=SimpleNamespace(query=lambda connection, command, force=False: connection.query(command, force=force)))
    monkeypatch.setattr("car_telemetry.obd_service.obd", fake_obd)
    values = {"stored": [("P0300", "Misfire")], "pending": None, "freeze": []}
    connection = SimpleNamespace(paused=nullcontext, supports=lambda command: True,
        query=lambda command, force=False: Response(values[command], null=values[command] is None))
    observations = ObservationStore()
    service = OBDService(replace(settings(), vehicle_profile_dir=str(tmp_path)), DeviceState("DEV-001", "VEH-001", 1), observations)
    service.connection = connection
    service.refresh_dtcs()
    scan = observations.snapshot("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z").obd["dtc"]
    assert scan["stored"] == ["P0300"]
    assert "pending" not in scan
    assert scan["quality"] == "valid"
    values["stored"] = None
    service.refresh_dtcs()
    scan = observations.snapshot("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z").obd["dtc"]
    assert scan["quality"] == "invalid"
    assert "stored" not in scan and "pending" not in scan
    values["stored"] = []
    values["pending"] = []
    service.refresh_dtcs()
    scan = observations.snapshot("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z").obd["dtc"]
    assert scan["stored"] == scan["pending"] == []
    assert scan["quality"] == "valid"


def test_obd_callback_normalizes_timestamp_unit_source_and_engine_state(tmp_path):
    configured = replace(settings(), vehicle_profile_dir=str(tmp_path))
    state = DeviceState("DEV-001", "VEH-001", 1)
    observations = ObservationStore()
    service = OBDService(configured, state, observations)

    callback = service._callback("RPM", {"description": "Engine RPM"})
    callback(Response(Quantity(1800)))

    snapshot = observations.snapshot(
        "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"
    )
    rpm = snapshot.obd["signals"]["RPM"]
    assert rpm["value"] == 1800
    assert rpm["unit"] == "rpm"
    assert rpm["source"] == "obd.pid"
    assert rpm["observedAt"].endswith("Z")
    assert snapshot.obd["engineOn"] is True


def test_null_obd_read_does_not_replace_last_valid_observation(tmp_path):
    configured = replace(settings(), vehicle_profile_dir=str(tmp_path))
    state = DeviceState("DEV-001", "VEH-001", 1)
    observations = ObservationStore()
    service = OBDService(configured, state, observations)
    callback = service._callback("SPEED", {})
    callback(Response(61))
    callback(Response(null=True))

    snapshot = observations.snapshot(
        "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"
    )
    assert snapshot.obd["signals"]["SPEED"]["value"] == 61
    assert state.snapshot()["obd"]["signals"]["SPEED"]["value"] is None
