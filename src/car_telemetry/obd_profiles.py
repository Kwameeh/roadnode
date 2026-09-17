"""Named Bluetooth OBD profiles and the `telemetry obd-profile` switch sequence.

The switcher only prepares the Bluetooth side (pair, trust, SPP channel,
telemetry.env, /dev/rfcomm0). python-OBD in the engine still owns the ELM327
conversation, so a switch ends by restarting the engine and watching its status.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import bluetooth
from .common import Check, print_check, read_json, run
from .config import Settings, find_env, set_env_values

ENGINE_SERVICE = "car-telemetry.service"
LINK_SERVICE = "car-telemetry-obd-link.service"
RFCOMM_DEVICE = "rfcomm0"
BLUETOOTH_PORT = f"/dev/{RFCOMM_DEVICE}"


@dataclass(frozen=True)
class ObdProfile:
    id: str
    name: str
    mac: str
    channel: int
    # PINs tried in order when the device is not paired yet. Empty means the
    # device confirms pairing itself (a phone shows a passkey prompt).
    pins: tuple[str, ...] = ()
    # When SDP finds no Serial Port service: True aborts before anything is
    # stopped, False falls back to the known-good channel.
    require_service: bool = False
    service_hint: str = ""


PROFILES: dict[str, ObdProfile] = {
    profile.id: profile
    for profile in (
        ObdProfile(
            id="obd2",
            name="OBDII / Physical ELM327",
            mac="00:10:CC:4F:36:03",
            channel=1,
            # ELM327 clones ship with 1234 or 1111; 0000 is a last resort.
            pins=("1234", "1111", "0000"),
            service_hint="Make sure the adapter is plugged into a powered OBD-II port.",
        ),
        ObdProfile(
            id="android",
            name="Android ELM327 Emulator",
            mac="EC:46:2C:93:7E:F4",
            channel=7,
            require_service=True,
            service_hint="Start the ELM327 Emulator app/server on the phone.",
        ),
    )
}


def get_profile(profile_id: str) -> ObdProfile:
    try:
        return PROFILES[profile_id.strip().lower()]
    except KeyError:
        raise ValueError(f"Unknown OBD profile '{profile_id}'. Choose one of: {', '.join(PROFILES)}") from None


def profile_env_values(profile: ObdProfile, channel: int) -> dict[str, str]:
    channel = bluetooth.validate_channel(channel)
    return {
        "OBD_ENABLED": "true",
        "OBD_TRANSPORT": "bluetooth",
        "OBD_BLUETOOTH_PORT": BLUETOOTH_PORT,
        "OBD_MAC": profile.mac,
        # obd-link.sh binds from the candidate list before it looks at OBD_MAC,
        # so a stale list here would keep rebinding the previous device.
        "OBD_BLUETOOTH_CANDIDATES": f"{profile.mac}@{channel}",
        "OBD_RFCOMM_CHANNEL": str(channel),
    }


def configured_target(s: Settings) -> tuple[str, int]:
    """The MAC and channel obd-link.sh will actually bind first."""
    try:
        candidates = bluetooth.parse_candidates(s.obd_bluetooth_candidates, s.obd_mac, s.obd_rfcomm_channel)
    except ValueError:
        candidates = []
    if candidates:
        first = candidates[0]
        return first.mac, first.channel if first.channel is not None else s.obd_rfcomm_channel
    return s.obd_mac, s.obd_rfcomm_channel


def match_profile(mac: str) -> ObdProfile | None:
    return next((p for p in PROFILES.values() if p.mac == mac.strip().upper()), None)


def _as_root(command: list[str]) -> list[str]:
    geteuid: Callable[[], int] | None = getattr(os, "geteuid", None)
    return command if geteuid is not None and geteuid() == 0 else ["sudo", *command]


def _root_run(command: list[str], timeout: float = 30) -> tuple[int, str, str]:
    return run(_as_root(command), timeout)


class SwitchFailed(RuntimeError):
    pass


def _ok(name: str, detail: str) -> None:
    print_check(Check(name, "OK", detail))


def _warn(name: str, detail: str) -> None:
    print_check(Check(name, "WARN", detail))


def _fail(name: str, detail: str) -> SwitchFailed:
    print_check(Check(name, "FAIL", detail))
    return SwitchFailed(detail)


def print_profiles(s: Settings) -> None:
    active_mac, _ = configured_target(s)
    print("Available OBD Bluetooth profiles\n")
    for profile in PROFILES.values():
        marker = "  (active)" if profile.mac == active_mac else ""
        print(f"  {profile.id}{marker}")
        print(f"    Device:  {profile.name}")
        print(f"    MAC:     {profile.mac}")
        print(f"    RFCOMM:  {profile.channel}")
        if profile.pins:
            print(f"    PINs:    {', '.join(profile.pins)}")
        print()


def print_current(s: Settings) -> int:
    mac, channel = configured_target(s)
    profile = match_profile(mac) if mac else None
    print("RoadNode OBD Profile\n")
    print(f"Profile:   {profile.id if profile else 'custom' if mac else 'none'}")
    print(f"Transport: {s.obd_transport}")
    print(f"MAC:       {mac or '-'}")
    print(f"RFCOMM:    {channel}")
    print(f"Port:      {s.obd_bluetooth_port}")
    print()

    if not mac:
        return 1
    info = bluetooth.device_info(mac)
    link = "connected" if info["connected"] else "paired" if info["paired"] else "not paired"
    print(f"Bluetooth: {link}{'' if info['trusted'] else ' (not trusted)'}")

    binding = bluetooth.rfcomm_bindings().get(RFCOMM_DEVICE)
    if binding is None:
        print(f"RFCOMM:    {RFCOMM_DEVICE} not bound")
    elif binding["mac"] == mac and binding["channel"] == channel:
        print(f"RFCOMM:    ready ({binding['state'] or 'bound'})")
    else:
        print(f"RFCOMM:    STALE - bound to {binding['mac']} channel {binding['channel']}")

    obd = (read_json(s.status_file) or {}).get("obd", {})
    if obd.get("connected"):
        print(f"ELM327:    responding ({obd.get('protocolName') or obd.get('status') or 'connected'})")
    else:
        print(f"ELM327:    not connected{' - ' + obd['error'] if obd.get('error') else ''}")
    return 0


def _prepare_bluetooth(profile: ObdProfile) -> None:
    _root_run(["systemctl", "enable", "--now", "bluetooth.service"])
    bluetooth.power_on()
    controller = bluetooth.controller_status()
    if not controller.get("available") or not controller.get("powered"):
        raise _fail("Bluetooth controller", str(controller.get("error") or "controller is not powered"))
    _ok("Bluetooth controller", "ready")

    if not bluetooth.device_known(profile.mac):
        print(f"       Scanning for {profile.mac} ...")
        bluetooth.scan(8)

    info = bluetooth.device_info(profile.mac)
    if info["paired"]:
        _ok("Device paired", info["name"])
    else:
        errors: list[str] = []
        for pin in profile.pins or (None,):
            try:
                info = bluetooth.pair(profile.mac, pin)
                if info.get("paired"):
                    break
            except Exception as exc:
                errors.append(f"PIN {pin}: {exc}" if pin else str(exc))
        if not info.get("paired"):
            detail = "; ".join(errors) or "pairing did not complete"
            raise _fail("Device paired", f"{detail}. {profile.service_hint}".strip())
        _ok("Device paired", info["name"])

    if not info["trusted"] and not bluetooth.trust(profile.mac):
        raise _fail("Device trusted", f"bluetoothctl trust {profile.mac} failed")
    _ok("Device trusted", profile.mac)


def _resolve_channel(profile: ObdProfile, forced: int | None) -> int:
    if forced is not None:
        channel = bluetooth.validate_channel(forced)
        _ok("RFCOMM channel", f"{channel} (--channel override)")
        return channel

    try:
        discovered = bluetooth.search_serial_channel(profile.mac)
        problem = "no Serial Port service advertised"
    except RuntimeError as exc:
        discovered = None
        problem = str(exc)

    if discovered is not None:
        _ok("Serial Port discovered", f"RFCOMM channel {discovered}")
        if discovered != profile.channel:
            _warn("RFCOMM channel", f"device now uses {discovered}, profile default is {profile.channel}")
        return discovered

    if profile.require_service:
        raise _fail(
            "Serial Port discovered",
            f"{profile.name} was found but {problem}. {profile.service_hint} "
            f"Then run: telemetry obd-profile {profile.id}",
        )
    _warn("Serial Port discovered", f"{problem}; using known channel {profile.channel}")
    return profile.channel


def _wait_for_binding(profile: ObdProfile, channel: int, seconds: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + seconds
    binding = None
    while time.monotonic() < deadline:
        binding = bluetooth.rfcomm_bindings().get(RFCOMM_DEVICE)
        if binding and Path(BLUETOOTH_PORT).exists():
            return binding
        time.sleep(1)
    return binding


def _wait_for_elm(s: Settings, since: float, seconds: float) -> dict[str, Any]:
    status = Path(s.status_file)
    deadline = time.monotonic() + seconds
    obd: dict[str, Any] = {}
    while time.monotonic() < deadline:
        # Only trust a status file the restarted engine wrote, not the old one.
        if status.exists() and status.stat().st_mtime >= since:
            obd = (read_json(s.status_file) or {}).get("obd", {})
            if obd.get("connected"):
                return obd
        time.sleep(2)
    return obd


def switch_profile(
    s: Settings,
    profile_id: str,
    channel: int | None = None,
    reboot: bool = False,
    verify_seconds: float = 45,
) -> int:
    profile = get_profile(profile_id)
    print("RoadNode OBD Profile Switch")
    print("---------------------------")
    print(f"Profile        {profile.name} ({profile.id})")
    print(f"MAC            {profile.mac}")
    print(f"Saved channel  {profile.channel}\n")

    try:
        env_path = find_env()
        if env_path is None:
            raise _fail(
                "telemetry.env",
                "not found. Run without sudo from the RoadNode user, or set TELEMETRY_ENV.",
            )

        # Everything above the service stop is read-only: a failure here leaves
        # the currently running profile untouched.
        _prepare_bluetooth(profile)
        channel = _resolve_channel(profile, channel)

        _root_run(["systemctl", "stop", ENGINE_SERVICE])
        _ok("Telemetry stopped", ENGINE_SERVICE)
        _root_run(["systemctl", "stop", LINK_SERVICE])
        _root_run(["rfcomm", "release", "all"])
        _ok("Old RFCOMM released", "rfcomm release all")

        set_env_values(profile_env_values(profile, channel), str(env_path))
        _ok("telemetry.env updated", str(env_path))

        if reboot:
            _ok("Reboot", "the OBD link service will bind the new profile at boot")
            _root_run(["systemctl", "reboot"])
            return 0

        _root_run(["systemctl", "start", LINK_SERVICE])
        _ok("OBD link restarted", LINK_SERVICE)

        binding = _wait_for_binding(profile, channel, 20)
        link_ok = bool(binding and binding["mac"] == profile.mac and binding["channel"] == channel)
        if link_ok:
            _ok(BLUETOOTH_PORT, f"{profile.mac} channel {channel}")
        elif binding:
            _fail(BLUETOOTH_PORT, f"bound to {binding['mac']} channel {binding['channel']}, expected {profile.mac} channel {channel}")
        else:
            _fail(BLUETOOTH_PORT, f"not created. Check: journalctl -u {LINK_SERVICE} -n 50")

        # Restart the engine even after a link failure so the Pi is never left
        # with telemetry stopped.
        started = time.time()
        _root_run(["systemctl", "start", ENGINE_SERVICE])
        _ok("Telemetry restarted", ENGINE_SERVICE)
        if not link_ok:
            return 1

        if verify_seconds > 0:
            print(f"       Waiting up to {int(verify_seconds)}s for python-OBD ...")
            obd = _wait_for_elm(s, started, verify_seconds)
            if obd.get("connected"):
                _ok("ELM327 responding", str(obd.get("protocolName") or obd.get("status") or "connected"))
            else:
                _warn(
                    "ELM327 responding",
                    f"not yet ({obd.get('error') or 'no status'}). {profile.service_hint} "
                    "Check again with: telemetry obd-profile current",
                )
    except SwitchFailed:
        return 1

    print(f"\nActive profile: {profile.id}")
    return 0
