from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import pexpect

from .common import run

MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


@dataclass(frozen=True)
class BluetoothCandidate:
    mac: str
    channel: int | None = None

    def token(self) -> str:
        return f"{self.mac}@{self.channel}" if self.channel is not None else self.mac


def validate_mac(mac: str) -> str:
    value = mac.strip().upper()
    if not MAC_RE.fullmatch(value):
        raise ValueError("Bluetooth MAC must look like AA:BB:CC:DD:EE:FF")
    return value


def validate_channel(channel: int | str) -> int:
    value = int(str(channel).strip(), 10)
    if value < 1 or value > 30:
        raise ValueError("RFCOMM channel must be between 1 and 30")
    return value


def parse_candidates(raw: str, fallback_mac: str = "", fallback_channel: int | str = 1) -> list[BluetoothCandidate]:
    tokens = [item.strip() for item in raw.split(",") if item.strip()]
    if not tokens and fallback_mac.strip():
        tokens = [f"{fallback_mac}@{fallback_channel}"]

    candidates: list[BluetoothCandidate] = []
    seen: set[str] = set()
    for token in tokens:
        mac_raw, sep, channel_raw = token.partition("@")
        mac = validate_mac(mac_raw)
        if mac in seen:
            continue
        seen.add(mac)
        channel = validate_channel(channel_raw) if sep else None
        candidates.append(BluetoothCandidate(mac, channel))
    return candidates


def power_on() -> None:
    subprocess.run(["rfkill", "unblock", "bluetooth"], check=False, capture_output=True)
    subprocess.run(["bluetoothctl", "power", "on"], check=False, capture_output=True)


def controller_status() -> dict[str, Any]:
    code, out, err = run(["bluetoothctl", "show"], 5)
    if code != 0:
        return {"available": False, "powered": False, "error": err or out or "bluetoothctl failed"}
    return {
        "available": True,
        "powered": "Powered: yes" in out,
        "discoverable": "Discoverable: yes" in out,
        "pairable": "Pairable: yes" in out,
        "raw": out,
    }


