# Data Collection

Collected categories:

- GPS: latitude, longitude, speed, heading, altitude, satellites, HDOP/fix
- IMU: acceleration, gyro, resultant G and derived driving events
- OBD live signals: discovered through python-OBD; mandatory core signals plus owner-selected optional signals
- Vehicle metadata: VIN when available, protocol, adapter information, OBD compliance/fuel type where supported
- Diagnostics: stored DTCs, current-cycle DTCs, freeze-frame DTC and DTC change events
- Device health: CPU, RAM, temperature, disk, connectivity and service state

## Normalized v2 observation path

Collectors now write to a thread-safe normalized observation store in addition to the backward-compatible local UI state. GPS groups, individual OBD signals, IMU samples, and device health retain their own UTC `observedAt`, `source`, `quality`, and `maxAgeMs`. A failed or unsupported OBD read never replaces a valid value with zero.

The store owns no network behavior. The local UI continues to consume `DeviceState`; the v2 frame builder consumes only the read-only observation interface. This separates acquisition, projection, persistence, and MQTT transport.

GPS uses the NMEA source timestamp when present. OBD uses callback time and canonical units. Device health uses its independent polling time. IMU samples use their acquisition time and are selected from half-open frame windows so a boundary sample cannot appear in two frames.

## Core and optional signals

`EDGE-003` splits pollable signals into mandatory core, default-on, and optional
tiers, and bounds optional selection by the polling budget that keeps `RPM` and
`SPEED` inside their freshness limit. `EDGE-004` stores owner intent per vehicle,
survives reconnect and VIN discovery, and publishes a `signalsRevision` with the
resolved selection in retained metadata. An unsupported core signal is reported as
unavailable and never substituted with zero.

Full policy, tiers, budget arithmetic and persistence rules are in
[Core and Optional Signals](15_CORE_AND_OPTIONAL_SIGNALS.md).

## MPU6050 calibration

At startup the IMU worker loads the persisted calibration when its schema, checksum-derived identity, orientation, and maximum age are valid. Missing, stale, mismatched, or invalid calibration triggers a stationary calibration. Excess acceleration variance, rotation, or a gravity magnitude far from 1 g rejects the calibration.

The calibrated file records orientation, acceleration/gyro bias, sample count, creation time, and a deterministic `calibrationVersion`. Supported mounting rotations are configured with `IMU_ORIENTATION`; the default is `x-forward-y-left-z-up`. Invalid calibration is explicit and prevents trusted IMU samples from entering frames.

## One-second frame builder

The frame-builder worker reads immutable one-second snapshots and creates one MQTT v2 `vehicle_frame` in local runtime state. A normal frame contains 20 ordered IMU tuples plus the latest GPS, OBD, and device observations with their original timestamps. The builder performs no network I/O. Each completed frame is serialized to compact JSON and appended to the durable SQLite outbox (`MQTT-003`), which retains it until a QoS-1 PUBACK arrives. MQTT v2 publication itself belongs to a later transport issue.

## Durable outbox

`outbox.sqlite3` is an append-only queue in WAL mode with `synchronous=FULL`, so a power loss cannot acknowledge a message the broker never received. Rows are drained oldest-first by capture time and deleted only after PUBACK; a failed publish records an attempt and keeps the row. The queue is bounded to 24 hours or 256 MB, evicting oldest routine frames before critical DTC, status, or metadata messages and reporting cumulative dropped counts with the first and last missing capture timestamps.

## Collection modes

`EDGE-007` chooses between an active and an idle cadence. Active is one frame per
second. Idle reduces to one frame every 30 seconds carrying an empty IMU array and
an explicit `inactiveReason`, and is entered only after 30 seconds of sustained
quiet backed by positive evidence: a connected OBD link reporting ignition off,
with zero speed and no measured motion. Absent data never counts as idle, so a
disconnected adapter or unknown ignition keeps the device active rather than
risking lost data. Any mode change, DTC change, or connection change publishes a
frame immediately, so reducing the idle cadence can never hide a transition.

## Vehicle support

`EDGE-002` records real vehicle/support combinations under
`tests/fixtures/vehicles/`. A signal is published only when the ECU advertises the
PID *and* returns a non-null response. A PID that is unsupported, or advertised
but returning null, is omitted from the frame entirely. It is never emitted as
zero, because a missing fuel level and an empty tank must stay distinguishable in
both directions.

## Transport

The publisher (`MQTT-004`) drains the outbox oldest-first over MQTT 5 with TLS and
QoS 1, deleting a row only after PUBACK. Messages delayed by an outage or a failed
attempt are republished with `replay=true` (`MQTT-005`); `messageId` and
`capturedAt` never change, only `sentAt` and the replay flag. Every publication is
checked against the device's own exact namespace before it reaches the broker, so
the device enforces the same rule as the EMQX ACL (`SEC-001`).
