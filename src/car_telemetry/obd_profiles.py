"""Named Bluetooth OBD profiles and the `telemetry obd-profile` switch sequence.

The switcher only prepares the Bluetooth side (pair, trust, SPP channel,
telemetry.env, /dev/rfcomm0). python-OBD in the engine still owns the ELM327
conversation, so a switch ends by restarting the engine and watching its status.

Two profiles are built in. Any other adapter can be switched to by MAC address
and saved under a name (`OBD_PROFILES_FILE`) to be used by name next time.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import bluetooth
from .common import Check, print_check, read_json, run
from .config import Settings, find_env, set_env_values

ENGINE_SERVICE = "car-telemetry.service"
LINK_SERVICE = "car-telemetry-obd-link.service"
RFCOMM_DEVICE = "rfcomm0"
BLUETOOTH_PORT = f"/dev/{RFCOMM_DEVICE}"

# ELM327 clones ship with 1234 or 1111; 0000 is a last resort. A phone ignores
# the PIN and asks for confirmation instead.
DEFAULT_PINS = ("1234", "1111", "0000")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED_NAMES = {"list", "current", "add", "remove", "use"}


@dataclass(frozen=True)
class ObdProfile:
    id: str
    name: str
    mac: str
    # None means "discover it with SDP every time".
    channel: int | None
    # PINs tried in order when the device is not paired yet.
    pins: tuple[str, ...] = DEFAULT_PINS
    # When SDP finds no Serial Port service: True aborts before anything is
    # stopped, False falls back to the saved channel.
    require_service: bool = False
    service_hint: str = ""
    builtin: bool = False


BUILTIN_PROFILES: dict[str, ObdProfile] = {
    profile.id: profile
    for profile in (
        ObdProfile(
            id="obd2",
            name="OBDII / Physical ELM327",
            mac="00:10:CC:4F:36:03",
            channel=1,
            service_hint="Make sure the adapter is plugged into a powered OBD-II port.",
            builtin=True,
        ),
        ObdProfile(
            id="android",
            name="Android ELM327 Emulator",
            mac="EC:46:2C:93:7E:F4",
            channel=7,
            pins=(),
            require_service=True,
            service_hint="Start the ELM327 Emulator app/server on the phone.",
            builtin=True,
        ),
    )
}
# Kept for callers that only need the built-in names.
PROFILES = BUILTIN_PROFILES


# --- saved profiles -----------------------------------------------------------


def validate_name(name: str) -> str:
    value = name.strip().lower()
    if not PROFILE_NAME_RE.fullmatch(value):
        raise ValueError("Profile names use 1-32 lower-case letters, digits, '-' or '_', starting with a letter or digit")
    if value in RESERVED_NAMES:
        raise ValueError(f"'{value}' is a command, not a profile name")
    if value in BUILTIN_PROFILES:
        raise ValueError(f"'{value}' is a built-in profile; choose another name")
    return value


def load_saved_profiles(path: str | Path) -> dict[str, ObdProfile]:
    file = Path(path).expanduser()
    if not file.exists():
        return {}
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{file} is not valid JSON: {exc}") from exc

    profiles: dict[str, ObdProfile] = {}
    for key, raw in (document.get("profiles") or {}).items():
        try:
            profile_id = validate_name(key)
            channel = raw.get("channel")
            profiles[profile_id] = ObdProfile(
                id=profile_id,
                name=str(raw.get("name") or profile_id),
                mac=bluetooth.validate_mac(str(raw.get("mac", ""))),
                channel=None if channel in (None, "") else bluetooth.validate_channel(channel),
                pins=tuple(str(pin) for pin in raw.get("pins", DEFAULT_PINS)),
            )
        except (ValueError, TypeError, AttributeError):
            # One bad entry must not hide the others.
            continue
    return profiles


def _write_saved_profiles(path: str | Path, profiles: dict[str, ObdProfile]) -> Path:
    file = Path(path).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "profiles": {
            profile.id: {
                "name": profile.name,
                "mac": profile.mac,
                "channel": profile.channel,
                "pins": list(profile.pins),
            }
            for profile in sorted(profiles.values(), key=lambda item: item.id)
        }
    }
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(file)
    return file


def save_profile(path: str | Path, profile: ObdProfile) -> Path:
    profiles = load_saved_profiles(path)
    profiles[profile.id] = replace(profile, builtin=False, require_service=False, service_hint="")
    return _write_saved_profiles(path, profiles)


def remove_profile(path: str | Path, name: str) -> bool:
    profiles = load_saved_profiles(path)
    if profiles.pop(name.strip().lower(), None) is None:
        return False
    _write_saved_profiles(path, profiles)
    return True


def make_profile(
    mac: str,
    *,
    name: str | None = None,
    channel: int | None = None,
    pins: list[str] | tuple[str, ...] | None = None,
    label: str | None = None,
) -> ObdProfile:
    mac = bluetooth.validate_mac(mac)
    profile_id = validate_name(name) if name else mac.replace(":", "").lower()
    return ObdProfile(
        id=profile_id,
        name=label or (profile_id if name else f"Bluetooth device {mac}"),
        mac=mac,
        channel=None if channel is None else bluetooth.validate_channel(channel),
        pins=tuple(pins) if pins else DEFAULT_PINS,
    )


def all_profiles(s: Settings) -> dict[str, ObdProfile]:
    try:
        saved = load_saved_profiles(s.obd_profiles_file)
    except ValueError:
        saved = {}
    return {**BUILTIN_PROFILES, **saved}


def get_profile(profile_id: str, profiles: dict[str, ObdProfile] | None = None) -> ObdProfile:
    available = profiles if profiles is not None else BUILTIN_PROFILES
    try:
        return available[profile_id.strip().lower()]
    except KeyError:
        raise ValueError(
            f"Unknown OBD profile '{profile_id}'. Choose one of: {', '.join(available)}, "
            "or switch by address with --mac AA:BB:CC:DD:EE:FF"
        ) from None


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


def match_profile(mac: str, profiles: dict[str, ObdProfile] | None = None) -> ObdProfile | None:
    available = profiles if profiles is not None else BUILTIN_PROFILES
    return next((p for p in available.values() if p.mac == mac.strip().upper()), None)


# --- output helpers -----------------------------------------------------------


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
    profiles = all_profiles(s)
    print("Available OBD Bluetooth profiles\n")
    for profile in profiles.values():
        tags = [tag for tag in ("built-in" if profile.builtin else "saved", "active" if profile.mac == active_mac else "") if tag]
        print(f"  {profile.id}  ({', '.join(tags)})")
        print(f"    Device:  {profile.name}")
        print(f"    MAC:     {profile.mac}")
        print(f"    RFCOMM:  {profile.channel if profile.channel is not None else 'auto (discovered each switch)'}")
        if profile.pins:
            print(f"    PINs:    {', '.join(profile.pins)}")
        print()
    print(f"Saved profiles file: {Path(s.obd_profiles_file).expanduser()}")
    print("Add one:  telemetry obd-profile add NAME --mac AA:BB:CC:DD:EE:FF [--channel N]")


def print_current(s: Settings) -> int:
    mac, channel = configured_target(s)
    profile = match_profile(mac, all_profiles(s)) if mac else None
    print("RoadNode OBD Profile\n")
    print(f"Profile:   {profile.id if profile else 'unsaved' if mac else 'none'}")
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
    elif obd.get("connecting"):
        print("ELM327:    connecting (python-OBD is still talking to the adapter)")
    else:
        print(f"ELM327:    not connected{' - ' + obd['error'] if obd.get('error') else ''}")
    return 0


# --- switch sequence ----------------------------------------------------------


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
        if not bluetooth.device_known(profile.mac):
            raise _fail(
                "Device found",
                f"{profile.mac} is not in range or not discoverable. {profile.service_hint}".strip(),
            )

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
        _ok("RFCOMM channel", f"{channel} (--channel)")
        return channel

    try:
        discovered = bluetooth.search_serial_channel(profile.mac)
        problem = "no Serial Port service advertised"
    except RuntimeError as exc:
        discovered = None
        problem = str(exc)

    if discovered is not None:
        _ok("Serial Port discovered", f"RFCOMM channel {discovered}")
        if profile.channel is not None and discovered != profile.channel:
            _warn("RFCOMM channel", f"device now uses {discovered}, profile default is {profile.channel}")
        return discovered

    if profile.require_service or profile.channel is None:
        retry = f"telemetry obd-profile {profile.id}" if profile.builtin else f"telemetry obd-profile --mac {profile.mac} --channel N"
        raise _fail(
            "Serial Port discovered",
            f"{profile.name}: {problem}. {profile.service_hint} "
            f"Check `sdptool browse {profile.mac}` for the channel, then run: {retry}".replace("  ", " "),
        )
    _warn("Serial Port discovered", f"{problem}; using saved channel {profile.channel}")
    return profile.channel


def _release_rfcomm(attempts: int = 5) -> None:
    """Release every RFCOMM binding and confirm rfcomm0 is really gone.

    The kernel refuses to release a device another process still has open, and
    obd-link.sh never rebinds an existing rfcomm0, so carrying on would leave
    the old adapter in place.
    """
    for _ in range(attempts):
        _root_run(["rfcomm", "release", "all"])
        binding = bluetooth.rfcomm_bindings().get(RFCOMM_DEVICE)
        if binding is None:
            _ok("Old RFCOMM released", "rfcomm release all")
            return
        time.sleep(1)
    _, out, err = _root_run(["fuser", "-v", BLUETOOTH_PORT], 5)
    holders = " ".join((out + " " + err).split()) or "unknown process"
    raise _fail(
        "Old RFCOMM released",
        f"{BLUETOOTH_PORT} is still bound to {binding['mac']} channel {binding['channel']} "
        f"and held open by: {holders}. Stop that process (ModemManager: "
        "sudo systemctl disable --now ModemManager) and switch again.",
    )


def _wait_for_binding(channel: int, seconds: float) -> dict[str, Any] | None:
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


def _restart_services() -> None:
    _root_run(["systemctl", "start", LINK_SERVICE])
    _root_run(["systemctl", "start", ENGINE_SERVICE])


def switch_profile(
    s: Settings,
    target: str | ObdProfile,
    channel: int | None = None,
    reboot: bool = False,
    verify_seconds: float = 45,
    save_as: str | None = None,
) -> int:
    """Switch to a profile id, or to an ad-hoc profile; `save_as` stores it on success."""
    try:
        profile = target if isinstance(target, ObdProfile) else get_profile(target, all_profiles(s))
    except ValueError as exc:
        print(exc)
        return 2

    print("RoadNode OBD Profile Switch")
    print("---------------------------")
    print(f"Profile        {profile.name} ({profile.id})")
    print(f"MAC            {profile.mac}")
    print(f"Saved channel  {profile.channel if profile.channel is not None else 'auto'}\n")

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

        if save_as:
            saved = replace(profile, id=save_as, channel=channel)
            if profile.name == f"Bluetooth device {profile.mac}":
                saved = replace(saved, name=save_as)
            path = save_profile(s.obd_profiles_file, saved)
            profile = saved
            _ok("Profile saved", f"{save_as} -> {path} (next time: telemetry obd-profile {save_as})")

        _root_run(["systemctl", "stop", ENGINE_SERVICE])
        _ok("Telemetry stopped", ENGINE_SERVICE)
        _root_run(["systemctl", "stop", LINK_SERVICE])
        try:
            _release_rfcomm()
        except SwitchFailed:
            # Nothing was changed yet: bring the previous profile back up.
            _restart_services()
            raise

        set_env_values(profile_env_values(profile, channel), str(env_path))
        _ok("telemetry.env updated", str(env_path))

        if reboot:
            _ok("Reboot", "the OBD link service will bind the new profile at boot")
            _root_run(["systemctl", "reboot"])
            return 0

        _root_run(["systemctl", "start", LINK_SERVICE])
        _ok("OBD link restarted", LINK_SERVICE)

        binding = _wait_for_binding(channel, 20)
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
                state = "still connecting" if obd.get("connecting") else obd.get("error") or "no status"
                _warn(
                    "ELM327 responding",
                    f"not yet ({state}). {profile.service_hint} "
                    "Check again with: telemetry obd-profile current",
                )
    except SwitchFailed:
        return 1

    print(f"\nActive profile: {profile.id}")
    return 0


def add_profile(s: Settings, name: str, mac: str, channel: int | None, pins: list[str] | None, label: str | None) -> int:
    try:
        profile = make_profile(mac, name=name, channel=channel, pins=pins, label=label)
    except ValueError as exc:
        print(exc)
        return 2
    path = save_profile(s.obd_profiles_file, profile)
    print(f"Saved profile '{profile.id}' ({profile.mac}, RFCOMM {profile.channel if profile.channel is not None else 'auto'}) to {path}")
    print(f"Switch to it with: telemetry obd-profile {profile.id}")
    return 0


def delete_profile(s: Settings, name: str) -> int:
    if name.strip().lower() in BUILTIN_PROFILES:
        print(f"'{name}' is built in and cannot be removed")
        return 2
    if remove_profile(s.obd_profiles_file, name):
        print(f"Removed profile '{name.strip().lower()}'")
        return 0
    print(f"No saved profile named '{name}'")
    return 1