def _device_info(mac: str, fallback_name: str = "") -> dict[str, Any]:
    mac = validate_mac(mac)
    _, out, _ = run(["bluetoothctl", "info", mac], 5)
    result: dict[str, Any] = {
        "mac": mac,
        "name": fallback_name or mac,
        "paired": False,
        "trusted": False,
        "connected": False,
        "blocked": False,
    }
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("Name:"):
            result["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Alias:") and result["name"] == mac:
            result["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            result["paired"] = line.endswith("yes")
        elif line.startswith("Trusted:"):
            result["trusted"] = line.endswith("yes")
        elif line.startswith("Connected:"):
            result["connected"] = line.endswith("yes")
        elif line.startswith("Blocked:"):
            result["blocked"] = line.endswith("yes")
        elif line.startswith("RSSI:"):
            try:
                result["rssi"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return result


def devices() -> list[dict[str, Any]]:
    _, out, _ = run(["bluetoothctl", "devices"], 5)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in out.splitlines():
        match = re.match(r"Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)", line.strip())
        if not match:
            continue
        mac = match.group(1).upper()
        if mac in seen:
            continue
        seen.add(mac)
        found.append(_device_info(mac, match.group(2).strip()))
    found.sort(key=lambda item: (not item.get("connected", False), not item.get("paired", False), item.get("name", "")))
    return found


def scan(seconds: int = 10) -> list[dict[str, Any]]:
    power_on()
    seconds = max(3, min(int(seconds), 30))
    subprocess.run(
        ["timeout", str(seconds), "bluetoothctl", "scan", "on"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(["bluetoothctl", "scan", "off"], capture_output=True, check=False)
    return devices()


def pair(mac: str, pin: str | None = None) -> dict[str, Any]:
    mac = validate_mac(mac)
    power_on()

    child = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=20)
    try:
        child.expect([r"\[.*\]#", r"# "])
        child.sendline("agent KeyboardDisplay")
        child.expect([r"Agent registered", r"Agent is already registered", r"\[.*\]#", r"# "])
        child.sendline("default-agent")
        time.sleep(0.3)
        child.sendline(f"pair {mac}")

        deadline = time.monotonic() + 35
        success = False
        while time.monotonic() < deadline:
            index = child.expect(
                [
                    r"Enter PIN code:",
                    r"Enter passkey.*:",
                    r"Confirm passkey.*\(yes/no\):",
                    r"Pairing successful",
                    r"AlreadyExists",
                    r"Failed to pair:.*",
                    r"AuthenticationFailed",
                    pexpect.TIMEOUT,
                ],
                timeout=5,
            )
            if index in (0, 1):
                if not pin:
                    raise RuntimeError("The device requested a PIN. Enter its PIN in the web app and try again.")
                child.sendline(pin)
            elif index == 2:
                child.sendline("yes")
            elif index in (3, 4):
                success = True
                break
            elif index in (5, 6):
                raise RuntimeError("Bluetooth pairing failed")
            elif index == 7:
                info = _device_info(mac)
                if info.get("paired"):
                    success = True
                    break

        if not success and not _device_info(mac).get("paired"):
            raise RuntimeError("Bluetooth pairing did not complete")

        child.sendline(f"trust {mac}")
        time.sleep(0.5)
        child.sendline(f"connect {mac}")
        time.sleep(1.0)
    finally:
        try:
            child.sendline("quit")
            child.close(force=True)
        except Exception:
            pass

    return _device_info(mac)


def disconnect(mac: str) -> dict[str, Any]:
    mac = validate_mac(mac)
    run(["bluetoothctl", "disconnect", mac], 10)
    return _device_info(mac)


def forget(mac: str) -> dict[str, Any]:
    mac = validate_mac(mac)
    code, out, err = run(["bluetoothctl", "remove", mac], 10)
    return {"ok": code == 0, "mac": mac, "message": out or err}


def parse_rfcomm_channel(sdp_output: str, require_serial_port: bool = True) -> int | None:
    """Pick the ELM327 RFCOMM channel from `sdptool browse` or `sdptool search SP` output.

    A browse lists every service (phones also advertise OBEX over RFCOMM), so it
    needs the "Serial Port" class check; a `search ... SP` result is already SPP.
    """
    blocks = re.split(r"\n\s*\n", sdp_output)
    for block in blocks:
        if "ELM327" in block.upper() and "RFCOMM" in block:
            match = re.search(r"Channel:\s*(\d+)", block)
            if match:
                return int(match.group(1))
    for block in blocks:
        if "RFCOMM" in block and (not require_serial_port or '"Serial Port"' in block):
            match = re.search(r"Channel:\s*(\d+)", block)
            if match:
                return int(match.group(1))
    return None


def discover_channel(mac: str) -> int | None:
    mac = validate_mac(mac)
    _, out, err = run(["sdptool", "browse", mac], 20)
    if not out and err:
        raise RuntimeError(err)
    return parse_rfcomm_channel(out)


def search_serial_channel(mac: str) -> int | None:
    """Ask the device for its Serial Port Profile channel, falling back to a full browse."""
    mac = validate_mac(mac)
    _, out, err = run(["sdptool", "search", "--bdaddr", mac, "SP"], 20)
    channel = parse_rfcomm_channel(out, require_serial_port=False)
    if channel is not None:
        return channel
    if "Failed to connect" in err or "Host is down" in err:
        raise RuntimeError(err)
    return discover_channel(mac)


def device_known(mac: str) -> bool:
    code, _, _ = run(["bluetoothctl", "info", validate_mac(mac)], 5)
    return code == 0


def device_info(mac: str) -> dict[str, Any]:
    return _device_info(mac)


def trust(mac: str) -> bool:
    code, _, _ = run(["bluetoothctl", "trust", validate_mac(mac)], 10)
    return code == 0


RFCOMM_BINDING_RE = re.compile(
    r"^(rfcomm\d+):\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+channel\s+(\d+)\s*(.*)$"
)


def parse_rfcomm_bindings(output: str) -> dict[str, dict[str, Any]]:
    """Parse `rfcomm` output such as `rfcomm0: 00:10:CC:4F:36:03 channel 1 clean`."""
    bindings: dict[str, dict[str, Any]] = {}
    for raw in output.splitlines():
        match = RFCOMM_BINDING_RE.match(raw.strip())
        if match:
            bindings[match.group(1)] = {
                "mac": match.group(2).upper(),
                "channel": int(match.group(3)),
                "state": match.group(4).strip(),
            }
    return bindings


def rfcomm_bindings() -> dict[str, dict[str, Any]]:
    _, out, _ = run(["rfcomm"], 5)
    return parse_rfcomm_bindings(out)


def bind(mac: str, channel: int) -> None:
    mac = validate_mac(mac)
    channel = validate_channel(channel)
    subprocess.run(
        ["sudo", "rfcomm", "release", "rfcomm0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(["sudo", "rfcomm", "bind", "rfcomm0", mac, str(int(channel))], check=True)


def bind_first_candidate(candidates: list[BluetoothCandidate]) -> BluetoothCandidate:
    errors: list[str] = []
    for candidate in candidates:
        try:
            channel = candidate.channel if candidate.channel is not None else discover_channel(candidate.mac)
            if channel is None:
                raise RuntimeError("No ELM327/Serial Port RFCOMM channel was found")
            bind(candidate.mac, channel)
            return BluetoothCandidate(candidate.mac, channel)
        except Exception as exc:
            errors.append(f"{candidate.mac}: {exc}")
    raise RuntimeError("No Bluetooth candidate could be bound to rfcomm0" + (f" ({'; '.join(errors)})" if errors else ""))
