from __future__ import annotations

import json
import logging
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .config import Settings
from .device_identity import (
    CredentialError,
    DeviceCredential,
    assert_publish_allowed,
)
from .observations import parse_utc, utc_iso, utc_now
from .outbox import OutboxItem, SqliteOutbox
from .state import DeviceState

LOGGER = logging.getLogger(__name__)

CONTENT_TYPE = "application/json"
PAYLOAD_FORMAT_UTF8 = 1
DEFAULT_REPLAY_AFTER_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 60.0


def credential_from_settings(settings: Settings) -> DeviceCredential:
    """Build the v2 device credential from the single telemetry.env file."""
    username = settings.mqtt_username.strip()
    password = settings.mqtt_password
    if not username or not password:
        raise CredentialError(
            "MQTT_USERNAME and MQTT_PASSWORD must both be set in telemetry.env"
        )
    return DeviceCredential(
        device_id=settings.device_id,
        username=username,
        secret=password,
        credential_version=1,
        issued_at=utc_now(),
        expires_at=None,
        revoked_at=None,
    )


class PublishRejected(RuntimeError):
    """The broker refused the message; the outbox row must be kept."""


@dataclass(frozen=True)
class PublishResult:
    acknowledged: bool
    reason: str | None = None


class Transport(Protocol):
    """Minimal MQTT surface the drain loop depends on."""

    @property
    def connected(self) -> bool: ...

    def connect(self) -> None: ...

    def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
        content_type: str,
        payload_format_indicator: int,
        message_expiry_interval: int | None = None,
    ) -> PublishResult: ...

    def disconnect(self) -> None: ...


# --- replay semantics (MQTT-005) -------------------------------------------


def should_replay(
    item: OutboxItem,
    *,
    now: datetime,
    replay_after_seconds: float = DEFAULT_REPLAY_AFTER_SECONDS,
) -> bool:
    """A message resent after a failed attempt or a connection gap is a replay."""
    if item.attempts > 0:
        return True
    age = (now - parse_utc(item.captured_at)).total_seconds()
    return age > replay_after_seconds


def prepare_for_send(
    payload: bytes,
    *,
    sent_at: str,
    replay: bool,
) -> bytes:
    """Stamp send-time fields, preserving identity and capture time.

    Only ``sentAt`` and ``replay`` may change between the original publication
    and a replay. ``messageId`` and ``capturedAt`` are never rewritten.
    """
    document = json.loads(payload.decode("utf-8"))
    document["sentAt"] = sent_at
    document["replay"] = replay
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


# --- drain loop (MQTT-004) --------------------------------------------------


@dataclass
class DrainReport:
    published: int = 0
    failed: int = 0
    rejected: int = 0
    replayed: int = 0


def drain_once(
    outbox: SqliteOutbox,
    transport: Transport,
    *,
    device_id: str,
    batch_size: int = 50,
    now: datetime | None = None,
    replay_after_seconds: float = DEFAULT_REPLAY_AFTER_SECONDS,
) -> DrainReport:
    """Publish oldest-first; delete a row only after PUBACK.

    A message the broker refuses on authorization grounds is dropped, because
    retrying it would block the queue forever. Every other failure keeps the
    row and records an attempt.
    """
    report = DrainReport()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    for item in outbox.batch(batch_size):
        if not transport.connected:
            break

        # Enforce the same exact-namespace rule the broker ACL applies.
        try:
            assert_publish_allowed(device_id, item.topic)
        except Exception as exc:
            LOGGER.error("dropping unpublishable message %s: %s", item.message_id, exc)
            outbox.delete(item.message_id)
            report.rejected += 1
            continue

        replay = should_replay(
            item, now=current, replay_after_seconds=replay_after_seconds
        )
        try:
            payload = prepare_for_send(
                item.payload, sent_at=utc_iso(current), replay=replay
            )
        except (ValueError, UnicodeDecodeError) as exc:
            LOGGER.error("dropping malformed message %s: %s", item.message_id, exc)
            outbox.delete(item.message_id)
            report.rejected += 1
            continue

        try:
            result = transport.publish(
                item.topic,
                payload,
                qos=item.qos,
                retain=item.retain,
                content_type=CONTENT_TYPE,
                payload_format_indicator=PAYLOAD_FORMAT_UTF8,
            )
        except Exception as exc:  # transport error: keep the row
            LOGGER.warning("publish failed for %s: %s", item.message_id, exc)
            outbox.record_attempt(item.message_id)
            report.failed += 1
            break

        if result.acknowledged:
            outbox.delete(item.message_id)
            report.published += 1
            if replay:
                report.replayed += 1
        else:
            LOGGER.warning(
                "no PUBACK for %s: %s", item.message_id, result.reason or "timeout"
            )
            outbox.record_attempt(item.message_id)
            report.failed += 1
            break

    return report


