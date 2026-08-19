from __future__ import annotations

import hashlib
import json
import math
import ssl
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .config import Settings
from .state import DeviceState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _telemetry_obd(obd_state: dict) -> dict:
    return {
        'connected': obd_state.get('connected', False),
        'connecting': obd_state.get('connecting', False),
        'transport': obd_state.get('transport'),
        'port': obd_state.get('port'),
        'protocolId': obd_state.get('protocolId'),
        'protocolName': obd_state.get('protocolName'),
        'vehicleProfileKey': obd_state.get('vehicleProfileKey'),
        'coreSignals': obd_state.get('coreSignals', []),
        'selectedSignals': obd_state.get('selectedSignals', []),
        'signals': obd_state.get('signals', {}),
        'dtc': obd_state.get('dtc', {}),
        'error': obd_state.get('error'),
    }


def metadata_body(snapshot: dict) -> dict:
    obd_state = snapshot.get('obd', {})
    return {
        'vehicleProfileKey': obd_state.get('vehicleProfileKey'),
        'vehicle': obd_state.get('vehicle', {}),
        'transport': obd_state.get('transport'),
        'protocolId': obd_state.get('protocolId'),
        'protocolName': obd_state.get('protocolName'),
        'supportedSignals': [item.get('name') for item in obd_state.get('supportedSignals', [])],
        'coreSignals': obd_state.get('coreSignals', []),
        'selectedSignals': obd_state.get('selectedSignals', []),
    }


def telemetry_body(snapshot: dict) -> dict:
    return {
        'gps': snapshot.get('gps', {}),
        'imu': snapshot.get('imu', {}),
        'obd': _telemetry_obd(snapshot.get('obd', {})),
        'events': snapshot.get('events', {}),
        'system': snapshot.get('system', {}),
    }


def make_message(
    settings: Settings,
    session_id: str,
    sequence: int,
    message_type: str,
    body: dict,
    captured_at: str | None = None,
) -> dict:
    message_id = f'{settings.device_id}:{session_id}:{sequence}'
    return {
        'schemaVersion': 1,
        'messageId': message_id,
        'messageType': message_type,
        'deviceId': settings.device_id,
        'vehicleId': settings.vehicle_id,
        'sessionId': session_id,
        'sequence': sequence,
        'capturedAt': captured_at or utc_now(),
        'sentAt': None,
        **deepcopy(body),
    }


class MemoryOutbox:
    """A bounded, age-limited, in-memory MQTT queue. It never touches disk."""

    def __init__(self, max_age_seconds: float, publish_seconds: float):
        self.max_age_seconds = max(0.0, max_age_seconds)
        telemetry_slots = math.ceil(self.max_age_seconds / max(0.2, publish_seconds))
        self.max_messages = max(1, telemetry_slots * 4 + 32)
        self.items: deque[dict[str, Any]] = deque()
        self.dropped = 0

    def _expired(self, item: dict, now: float) -> bool:
        return self.max_age_seconds <= 0 or now - item['queuedMonotonic'] > self.max_age_seconds

    def prune(self, now: float | None = None):
        current = time.monotonic() if now is None else now
        while self.items and self._expired(self.items[0], current):
            self.items.popleft()
            self.dropped += 1

    def put(self, topic: str, message: dict, retain: bool = False, key: str | None = None):
        now = time.monotonic()
        self.prune(now)
        if key:
            self.items = deque(item for item in self.items if item.get('key') != key)
        if self.max_age_seconds <= 0:
            self.dropped += 1
            return
        self.items.append(
            {
                'topic': topic,
                'message': message,
                'retain': retain,
                'key': key,
                'queuedMonotonic': now,
            }
        )
        while len(self.items) > self.max_messages:
            self.items.popleft()
            self.dropped += 1

    def peek(self) -> dict | None:
        self.prune()
        return self.items[0] if self.items else None

    def pop(self):
        if self.items:
            self.items.popleft()

    def __len__(self):
        self.prune()
        return len(self.items)


