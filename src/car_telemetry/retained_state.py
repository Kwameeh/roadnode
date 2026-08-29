"""Retained metadata and status, plus the broker last will (MQTT-007).

Frames are transient; metadata and status are retained, so a subscriber that
connects late still learns what a device is and whether it is online. Two rules
shape this module:

- retained status is never proof that telemetry is fresh. A device that lost
  power leaves `online` retained until the broker publishes the will, so
  freshness is always computed from timestamps instead.
- metadata is published only when it changes. Republishing an unchanged
  document on every boot would make "last changed" meaningless.

Signal selection lives in `signal_selection`, which supplies the body and its
own `signals.revision`. This module only decides whether a body is new enough
to be worth resending.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .device_identity import topic_for
from .observations import utc_now

SCHEMA_VERSION = 2

ONLINE = "online"
OFFLINE = "offline"
SHUTTING_DOWN = "shutting_down"
DEGRADED = "degraded"
VALID_STATUS = frozenset({ONLINE, OFFLINE, SHUTTING_DOWN, DEGRADED})


@dataclass(frozen=True)
class RetainedMessage:
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = True

    def document(self) -> dict[str, Any]:
        return json.loads(self.payload.decode("utf-8"))


def _encode(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def metadata_revision(body: dict[str, Any]) -> str:
    """Stable identity of a metadata document, ignoring key order."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_metadata(
    device_id: str,
    body: dict[str, Any],
    *,
    sent_at: str | None = None,
) -> RetainedMessage:
    """Retained description of what this device is and can measure."""
    revision = metadata_revision(body)
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "messageType": "metadata",
        "deviceId": device_id,
        "revision": revision,
        "sentAt": sent_at or utc_now(),
        "body": body,
    }
    return RetainedMessage(topic=topic_for(device_id, "metadata"), payload=_encode(document))


def build_status(
    device_id: str,
    state: str,
    *,
    queue_depth: int = 0,
    reason: str = "",
    sensor_health: dict[str, Any] | None = None,
    sent_at: str | None = None,
) -> RetainedMessage:
    if state not in VALID_STATUS:
        raise ValueError(f"unsupported status: {state}")
    document: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "messageType": "status",
        "deviceId": device_id,
        "state": state,
        "queueDepth": int(queue_depth),
        "reason": reason,
        "sensorHealth": sensor_health or {},
        "sentAt": sent_at or utc_now(),
    }
    return RetainedMessage(topic=topic_for(device_id, "status"), payload=_encode(document))


def build_last_will(device_id: str) -> RetainedMessage:
    """The broker publishes this when the device disappears unexpectedly.

    It deliberately carries no `sentAt`: the device is not sending it, and
    stamping a device time would misrepresent when the disconnect happened.
    The server records its own receive time instead.
    """
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "messageType": "status",
        "deviceId": device_id,
        "state": OFFLINE,
        "reason": "unexpected_disconnect",
        "queueDepth": None,
        "sensorHealth": {},
        "sentAt": None,
    }
    return RetainedMessage(topic=topic_for(device_id, "status"), payload=_encode(document))


class RetainedStatePublisher:
    """Tracks what has been published so unchanged documents are not resent."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._last_metadata_revision: str | None = None
        self._last_status: str | None = None

    @property
    def last_metadata_revision(self) -> str | None:
        return self._last_metadata_revision

    def metadata_if_changed(
        self, body: dict[str, Any], *, sent_at: str | None = None
    ) -> RetainedMessage | None:
        """Returns a message only when the metadata actually changed."""
        revision = metadata_revision(body)
        if revision == self._last_metadata_revision:
            return None
        self._last_metadata_revision = revision
        return build_metadata(self.device_id, body, sent_at=sent_at)

    def status_if_changed(
        self,
        state: str,
        *,
        queue_depth: int = 0,
        reason: str = "",
        sensor_health: dict[str, Any] | None = None,
        sent_at: str | None = None,
        force: bool = False,
    ) -> RetainedMessage | None:
        """Publish on a state transition, or on the periodic heartbeat.

        `force` covers the 30-second heartbeat, which republishes an unchanged
        state so queue depth and sensor health stay current.
        """
        if state not in VALID_STATUS:
            raise ValueError(f"unsupported status: {state}")
        if state == self._last_status and not force:
            return None
        self._last_status = state
        return build_status(
            self.device_id,
            state,
            queue_depth=queue_depth,
            reason=reason,
            sensor_health=sensor_health,
            sent_at=sent_at,
        )

    def reset(self) -> None:
        """After a reconnect the broker's retained state may be stale."""
        self._last_metadata_revision = None
        self._last_status = None


def freshness_is_unprovable(status_document: dict[str, Any]) -> bool:
    """True when a status document cannot establish telemetry freshness.

    A retained `online` with no send time is exactly what a stale retained
    message looks like, so callers must fall back to frame timestamps.
    """
    return status_document.get("sentAt") is None
