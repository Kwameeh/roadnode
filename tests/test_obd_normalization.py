from dataclasses import replace

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
