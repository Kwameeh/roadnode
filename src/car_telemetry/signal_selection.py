"""Owner signal selection: persistence, guard rails and revision (EDGE-004).

`signal_policy` decides what *may* be polled. This module owns what an owner
actually chose, and the three properties the Signals UI depends on:

- a core signal cannot be removed, from the UI or by editing a stored file;
- a choice survives reconnect, a VIN change and a restart, including a choice
  the currently connected vehicle cannot honour - the request is kept and
  reported as unavailable rather than silently deleted, because a driver who
  swaps back to the other car expects their setting to still be there;
- every change produces a new revision, which is what retained metadata
  publishes so a late subscriber learns the current selection without the
  device replaying its history.

Preferences are stored as intent (`requested`, `deselected`), never as the
resolved list. Storing the resolved list would freeze one vehicle's support
into the owner's profile, so a firmware update that adds a PID would never
reach them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .observations import utc_now
from .signal_policy import (
    DEFAULT_ROUND_TRIP_SECONDS,
    POLLING_BUDGET_SECONDS,
    SignalPlan,
    definition,
    plan_selection,
)
from .vehicle_profiles import safe_id

SELECTION_VERSION = 1


class SignalSelectionError(ValueError):
    """A selection the owner is not allowed to make, with a reason to show."""


def _clean(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted({str(name).strip().upper() for name in names if str(name).strip()})
    )


@dataclass(frozen=True)
class StoredSelection:
    """Owner intent for one vehicle. Never the resolved poll list."""

    vehicle_key: str
    requested: tuple[str, ...] = ()
    deselected: tuple[str, ...] = ()
    updated_at: str = ""

    @classmethod
    def from_profile(cls, vehicle_key: str, profile: dict[str, Any]) -> "StoredSelection":
        """Reads the current shape, falling back to the pre-EDGE-004 profile.

        Older installs stored a flat `selectedSignals` list with no notion of
        removing a default-on signal. Treating that list as `requested` keeps
        those owners' extras and lets the policy restore the defaults.
        """
        selection = profile.get("signalSelection")
        if isinstance(selection, dict):
            return cls(
                vehicle_key=vehicle_key,
                requested=_clean(selection.get("requested", ())),
                deselected=_clean(selection.get("deselected", ())),
                updated_at=str(selection.get("updatedAt", "")),
            )
        return cls(
            vehicle_key=vehicle_key,
            requested=_clean(profile.get("selectedSignals", ())),
            updated_at=str(profile.get("updatedAt", "")),
        )

    def document(self) -> dict[str, Any]:
        return {
            "version": SELECTION_VERSION,
            "requested": list(self.requested),
            "deselected": list(self.deselected),
            "updatedAt": self.updated_at,
        }

    def with_choice(self, name: str, selected: bool, *, now: str) -> "StoredSelection":
        requested = set(self.requested)
        deselected = set(self.deselected)
        if selected:
            requested.add(name)
            deselected.discard(name)
        else:
            requested.discard(name)
            deselected.add(name)
        return replace(
            self,
            requested=_clean(requested),
            deselected=_clean(deselected),
            updated_at=now,
        )


class SignalSelectionStore:
    """Per-vehicle JSON preferences, written atomically.

    A half-written preferences file after a power cut would silently reset an
    owner's selection, so the write goes through a temporary file and a
    replace rather than truncating the real one.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, vehicle_key: str) -> Path:
        return self.root / f"{safe_id(vehicle_key)}.json"

    def _profile(self, vehicle_key: str) -> dict[str, Any]:
        path = self.path(vehicle_key)
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def load(self, vehicle_key: str) -> StoredSelection:
        return StoredSelection.from_profile(vehicle_key, self._profile(vehicle_key))

    def save(self, selection: StoredSelection, **profile_updates: Any) -> None:
        profile = self._profile(selection.vehicle_key)
        profile.update(profile_updates)
        profile["vehicleKey"] = selection.vehicle_key
        profile["signalSelection"] = selection.document()
        # Kept for the local UI and any older reader; the policy no longer
        # treats it as intent.
        profile["selectedSignals"] = list(selection.requested)
        profile["updatedAt"] = selection.updated_at or utc_now()
        path = self.path(selection.vehicle_key)
        handle, temporary = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(profile, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


class SignalSelectionService:
    """The Signals UI's whole contract: what is offered, chosen and published."""

    def __init__(
        self,
        store: SignalSelectionStore,
        vehicle_key: str,
        *,
        round_trip_seconds: float = DEFAULT_ROUND_TRIP_SECONDS,
        budget_seconds: float = POLLING_BUDGET_SECONDS,
        now: Any = utc_now,
    ):
        self.store = store
        self._now = now
        self.round_trip_seconds = round_trip_seconds
        self.budget_seconds = budget_seconds
        self.vehicle_key = vehicle_key
        self.selection = store.load(vehicle_key)
        self.supported: tuple[str, ...] = ()
        self._plan = self._build()

    def _build(self) -> SignalPlan:
        return plan_selection(
            self.supported,
            self.selection.requested,
            deselected=self.selection.deselected,
            round_trip_seconds=self.round_trip_seconds,
            budget_seconds=self.budget_seconds,
        )

    @property
    def plan(self) -> SignalPlan:
        return self._plan

    @property
    def revision(self) -> str:
        return self._plan.revision

    def use_vehicle(self, vehicle_key: str) -> SignalPlan:
        """Switch to another vehicle's stored preferences, e.g. once VIN arrives."""
        if vehicle_key != self.vehicle_key:
            self.vehicle_key = vehicle_key
            self.selection = self.store.load(vehicle_key)
        self._plan = self._build()
        return self._plan

    def observe_supported(self, names: Iterable[str]) -> SignalPlan:
        """Recompute after (re)connect, when the vehicle states what it offers.

        The stored intent is deliberately not trimmed to the new support set:
        an adapter that reconnects mid-discovery briefly advertises less than
        the car really has, and pruning here would quietly discard the owner's
        choices on every flaky reconnect.
        """
        self.supported = _clean(names)
        self._plan = self._build()
        return self._plan

    def select(self, name: str, selected: bool = True) -> SignalPlan:
        """Apply one owner choice, or refuse it with a reason worth showing."""
        cleaned = str(name).strip().upper()
        if not cleaned:
            raise SignalSelectionError("A signal name is required.")
        if not self.supported:
            raise SignalSelectionError(
                "Signals can only be changed while the vehicle is connected."
            )
        if definition(cleaned).mandatory and not selected:
            raise SignalSelectionError(
                f"{cleaned} is a core signal and cannot be removed. "
                "Trip detection, distance and health alerts depend on it."
            )
        if cleaned not in self.supported:
            raise SignalSelectionError(
                f"{cleaned} is not advertised by this vehicle, so it cannot be added."
            )

        candidate = self.selection.with_choice(cleaned, selected, now=self._now())
        plan = plan_selection(
            self.supported,
            candidate.requested,
            deselected=candidate.deselected,
            round_trip_seconds=self.round_trip_seconds,
            budget_seconds=self.budget_seconds,
        )
        if selected and cleaned in plan.rejected:
            raise SignalSelectionError(
                f"{cleaned} cannot be added: reading it would slow speed and "
                "engine state past their freshness limit. Remove another "
                "optional signal first."
            )

        self.selection = candidate
        self._plan = plan
        self.store.save(
            candidate,
            supportedSignals=list(self.supported),
            signalsRevision=plan.revision,
        )
        return plan

    def metadata_body(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """The signal half of retained metadata, carrying its own revision.

        Only the resolved facts go out. Owner intent stays on the device: a
        subscriber needs to know what is being measured, not what was asked
        for on a car this device is no longer plugged into.
        """
        document = self._plan.document()
        body = dict(extra or {})
        body["signals"] = {
            "revision": self._plan.revision,
            "selected": document["selected"],
            "core": document["core"],
            "unavailableCore": document["unavailableCore"],
            "cycleSeconds": document["cycleSeconds"],
            "budgetSeconds": document["budgetSeconds"],
        }
        return body

    def ui_document(self) -> dict[str, Any]:
        """Everything the Signals screen renders, decided on the device."""
        document = self._plan.document()
        document["vehicleKey"] = self.vehicle_key
        document["revision"] = self._plan.revision
        document["supported"] = list(self.supported)
        document["requested"] = list(self.selection.requested)
        document["deselected"] = list(self.selection.deselected)
        document["explanation"] = list(self._plan.explanation())
        document["connected"] = bool(self.supported)
        document["updatedAt"] = self.selection.updated_at
        return document
