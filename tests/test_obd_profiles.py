import pytest

from car_telemetry.bluetooth import parse_rfcomm_bindings, parse_rfcomm_channel
from car_telemetry.config import set_env_values, settings
from car_telemetry.obd_profiles import (
    PROFILES,
    configured_target,
    get_profile,
    match_profile,
    profile_env_values,
)


SP_SEARCH_ANDROID = """Searching for SP on EC:46:2C:93:7E:F4 ...
Service Name: ELM327 Emulator
Service RecHandle: 0x10009
Service Class ID List:
  "Serial Port" (0x1101)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 7
"""

BROWSE_PHYSICAL = """Browsing 00:10:CC:4F:36:03 ...
Service Name: OBEX Object Push
Service Class ID List:
  "OBEX Object Push" (0x1105)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 12

Service Name: JL_SPP
Service Class ID List:
  "Serial Port" (0x1101)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 1
"""


def test_known_profiles_match_tested_adapters():
    assert get_profile("OBD2").mac == "00:10:CC:4F:36:03"
    assert get_profile("obd2").channel == 1
    assert get_profile("obd2").pins[:2] == ("1234", "1111")
    assert get_profile("android").mac == "EC:46:2C:93:7E:F4"
    assert get_profile("android").channel == 7
    assert get_profile("android").require_service


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError):
        get_profile("usb")


def test_sp_search_output_yields_channel():
    assert parse_rfcomm_channel(SP_SEARCH_ANDROID, require_serial_port=False) == 7


def test_browse_skips_non_serial_rfcomm_services():
    assert parse_rfcomm_channel(BROWSE_PHYSICAL) == 1


def test_no_rfcomm_service_returns_none():
    assert parse_rfcomm_channel("Searching for SP on EC:46:2C:93:7E:F4 ...\n", require_serial_port=False) is None


def test_parse_rfcomm_bindings():
    assert parse_rfcomm_bindings("rfcomm0: ec:46:2c:93:7e:f4 channel 7 clean \n") == {
        "rfcomm0": {"mac": "EC:46:2C:93:7E:F4", "channel": 7, "state": "clean"},
    }


def test_switch_rewrites_candidates_so_link_service_binds_new_device(monkeypatch, tmp_path):
    env = tmp_path / "telemetry.env"
    env.write_text(
        "OBD_TRANSPORT=auto\n"
        "OBD_MAC=00:10:CC:4F:36:03\n"
        "OBD_BLUETOOTH_CANDIDATES=00:10:CC:4F:36:03@1,EC:46:2C:93:7E:F4\n"
        "OBD_RFCOMM_CHANNEL=1\n"
        "OBD_BAUD=auto\n",
        encoding="utf-8",
    )
    for key in ("OBD_TRANSPORT", "OBD_MAC", "OBD_BLUETOOTH_CANDIDATES", "OBD_RFCOMM_CHANNEL", "OBD_ENABLED", "OBD_BLUETOOTH_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEMETRY_ENV", str(env))

    set_env_values(profile_env_values(PROFILES["android"], 7), str(env))

    text = env.read_text(encoding="utf-8")
    assert "OBD_BLUETOOTH_CANDIDATES=EC:46:2C:93:7E:F4@7\n" in text
    assert "OBD_TRANSPORT=bluetooth\n" in text
    assert "OBD_BAUD=auto\n" in text
    s = settings(str(env))
    assert configured_target(s) == ("EC:46:2C:93:7E:F4", 7)
    assert match_profile(configured_target(s)[0]) is PROFILES["android"]


def test_profile_env_rejects_invalid_channel():
    with pytest.raises(ValueError):
        profile_env_values(PROFILES["obd2"], 0)
