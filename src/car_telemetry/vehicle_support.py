from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .observations import CANONICAL_OBD_UNITS

# Why a signal is not being published. "unsupported" means the ECU never
# advertised the PID; "no_data" means it advertised it but returned a null
# response. Neither may ever be represented as a zero measurement.
UNSUPPORTED = "unsupported"
NO_DATA = "no_data"
SUPPORTED = "supported"


class VehicleFixtureError(ValueError):
    pass


@dataclass(frozen=True)
class SignalSupport:
    name: str
    state: str
    unit: str | None = None

    @property
    def publishable(self) -> bool:
        return self.state == SUPPORTED


@dataclass(frozen=True)
class VehicleFixture:
    """A recorded vehicle/support combination used to prove edge behaviour."""

    key: str
    description: str
    protocol_name: str
    supported_commands: tuple[str, ...]
    null_responses: tuple[str, ...]
    responses: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VehicleFixture":
        try:
            fixture = cls(
                key=str(raw["vehicleKey"]),
                description=str(raw["description"]),
                protocol_name=str(raw["protocolName"]),
                supported_commands=tuple(raw["supportedCommands"]),
                null_responses=tuple(raw.get("nullResponses", ())),
                responses=dict(raw.get("responses", {})),
            )
        except (KeyError, TypeError) as exc:
            raise VehicleFixtureError("malformed vehicle fixture") from exc

        unknown = set(fixture.null_responses) - set(fixture.supported_commands)
        if unknown:
            raise VehicleFixtureError(
                f"{fixture.key}: null responses for unsupported commands: "
                f"{sorted(unknown)}"
            )
        undeclared = set(fixture.responses) - set(fixture.supported_commands)
        if undeclared:
            raise VehicleFixtureError(
                f"{fixture.key}: responses for unsupported commands: "
                f"{sorted(undeclared)}"
            )
        return fixture

    @classmethod
    def load(cls, path: str | Path) -> "VehicleFixture":
        resolved = Path(path).expanduser()
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VehicleFixtureError(f"unreadable vehicle fixture: {resolved}") from exc
        return cls.from_dict(raw)


def load_fixtures(directory: str | Path) -> tuple[VehicleFixture, ...]:
    root = Path(directory).expanduser()
    return tuple(
        VehicleFixture.load(path) for path in sorted(root.glob("*.json"))
    )


def classify_signal(name: str, fixture: VehicleFixture) -> SignalSupport:
    if name not in fixture.supported_commands:
        return SignalSupport(name=name, state=UNSUPPORTED)
    if name in fixture.null_responses:
        return SignalSupport(name=name, state=NO_DATA)
    return SignalSupport(
        name=name, state=SUPPORTED, unit=CANONICAL_OBD_UNITS.get(name)
    )


def classify_all(
    names: Iterable[str], fixture: VehicleFixture
) -> dict[str, SignalSupport]:
    return {name: classify_signal(name, fixture) for name in names}


def publishable_signals(
    names: Iterable[str], fixture: VehicleFixture
) -> dict[str, Any]:
    """Only signals with a real measurement. Absence is never zero.

    A caller must publish exactly these keys; anything omitted is genuinely
    unknown to this vehicle and must stay absent from the frame.
    """
    published: dict[str, Any] = {}
    for name, support in classify_all(names, fixture).items():
        if not support.publishable:
            continue
        value = fixture.responses.get(name)
        if value is None:
            continue
        published[name] = {"value": value, "unit": support.unit or "1"}
    return published


def discovery_report(
    names: Iterable[str], fixture: VehicleFixture
) -> dict[str, list[str]]:
    """Metadata describing what this vehicle can and cannot provide."""
    classified = classify_all(names, fixture)
    report: dict[str, list[str]] = {SUPPORTED: [], NO_DATA: [], UNSUPPORTED: []}
    for name, support in sorted(classified.items()):
        report[support.state].append(name)
    return report
