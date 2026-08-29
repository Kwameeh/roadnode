"""Device inventory, provisioning, and installation lifecycle (BIZ-004).

A pilot fleet needs to answer three questions at any moment: which devices
exist, which are actually installed in a vehicle, and which have been retired
or stolen. Without that record, revocation has no list to work from — so this
is the bookkeeping that makes threat A1's controls operable rather than
theoretical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .common import write_json_atomic
from .device_identity import DeviceCredential, provision
from .observations import parse_utc, utc_now

INVENTORY_SCHEMA_VERSION = 1

# A device moves forward through these; it never silently returns to stock.
LIFECYCLE = ("registered", "provisioned", "installed", "retired", "lost")
TERMINAL = frozenset({"retired", "lost"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "registered": frozenset({"provisioned", "retired", "lost"}),
    "provisioned": frozenset({"installed", "retired", "lost"}),
    "installed": frozenset({"provisioned", "retired", "lost"}),
    "retired": frozenset(),
    "lost": frozenset(),
}


class InventoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    state: str
    hardware_revision: str
    registered_at: str
    vehicle_id: str | None = None
    installed_at: str | None = None
    calibration_version: str | None = None
    credential_version: int = 0
    notes: str = ""

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "state": self.state,
            "hardwareRevision": self.hardware_revision,
            "registeredAt": self.registered_at,
            "vehicleId": self.vehicle_id,
            "installedAt": self.installed_at,
            "calibrationVersion": self.calibration_version,
            "credentialVersion": self.credential_version,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DeviceRecord":
        try:
            return cls(
                device_id=str(raw["deviceId"]),
                state=str(raw["state"]),
                hardware_revision=str(raw["hardwareRevision"]),
                registered_at=str(raw["registeredAt"]),
                vehicle_id=raw.get("vehicleId"),
                installed_at=raw.get("installedAt"),
                calibration_version=raw.get("calibrationVersion"),
                credential_version=int(raw.get("credentialVersion", 0)),
                notes=str(raw.get("notes", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InventoryError("malformed device record") from exc


def assert_transition(current: str, target: str) -> None:
    if current not in ALLOWED_TRANSITIONS:
        raise InventoryError(f"unknown state: {current}")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InventoryError(f"cannot move a device from {current} to {target}")


class FleetInventory:
    """The authoritative list of devices and where they are."""

    def __init__(self, records: Iterable[DeviceRecord] = ()):
        self._records: dict[str, DeviceRecord] = {r.device_id: r for r in records}

    def __len__(self) -> int:
        return len(self._records)

    def get(self, device_id: str) -> DeviceRecord:
        try:
            return self._records[device_id]
        except KeyError as exc:
            raise InventoryError(f"unknown device: {device_id}") from exc

    def all(self) -> tuple[DeviceRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def in_state(self, state: str) -> tuple[DeviceRecord, ...]:
        return tuple(r for r in self.all() if r.state == state)

    def register(
        self, device_id: str, *, hardware_revision: str, at: str | None = None
    ) -> DeviceRecord:
        if device_id in self._records:
            raise InventoryError(f"device already registered: {device_id}")
        record = DeviceRecord(
            device_id=device_id,
            state="registered",
            hardware_revision=hardware_revision,
            registered_at=at or utc_now(),
        )
        self._records[device_id] = record
        return record

    def mark_provisioned(
        self, device_id: str, credential: DeviceCredential
    ) -> DeviceRecord:
        record = self.get(device_id)
        assert_transition(record.state, "provisioned")
        updated = replace(
            record,
            state="provisioned",
            credential_version=credential.credential_version,
        )
        self._records[device_id] = updated
        return updated

    def install(
        self,
        device_id: str,
        *,
        vehicle_id: str,
        calibration_version: str,
        at: str | None = None,
    ) -> DeviceRecord:
        """Record a completed installation.

        Calibration is required: an installed device without a valid
        calibration produces IMU data that cannot be scored, so allowing the
        state would hide a broken install.
        """
        record = self.get(device_id)
        assert_transition(record.state, "installed")
        if not calibration_version.strip():
            raise InventoryError("installation requires a calibration version")
        occupant = self.vehicle_occupant(vehicle_id)
        if occupant is not None and occupant.device_id != device_id:
            raise InventoryError(
                f"vehicle {vehicle_id} already has device {occupant.device_id} installed"
            )
        updated = replace(
            record,
            state="installed",
            vehicle_id=vehicle_id,
            calibration_version=calibration_version,
            installed_at=at or utc_now(),
        )
        self._records[device_id] = updated
        return updated

    def uninstall(self, device_id: str) -> DeviceRecord:
        """Remove from a vehicle without retiring the hardware."""
        record = self.get(device_id)
        assert_transition(record.state, "provisioned")
        updated = replace(
            record, state="provisioned", vehicle_id=None, installed_at=None
        )
        self._records[device_id] = updated
        return updated

    def retire(self, device_id: str, *, reason: str = "") -> DeviceRecord:
        return self._terminate(device_id, "retired", reason)

    def report_lost(self, device_id: str, *, reason: str = "") -> DeviceRecord:
        """A stolen or missing device. Its credential must also be revoked."""
        return self._terminate(device_id, "lost", reason)

    def _terminate(self, device_id: str, state: str, reason: str) -> DeviceRecord:
        record = self.get(device_id)
        assert_transition(record.state, state)
        updated = replace(
            record,
            state=state,
            vehicle_id=None,
            installed_at=None,
            notes=reason or record.notes,
        )
        self._records[device_id] = updated
        return updated

    def vehicle_occupant(self, vehicle_id: str) -> DeviceRecord | None:
        for record in self.all():
            if record.state == "installed" and record.vehicle_id == vehicle_id:
                return record
        return None

    def revocation_list(self) -> tuple[str, ...]:
        """Devices whose broker access must not exist."""
        return tuple(r.device_id for r in self.all() if not r.active)

    def summary(self) -> dict[str, int]:
        counts = {state: 0 for state in LIFECYCLE}
        for record in self.all():
            counts[record.state] = counts.get(record.state, 0) + 1
        return counts

    # --- persistence ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": INVENTORY_SCHEMA_VERSION,
            "updatedAt": utc_now(),
            "devices": [r.to_dict() for r in self.all()],
        }

    def save(self, path: str | Path) -> None:
        write_json_atomic(str(Path(path).expanduser()), self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> "FleetInventory":
        resolved = Path(path).expanduser()
        if not resolved.exists():
            return cls()
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InventoryError("unreadable inventory") from exc
        if raw.get("schemaVersion") != INVENTORY_SCHEMA_VERSION:
            raise InventoryError(
                f"unsupported inventory schema version: {raw.get('schemaVersion')}"
            )
        devices = raw.get("devices")
        if not isinstance(devices, list):
            raise InventoryError("inventory is missing its device list")
        return cls(DeviceRecord.from_dict(item) for item in devices)


def provision_device(
    inventory: FleetInventory,
    device_id: str,
    *,
    valid_for_days: int = 365,
) -> tuple[DeviceRecord, DeviceCredential]:
    """Mint a credential and record that the device now has one."""
    credential = provision(device_id, valid_for_days=valid_for_days)
    record = inventory.mark_provisioned(device_id, credential)
    return record, credential


def installation_checklist(record: DeviceRecord) -> list[str]:
    """What still blocks this device from producing scoreable data."""
    outstanding: list[str] = []
    if record.state == "registered":
        outstanding.append("provision a unique broker credential")
    if record.state in {"registered", "provisioned"}:
        outstanding.append("fit the device and record its vehicle")
    if record.calibration_version is None:
        outstanding.append("run IMU calibration and record its version")
    if record.state in TERMINAL:
        outstanding.append(f"device is {record.state}; it cannot be installed")
    return outstanding


def stale_installations(
    inventory: FleetInventory, *, max_age_days: int, now: datetime | None = None
) -> tuple[DeviceRecord, ...]:
    """Installed devices whose calibration is older than the policy allows."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stale: list[DeviceRecord] = []
    for record in inventory.all():
        if record.state != "installed" or record.installed_at is None:
            continue
        age_days = (current - parse_utc(record.installed_at)).total_seconds() / 86400
        if age_days > max_age_days:
            stale.append(record)
    return tuple(stale)
