"""Mandatory core versus selectable optional OBD signals (EDGE-003).

Two questions get confused constantly, so this module answers them apart from
each other:

- *What must RoadNode always poll?* Trip detection, distance, overspeed,
  overheat and battery health are product promises. The signals behind them
  are `CORE` and an owner cannot switch them off.
- *What may an owner add?* Everything else the vehicle actually advertises.
  Adding signals is never free: every extra PID lengthens the adapter's
  round-robin loop, and once the loop runs longer than the freshness budget of
  `RPM`/`SPEED` the core promises quietly degrade. Optional selection is
  therefore bounded by a polling budget rather than by taste.

Support is decided elsewhere. `vehicle_support` classifies what a vehicle can
answer; this module only decides what to ask for among the answerable. A core
signal the vehicle never advertises is reported as unavailable - it is never
substituted with a zero, and the vehicle is never rejected for lacking it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .observations import CANONICAL_OBD_UNITS, FAST_OBD_MAX_AGE_MS, FAST_OBD_SIGNALS

CORE = "core"
DEFAULT = "default"
OPTIONAL = "optional"
TIER_ORDER = {CORE: 0, DEFAULT: 1, OPTIONAL: 2}

SELECTED = "selected"
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
REJECTED = "rejected"

# One PID query costs one adapter round trip. 0.08s is the conservative end of
# what an ELM327 clone sustains at 38400 baud in fast mode; the measured result
# from `transport_benchmark` is what may lower it, not optimism.
DEFAULT_ROUND_TRIP_SECONDS = 0.08

# The loop must come back around before RPM/SPEED breach their freshness
# contract, otherwise every consumer downstream sees a stale "current" speed.
POLLING_BUDGET_SECONDS = FAST_OBD_MAX_AGE_MS / 1000.0


class SignalPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SignalDefinition:
    """One pollable signal and the customer-visible reason it exists."""

    name: str
    tier: str
    purpose: str
    round_trip_seconds: float = DEFAULT_ROUND_TRIP_SECONDS

    @property
    def unit(self) -> str | None:
        return CANONICAL_OBD_UNITS.get(self.name)

    @property
    def mandatory(self) -> bool:
        return self.tier == CORE

    @property
    def on_by_default(self) -> bool:
        return self.tier in {CORE, DEFAULT}


_DEFINITIONS: tuple[SignalDefinition, ...] = (
    SignalDefinition(
        "RPM",
        CORE,
        "Detects when the engine is running, which starts and ends every trip.",
    ),
    SignalDefinition(
        "SPEED",
        CORE,
        "Measures distance, overspeed and the severity of harsh-driving events.",
    ),
    SignalDefinition(
        "COOLANT_TEMP",
        CORE,
        "Warns about overheating before it becomes engine damage.",
    ),
    SignalDefinition(
        "CONTROL_MODULE_VOLTAGE",
        CORE,
        "Shows battery and charging health, and explains unexpected shutdowns.",
    ),
    SignalDefinition(
        "ENGINE_LOAD",
        DEFAULT,
        "Adds context to fuel use and harsh-acceleration coaching.",
    ),
    SignalDefinition(
        "THROTTLE_POS",
        DEFAULT,
        "Separates driver-commanded acceleration from road or load effects.",
    ),
    SignalDefinition(
        "FUEL_LEVEL",
        DEFAULT,
        "Reports remaining fuel where the vehicle publishes it.",
    ),
    SignalDefinition(
        "INTAKE_TEMP",
        DEFAULT,
        "Supports fuel and air-intake diagnostics.",
    ),
    SignalDefinition(
        "MAF",
        DEFAULT,
        "Estimates fuel consumption when the vehicle reports no fuel rate.",
    ),
    SignalDefinition(
        "OIL_TEMP",
        OPTIONAL,
        "Adds oil-temperature health for vehicles that expose it.",
    ),
    SignalDefinition(
        "ODOMETER",
        OPTIONAL,
        "Uses the vehicle's own odometer instead of an accumulated estimate.",
    ),
)

CATALOG: Mapping[str, SignalDefinition] = {item.name: item for item in _DEFINITIONS}
CORE_SIGNALS: tuple[str, ...] = tuple(
    item.name for item in _DEFINITIONS if item.mandatory
)
DEFAULT_SIGNALS: tuple[str, ...] = tuple(
    item.name for item in _DEFINITIONS if item.on_by_default
)


def definition(name: str) -> SignalDefinition:
    """Known signals keep their curated wording; unknown ones stay optional.

    A vehicle may advertise PIDs this catalog has never seen. Refusing them
    would make the product poorer than the car, so they are admitted as plain
    optional signals with honest, generic wording.
    """
    known = CATALOG.get(name)
    if known is not None:
        return known
    return SignalDefinition(
        name,
        OPTIONAL,
        "Extra signal advertised by this vehicle. No RoadNode feature depends on it.",
    )


@dataclass(frozen=True)
class SignalDecision:
    name: str
    tier: str
    state: str
    reason: str
    purpose: str
    unit: str | None

    def document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "tier": self.tier,
            "state": self.state,
            "reason": self.reason,
            "purpose": self.purpose,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class SignalPlan:
    """What will actually be polled, and what the owner is told about it."""

    selected: tuple[str, ...]
    decisions: tuple[SignalDecision, ...]
    round_trip_seconds: float
    budget_seconds: float

    @property
    def cycle_seconds(self) -> float:
        return round(
            sum(definition(name).round_trip_seconds for name in self.selected), 6
        )

    @property
    def capacity(self) -> int:
        """How many more signals fit before core freshness is at risk."""
        remaining = self.budget_seconds - self.cycle_seconds
        return max(0, int(remaining // self.round_trip_seconds))

    def by_state(self, state: str) -> tuple[str, ...]:
        return tuple(item.name for item in self.decisions if item.state == state)

    @property
    def unavailable(self) -> tuple[str, ...]:
        return self.by_state(UNAVAILABLE)

    @property
    def rejected(self) -> tuple[str, ...]:
        return self.by_state(REJECTED)

    @property
    def unavailable_core(self) -> tuple[str, ...]:
        return tuple(
            item.name
            for item in self.decisions
            if item.state == UNAVAILABLE and item.tier == CORE
        )

    def document(self) -> dict[str, object]:
        return {
            "selected": list(self.selected),
            "core": list(CORE_SIGNALS),
            "unavailableCore": list(self.unavailable_core),
            "cycleSeconds": self.cycle_seconds,
            "budgetSeconds": self.budget_seconds,
            "capacity": self.capacity,
            "decisions": [item.document() for item in self.decisions],
        }

    @property
    def revision(self) -> str:
        """Stable identity of a plan, so metadata is republished only on change."""
        canonical = json.dumps(self.document(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def explanation(self) -> tuple[str, ...]:
        """Plain sentences the Signals UI and support can show unedited."""
        lines = [
            "{count} signals are read about {cycle:.2f}s apart; the limit is "
            "{budget:.2f}s so speed and engine state stay current.".format(
                count=len(self.selected),
                cycle=self.cycle_seconds,
                budget=self.budget_seconds,
            )
        ]
        if self.capacity == 0:
            lines.append(
                "No more optional signals can be added without slowing the core "
                "readings. Remove one first."
            )
        else:
            noun = "signal" if self.capacity == 1 else "signals"
            lines.append(f"You can add up to {self.capacity} more optional {noun}.")
        missing = self.unavailable_core
        if missing:
            lines.append(
                "This vehicle does not report "
                + ", ".join(missing)
                + ". Features that need it are shown as unavailable, never as zero."
            )
        for item in self.decisions:
            if item.state == REJECTED:
                lines.append(f"{item.name} was not added: {item.reason}.")
        return tuple(lines)


def _ordered(names: Iterable[str]) -> tuple[str, ...]:
    """Deterministic order: tier first, then name. Never selection history."""
    unique = {str(name).strip().upper() for name in names if str(name).strip()}
    return tuple(
        sorted(unique, key=lambda name: (TIER_ORDER[definition(name).tier], name))
    )


def plan_selection(
    supported: Iterable[str],
    requested: Iterable[str] = (),
    *,
    deselected: Iterable[str] = (),
    round_trip_seconds: float = DEFAULT_ROUND_TRIP_SECONDS,
    budget_seconds: float = POLLING_BUDGET_SECONDS,
) -> SignalPlan:
    """Decide what to poll for one vehicle.

    `supported` is what the vehicle advertises as a live PID, `requested` is
    what the owner added, and `deselected` is what the owner removed from the
    default-on set. Core membership is not negotiable, so a core name appearing
    in `deselected` is ignored rather than treated as an error: stored owner
    preferences may predate a change to the core list.
    """
    if round_trip_seconds <= 0:
        raise SignalPolicyError("round_trip_seconds must be positive")
    if budget_seconds < round_trip_seconds:
        raise SignalPolicyError("budget must fit at least one signal")

    available = _ordered(supported)
    wanted = set(_ordered(requested))
    removed = {name for name in _ordered(deselected) if not definition(name).mandatory}

    decisions: list[SignalDecision] = []
    selected: list[str] = []
    spent = 0.0

    # Core is walked first because it owns the budget before any optional
    # signal competes for it; `_ordered` puts the core tier ahead.
    candidates = list(available) + [name for name in CORE_SIGNALS if name not in available]
    for name in _ordered(candidates):
        spec = definition(name)
        if name not in available:
            decisions.append(
                SignalDecision(
                    name,
                    spec.tier,
                    UNAVAILABLE,
                    "this vehicle does not advertise the PID",
                    spec.purpose,
                    spec.unit,
                )
            )
            continue
        chosen = spec.mandatory or (
            name in wanted or (spec.on_by_default and name not in removed)
        )
        if not chosen:
            decisions.append(
                SignalDecision(
                    name, spec.tier, AVAILABLE, "not selected", spec.purpose, spec.unit
                )
            )
            continue
        cost = spec.round_trip_seconds
        # Rounded so accumulated float error cannot reject a signal that the
        # advertised remaining capacity says still fits.
        if not spec.mandatory and round(spent + cost, 6) > round(budget_seconds, 6):
            decisions.append(
                SignalDecision(
                    name,
                    spec.tier,
                    REJECTED,
                    "adding it would slow the core readings past their freshness limit",
                    spec.purpose,
                    spec.unit,
                )
            )
            continue
        spent = round(spent + cost, 6)
        selected.append(name)
        decisions.append(
            SignalDecision(
                name, spec.tier, SELECTED, "polled every cycle", spec.purpose, spec.unit
            )
        )

    for name in sorted(wanted - set(available)):
        spec = definition(name)
        decisions.append(
            SignalDecision(
                name,
                spec.tier,
                UNAVAILABLE,
                "this vehicle does not advertise the PID",
                spec.purpose,
                spec.unit,
            )
        )

    ordered_decisions = tuple(
        sorted(decisions, key=lambda item: (TIER_ORDER[item.tier], item.name))
    )
    return SignalPlan(
        selected=tuple(selected),
        decisions=ordered_decisions,
        round_trip_seconds=round_trip_seconds,
        budget_seconds=budget_seconds,
    )


def fast_signals_are_covered(plan: SignalPlan) -> bool:
    """True when every fast signal the vehicle supports is actually polled."""
    unavailable = set(plan.unavailable)
    return all(
        name in plan.selected or name in unavailable for name in FAST_OBD_SIGNALS
    )
