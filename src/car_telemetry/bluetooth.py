from __future__ import annotations

import re
import subprocess
import time
from typing import Any

import pexpect

from .common import run

MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def validate_mac(mac: str) -> str:
    value = mac.strip().upper()
    if not MAC_RE.fullmatch(value):
        raise ValueError("Bluetooth MAC must look like AA:BB:CC:DD:EE:FF")
    return value


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


def discover_channel(mac: str) -> int | None:
    mac = validate_mac(mac)
    _, out, err = run(["sdptool", "browse", mac], 20)
    if not out and err:
        raise RuntimeError(err)

    blocks = re.split(r"\n\s*\n", out)
    for block in blocks:
        if "ELM327" in block.upper() and "RFCOMM" in block:
            match = re.search(r"Channel:\s*(\d+)", block)
            if match:
                return int(match.group(1))
    for block in blocks:
        if '"Serial Port"' in block and "RFCOMM" in block:
            match = re.search(r"Channel:\s*(\d+)", block)
            if match:
                return int(match.group(1))
    return None


def bind(mac: str, channel: int) -> None:
    mac = validate_mac(mac)
    subprocess.run(
        ["sudo", "rfcomm", "release", "rfcomm0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(["sudo", "rfcomm", "bind", "rfcomm0", mac, str(int(channel))], check=True)