# --- paho transport ---------------------------------------------------------


class PahoTransport:
    """MQTT 5 over TLS with QoS-1 acknowledgement waiting."""

    def __init__(
        self,
        settings: Settings,
        credential: DeviceCredential,
        *,
        ack_timeout: float = 10.0,
    ):
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self._settings = settings
        self._credential = credential
        self._ack_timeout = ack_timeout
        self._client = mqtt.Client(
            client_id=credential.device_id,
            protocol=mqtt.MQTTv5,
            callback_api_version=getattr(
                mqtt, "CallbackAPIVersion", None
            ).VERSION2
            if hasattr(mqtt, "CallbackAPIVersion")
            else None,
        )
        self._client.username_pw_set(credential.username, credential.secret)
        if settings.mqtt_tls:
            self._client.tls_set(
                ca_certs=settings.mqtt_ca_cert or None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        self._connected = threading.Event()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None):
        if getattr(reason_code, "is_failure", False):
            LOGGER.error("broker refused connection: %s", reason_code)
            return
        self._connected.set()

    def _on_disconnect(self, *_args, **_kwargs):
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self) -> None:
        self._client.connect(
            self._settings.mqtt_host, self._settings.mqtt_port, keepalive=60
        )
        self._client.loop_start()
        self._connected.wait(timeout=self._ack_timeout)

    def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
        content_type: str,
        payload_format_indicator: int,
        message_expiry_interval: int | None = None,
    ) -> PublishResult:
        from paho.mqtt.properties import Properties
        from paho.mqtt.packettypes import PacketTypes

        properties = Properties(PacketTypes.PUBLISH)
        properties.ContentType = content_type
        properties.PayloadFormatIndicator = payload_format_indicator
        if message_expiry_interval is not None:
            properties.MessageExpiryInterval = message_expiry_interval

        info = self._client.publish(
            topic, payload, qos=qos, retain=retain, properties=properties
        )
        try:
            info.wait_for_publish(timeout=self._ack_timeout)
        except (ValueError, RuntimeError) as exc:
            return PublishResult(acknowledged=False, reason=str(exc))
        if info.is_published():
            return PublishResult(acknowledged=True)
        return PublishResult(acknowledged=False, reason="no PUBACK before timeout")

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        finally:
            self._connected.clear()


# --- worker -----------------------------------------------------------------


def worker(
    settings: Settings,
    state: DeviceState,
    stop: threading.Event,
    *,
    outbox: SqliteOutbox | None = None,
    transport: Transport | None = None,
    credential: DeviceCredential | None = None,
    now=None,
) -> None:
    """Drain the outbox until stopped, reconnecting with backoff."""
    if not settings.mqtt_enabled or not settings.mqtt_host:
        state.merge("publisher", {"enabled": False, "connected": False})
        return

    clock = now or (lambda: datetime.now(timezone.utc))
    queue = outbox if outbox is not None else SqliteOutbox(settings.outbox_file)
    owns_queue = outbox is None

    owns_transport = transport is None

    def build_transport() -> Transport:
        resolved = credential or credential_from_settings(settings)
        return PahoTransport(settings, resolved)

    backoff = 1.0
    state.merge("publisher", {"enabled": True, "connected": False, "published": 0})
    published_total = 0

    while not stop.is_set():
        try:
            # Built lazily so a missing credential retries instead of killing
            # the thread before the backoff loop starts.
            if transport is None:
                transport = build_transport()
            if not transport.connected:
                transport.connect()
                state.merge("publisher", {"connected": transport.connected})
                if not transport.connected:
                    raise ConnectionError("broker connection not established")
                backoff = 1.0

            report = drain_once(
                queue,
                transport,
                device_id=settings.device_id,
                batch_size=settings.outbox_batch_size,
                now=clock().astimezone(timezone.utc),
            )
            published_total += report.published
            stats = queue.stats()
            state.merge(
                "publisher",
                {
                    "connected": transport.connected,
                    "published": published_total,
                    "replayed": report.replayed,
                    "rejected": report.rejected,
                    "queueDepth": stats.depth,
                    "queueBytes": stats.bytes_used,
                    "error": None,
                },
            )
            if report.failed:
                stop.wait(min(backoff, MAX_BACKOFF_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            elif report.published == 0:
                stop.wait(0.5)
        except Exception as exc:
            state.merge("publisher", {"connected": False, "error": str(exc)})
            stop.wait(min(backoff, MAX_BACKOFF_SECONDS))
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    if owns_transport and transport is not None:
        try:
            transport.disconnect()
        except Exception:  # pragma: no cover - shutdown best effort
            pass
    if owns_queue:
        queue.close()