def worker(settings: Settings, state: DeviceState, stop: threading.Event):
    state.merge(
        'mqtt',
        {
            'enabled': settings.mqtt_enabled,
            'connected': False,
            'clientId': settings.mqtt_client_id,
            'topic': settings.mqtt_topic,
            'dtcTopic': settings.mqtt_dtc_topic,
            'metadataTopic': settings.mqtt_metadata_topic,
            'statusTopic': settings.mqtt_status_topic,
            'bufferSeconds': settings.mqtt_buffer_seconds,
            'bufferedMessages': 0,
            'droppedMessages': 0,
        },
    )
    if not settings.mqtt_enabled or not settings.mqtt_host:
        return

    session_id = uuid.uuid4().hex[:16]
    sequence = 0
    sequence_lock = threading.Lock()
    outbox = MemoryOutbox(settings.mqtt_buffer_seconds, settings.mqtt_publish_seconds)
    connected = threading.Event()

    def next_message(message_type: str, body: dict, captured_at: str | None = None) -> dict:
        nonlocal sequence
        with sequence_lock:
            sequence += 1
            current_sequence = sequence
        return make_message(settings, session_id, current_sequence, message_type, body, captured_at)

    def publish_status(client, online: bool, reason: str):
        message = next_message('DEVICE_STATUS', {'online': online, 'reason': reason})
        message['sentAt'] = utc_now()
        return client.publish(
            settings.mqtt_status_topic,
            json.dumps(message, separators=(',', ':'), default=str),
            qos=1,
            retain=True,
        )

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        ok = int(reason_code) == 0
        if ok:
            connected.set()
            publish_status(_client, True, 'connected')
        else:
            connected.clear()
        state.merge(
            'mqtt',
            {
                'connected': ok,
                'connectReason': str(reason_code),
                'sessionId': session_id,
                'error': None if ok else f'Broker rejected connection: {reason_code}',
            },
        )

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
        connected.clear()
        state.merge('mqtt', {'connected': False, 'disconnectReason': str(reason_code)})

    client = None
    while client is None and not stop.is_set():
        candidate = None
        try:
            candidate = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=settings.mqtt_client_id,
                clean_session=True,
                protocol=mqtt.MQTTv311,
            )
            candidate.max_inflight_messages_set(1)
            candidate.max_queued_messages_set(outbox.max_messages)
            if settings.mqtt_username:
                candidate.username_pw_set(settings.mqtt_username, settings.mqtt_password)
            if settings.mqtt_tls:
                candidate.tls_set(
                    ca_certs=settings.mqtt_ca_cert or None,
                    certfile=settings.mqtt_client_cert or None,
                    keyfile=settings.mqtt_client_key or None,
                    cert_reqs=ssl.CERT_REQUIRED,
                )

            offline = next_message('DEVICE_STATUS', {'online': False, 'reason': 'connection-lost'})
            offline['sentAt'] = utc_now()
            candidate.will_set(
                settings.mqtt_status_topic,
                json.dumps(offline, separators=(',', ':'), default=str),
                qos=1,
                retain=True,
            )
            candidate.on_connect = on_connect
            candidate.on_disconnect = on_disconnect
            candidate.reconnect_delay_set(1, 30)
            candidate.connect_async(settings.mqtt_host, settings.mqtt_port, 60)
            candidate.loop_start()
            client = candidate
            state.merge('mqtt', {'error': None})
        except Exception as exc:
            state.merge('mqtt', {'connected': False, 'error': f'MQTT setup failed: {exc}'})
            if candidate is not None:
                try:
                    candidate.disconnect()
                    candidate.loop_stop()
                except Exception:
                    pass
            stop.wait(5.0)

    if client is None:
        return

    last_dtc_seq = 0
    last_metadata_hash = ''

    def queue(topic: str, message: dict, retain: bool = False, key: str | None = None):
        outbox.put(topic, message, retain, key)
        state.merge(
            'mqtt',
            {'bufferedMessages': len(outbox), 'droppedMessages': outbox.dropped},
        )

    def drain():
        while connected.is_set() and not stop.is_set():
            item = outbox.peek()
            if item is None:
                break
            info = item.get('publishInfo')
            payload = item.get('publishedPayload')
            if info is None:
                payload = deepcopy(item['message'])
                payload['sentAt'] = utc_now()
                info = client.publish(
                    item['topic'],
                    json.dumps(payload, separators=(',', ':'), default=str),
                    qos=1,
                    retain=item['retain'],
                )
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    item['publishInfo'] = info
                    item['publishedPayload'] = payload
            ok = info.rc == mqtt.MQTT_ERR_SUCCESS
            if ok:
                try:
                    info.wait_for_publish(timeout=max(1.0, settings.mqtt_publish_seconds))
                    ok = info.is_published()
                except (RuntimeError, ValueError):
                    ok = False
            if not ok:
                state.merge('mqtt', {'lastPublishOk': False})
                break
            outbox.pop()
            state.merge(
                'mqtt',
                {
                    'lastPublishOk': True,
                    'lastPublishAt': time.time(),
                    'lastMessageId': payload['messageId'],
                    'bufferedMessages': len(outbox),
                    'droppedMessages': outbox.dropped,
                },
            )

    try:
        while not stop.is_set():
            captured_at = utc_now()
            snapshot = state.snapshot()
            obd_state = snapshot.get('obd', {})
            queue(
                settings.mqtt_topic,
                next_message('TELEMETRY', telemetry_body(snapshot), captured_at),
            )

            metadata = metadata_body(snapshot)
            metadata_json = json.dumps(metadata, sort_keys=True, separators=(',', ':'), default=str)
            metadata_hash = hashlib.sha256(metadata_json.encode('utf-8')).hexdigest()
            if metadata_hash != last_metadata_hash and obd_state.get('connected'):
                queue(
                    settings.mqtt_metadata_topic,
                    next_message('VEHICLE_METADATA', metadata, captured_at),
                    retain=True,
                    key='metadata',
                )
                last_metadata_hash = metadata_hash

            for event in obd_state.get('dtcEvents', []):
                try:
                    event_seq = int(event.get('seq', 0))
                except (TypeError, ValueError):
                    event_seq = 0
                if event_seq > last_dtc_seq:
                    queue(
                        settings.mqtt_dtc_topic,
                        next_message('DTC_EVENT', event, event.get('timestamp') or captured_at),
                    )
                    last_dtc_seq = max(last_dtc_seq, event_seq)

            drain()
            state.merge(
                'mqtt',
                {
                    'bufferedMessages': len(outbox),
                    'droppedMessages': outbox.dropped,
                    'lastDtcEventSeqQueued': last_dtc_seq,
                },
            )
            stop.wait(settings.mqtt_publish_seconds)
    finally:
        if connected.is_set():
            try:
                info = publish_status(client, False, 'clean-shutdown')
                info.wait_for_publish(timeout=2.0)
            except Exception:
                pass
        try:
            client.disconnect()
        finally:
            client.loop_stop()
