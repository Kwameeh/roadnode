# Core and Optional Signals

`signal_policy` decides what *may* be polled; `signal_selection` records what the
owner chose. The two are kept apart so a support question about a missing reading
has one answer: either the vehicle never offered it, or the owner turned it off.

## Tiers

| Tier | Signals | Behaviour |
|---|---|---|
| Core | `RPM`, `SPEED`, `COOLANT_TEMP`, `CONTROL_MODULE_VOLTAGE` | Always polled. Cannot be removed from the Signals page, and cannot be removed by editing the stored preferences file. |
| Default | `ENGINE_LOAD`, `THROTTLE_POS`, `FUEL_LEVEL`, `INTAKE_TEMP`, `MAF` | Polled unless the owner removes them. |
| Optional | `OIL_TEMP`, `ODOMETER`, and any other live PID the vehicle advertises | Polled only when the owner adds them. |

Core membership is a product decision, not a deployment setting. Trip start and
end depend on `RPM`; distance, overspeed and event severity depend on `SPEED`;
overheat warning depends on `COOLANT_TEMP`; battery health and unexplained
shutdowns depend on `CONTROL_MODULE_VOLTAGE`. There is no configuration that can
weaken those promises.

A PID the catalog has never seen is still admitted, as a plain optional signal
with generic wording. Refusing it would make the product poorer than the car.

## Polling impact

Every extra PID lengthens the adapter's round-robin loop. The loop must complete
inside the `RPM`/`SPEED` freshness limit of two seconds, otherwise every consumer
downstream sees a stale "current" speed while the UI still shows a full signal
list. So optional selection is bounded by a budget rather than by a count anyone
picked.

`OBD_ROUND_TRIP_SECONDS` is the measured cost of one query on the installed
adapter, defaulting to the conservative `0.08`. At that cost about 25 signals fit.
A selection that would exceed the budget is refused with its reason; it is never
accepted and then silently trimmed elsewhere.

## Unsupported core signals

A vehicle that does not advertise a core PID is not rejected. The signal is
reported as `unavailable`, never substituted with zero, and the features that
depend on it say so. `EDGE-002`'s recorded vehicle fixtures cover this, including
a legacy vehicle offering only `RPM` and `SPEED`.

## Persistence and revision

Preferences are stored per vehicle key as *intent* - which optional signals were
added, which default ones were removed - never as the resolved list. Storing the
resolved list would freeze one vehicle's support into the profile, so a vehicle
that later exposes a PID would never start reporting it.

The store writes atomically, so a power cut during a save cannot silently reset a
selection. Once a VIN is read it becomes the key, so a choice follows the car
rather than the device. A request the connected vehicle cannot honour is kept and
shown as unavailable rather than deleted - a driver who swaps back expects the
setting to still be there, and an adapter mid-discovery briefly advertises less
than the car really has.

Each resolved plan carries a `signalsRevision`. Retained metadata publishes that
revision alongside the resolved selection only; owner intent stays on the device.
A late subscriber can therefore tell a real selection change from a device that
merely reconnected.

## Signals page

The device decides tier, state and wording, and the page renders them unchanged.
A browser left open on a stale page cannot offer a choice the device would refuse,
and a refusal is shown with the device's reason rather than a generic failure.
