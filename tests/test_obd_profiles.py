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


# --- saved profiles and switching by MAC ---------------------------------------

from dataclasses import replace as dc_replace

from car_telemetry import cli, obd_profiles
from car_telemetry.obd_profiles import (
    DEFAULT_PINS,
    SwitchFailed,
    all_profiles,
    load_saved_profiles,
    make_profile,
    remove_profile,
    save_profile,
    validate_name,
)


def profile_settings(tmp_path):
    return dc_replace(settings(str(tmp_path / "missing.env")), obd_profiles_file=str(tmp_path / "profiles.json"))


def test_saved_profile_round_trips_and_is_listed_with_builtins(tmp_path):
    s = profile_settings(tmp_path)
    save_profile(s.obd_profiles_file, make_profile("aa:bb:cc:dd:ee:01", name="MyCar", channel=2, pins=["6789"]))
    save_profile(s.obd_profiles_file, make_profile("AA:BB:CC:DD:EE:02", name="spare"))

    saved = load_saved_profiles(s.obd_profiles_file)
    assert saved["mycar"].mac == "AA:BB:CC:DD:EE:01"
    assert saved["mycar"].channel == 2
    assert saved["mycar"].pins == ("6789",)
    assert saved["spare"].channel is None
    assert saved["spare"].pins == DEFAULT_PINS
    assert set(all_profiles(s)) == {"obd2", "android", "mycar", "spare"}

    assert remove_profile(s.obd_profiles_file, "MyCar") is True
    assert remove_profile(s.obd_profiles_file, "mycar") is False
    assert set(load_saved_profiles(s.obd_profiles_file)) == {"spare"}


@pytest.mark.parametrize("name", ["obd2", "android", "list", "current", "add", "remove", "has space", "-dash", "x" * 33])
def test_profile_names_cannot_collide_with_builtins_or_commands(name):
    with pytest.raises(ValueError):
        validate_name(name)


def test_bad_entries_in_the_profiles_file_are_skipped(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        '{"profiles": {"good": {"mac": "AA:BB:CC:DD:EE:03", "channel": 3},'
        ' "badmac": {"mac": "nope"}, "badchannel": {"mac": "AA:BB:CC:DD:EE:04", "channel": 99}}}',
        encoding="utf-8",
    )
    assert set(load_saved_profiles(path)) == {"good"}


def run_cli(monkeypatch, tmp_path, argv):
    calls = {}
    s = profile_settings(tmp_path)
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "EngineAPI", lambda *args, **kwargs: None)

    def fake_switch(settings_, target, channel=None, reboot=False, verify_seconds=45, save_as=None):
        calls.update(target=target, channel=channel, save_as=save_as)
        return 0

    monkeypatch.setattr(cli, "switch_profile", fake_switch)
    monkeypatch.setattr("sys.argv", ["telemetry", "obd-profile", *argv])
    return cli.main(), calls, s


def test_cli_switches_by_mac_with_discovery_and_saves_under_a_name(monkeypatch, tmp_path):
    code, calls, _ = run_cli(monkeypatch, tmp_path, ["--mac", "aa:bb:cc:dd:ee:10", "--name", "work-van"])
    assert code == 0
    assert calls["target"].mac == "AA:BB:CC:DD:EE:10"
    assert calls["target"].channel is None
    assert calls["channel"] is None
    assert calls["save_as"] == "work-van"


def test_cli_switches_by_mac_with_a_given_channel_without_saving(monkeypatch, tmp_path):
    code, calls, _ = run_cli(monkeypatch, tmp_path, ["--mac", "AA:BB:CC:DD:EE:11", "--channel", "4"])
    assert code == 0
    assert calls["channel"] == 4
    assert calls["save_as"] is None


def test_cli_add_then_switch_by_saved_name(monkeypatch, tmp_path):
    code, _, s = run_cli(monkeypatch, tmp_path, ["add", "mycar", "--mac", "AA:BB:CC:DD:EE:12", "--channel", "5"])
    assert code == 0
    assert load_saved_profiles(s.obd_profiles_file)["mycar"].channel == 5

    code, calls, _ = run_cli(monkeypatch, tmp_path, ["mycar"])
    assert code == 0
    assert calls["target"] == "mycar"


def test_cli_rejects_name_without_mac(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, tmp_path, ["--name", "mycar"])


def test_switch_to_unknown_name_explains_how_to_use_a_mac(tmp_path, capsys):
    s = profile_settings(tmp_path)
    assert obd_profiles.switch_profile(s, "nothing-here") == 2
    assert "--mac" in capsys.readouterr().out


def test_release_refuses_to_continue_while_rfcomm0_is_still_held(monkeypatch):
    commands = []
    monkeypatch.setattr(obd_profiles, "_root_run", lambda command, timeout=30: commands.append(command) or (0, "", "/dev/rfcomm0: kwameeh 812 F.... ModemManager"))
    monkeypatch.setattr(obd_profiles.bluetooth, "rfcomm_bindings", lambda: {"rfcomm0": {"mac": "EC:46:2C:93:7E:F4", "channel": 7, "state": "connected"}})
    monkeypatch.setattr(obd_profiles.time, "sleep", lambda _seconds: None)

    with pytest.raises(SwitchFailed) as failure:
        obd_profiles._release_rfcomm(attempts=3)

    assert commands.count(["sudo", "rfcomm", "release", "all"]) + commands.count(["rfcomm", "release", "all"]) == 3
    assert "ModemManager" in str(failure.value)
    assert "EC:46:2C:93:7E:F4" in str(failure.value)


def test_release_succeeds_once_the_binding_is_gone(monkeypatch):
    bindings = [{"rfcomm0": {"mac": "EC:46:2C:93:7E:F4", "channel": 7, "state": "closed"}}, {}]
    monkeypatch.setattr(obd_profiles, "_root_run", lambda command, timeout=30: (0, "", ""))
    monkeypatch.setattr(obd_profiles.bluetooth, "rfcomm_bindings", lambda: bindings.pop(0))
    monkeypatch.setattr(obd_profiles.time, "sleep", lambda _seconds: None)
    obd_profiles._release_rfcomm()


def test_channel_is_required_when_discovery_fails_and_none_is_saved(monkeypatch):
    monkeypatch.setattr(obd_profiles.bluetooth, "search_serial_channel", lambda mac: None)
    with pytest.raises(SwitchFailed):
        obd_profiles._resolve_channel(make_profile("AA:BB:CC:DD:EE:13"), None)
    saved = make_profile("AA:BB:CC:DD:EE:13", name="known", channel=3)
    assert obd_profiles._resolve_channel(saved, None) == 3
    assert obd_profiles._resolve_channel(make_profile("AA:BB:CC:DD:EE:13"), 6) == 6
