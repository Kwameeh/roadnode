import time
from dataclasses import replace
import json

import paho.mqtt.client as mqtt

from car_telemetry.config import settings
from car_telemetry import mqtt_client
from car_telemetry.mqtt_client import MemoryOutbox, make_message, metadata_body
from car_telemetry.state import DeviceState


def mqtt_settings():
    return replace(
        settings(),
        device_id='PROTO-001',
        vehicle_id='VEH-001',
        mqtt_enabled=True,
        mqtt_host='broker.example.test',
        mqtt_port=8883,
        mqtt_client_id='roadnode-pi-PROTO-001',
        mqtt_topic='roadnode/v1/vehicles/VEH-001/telemetry',
        mqtt_dtc_topic='roadnode/v1/vehicles/VEH-001/dtc',
        mqtt_metadata_topic='roadnode/v1/vehicles/VEH-001/metadata',
        mqtt_status_topic='roadnode/v1/vehicles/VEH-001/status',
        mqtt_username='roadnode-user',
        mqtt_password='secret',
        mqtt_tls=True,
        mqtt_ca_cert='',
        mqtt_client_cert='',
        mqtt_client_key='',
        mqtt_publish_seconds=0.2,
        mqtt_buffer_seconds=60,
    )


class ControlledStop:
    def __init__(self, waits_until_stop=1):
        self.waits_until_stop = waits_until_stop
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) >= self.waits_until_stop:
            self.stopped = True
        return self.stopped


class FakePublishInfo:
    def __init__(self, rc=mqtt.MQTT_ERR_SUCCESS, published=True):
        self.rc = rc
        self.published = published
        self.waited = False

    def wait_for_publish(self, timeout=None):
        self.waited = True
        self.timeout = timeout
        return True

    def is_published(self):
        return self.published


class FakeClient:
    def __init__(self, *args, connect=True, publish_rc=mqtt.MQTT_ERR_SUCCESS, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.connect_on_start = connect
        self.publish_rc = publish_rc
        self.on_connect = None
        self.on_disconnect = None
        self.credentials = None
        self.tls = None
        self.will = None
        self.reconnect_delay = None
        self.connect = None
        self.published = []
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.lifecycle = []
        self.max_inflight = None
        self.max_queued = None

    def max_inflight_messages_set(self, value):
        self.max_inflight = value

    def max_queued_messages_set(self, value):
        self.max_queued = value

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, json.loads(payload), qos, retain)

    def reconnect_delay_set(self, minimum, maximum):
        self.reconnect_delay = (minimum, maximum)

    def connect_async(self, host, port, keepalive):
        self.connect = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True
        self.lifecycle.append('loop_start')
        if self.connect_on_start:
            self.on_connect(self, None, None, 0, None)

    def publish(self, topic, payload, qos, retain):
        info = FakePublishInfo(self.publish_rc)
        self.published.append((topic, json.loads(payload), qos, retain, info))
        return info

    def loop_stop(self):
        self.loop_stopped = True
        self.lifecycle.append('loop_stop')

    def disconnect(self):
        self.disconnected = True
        self.lifecycle.append('disconnect')


def vehicle_state():
    state = DeviceState('PROTO-001', 'VEH-001', 1)
    state.merge(
        'obd',
        {
            'connected': True,
            'transport': 'usb',
            'vehicle': {'VIN': 'TESTVIN'},
            'supportedSignals': [{'name': 'RPM'}, {'name': 'SPEED'}],
            'selectedSignals': ['RPM'],
            'signals': {'RPM': {'value': 1800}},
            'dtcEvents': [
                {'seq': 4, 'code': 'P0300', 'timestamp': '2026-08-19T12:01:00+00:00'}
            ],
        },
    )
    state.merge('gps', {'validFix': True, 'latitude': 5.6, 'longitude': -0.18})
    return state


def test_message_contract_has_stable_identity_and_capture_time():
    message = make_message(
        mqtt_settings(),
        'session-a',
        7,
        'TELEMETRY',
        {'gps': {'validFix': True}},
        '2026-08-19T12:00:00+00:00',
    )
    assert message['schemaVersion'] == 1
    assert message['messageId'] == 'PROTO-001:session-a:7'
    assert message['capturedAt'] == '2026-08-19T12:00:00+00:00'
    assert message['sentAt'] is None
    assert message['gps']['validFix'] is True


def test_metadata_body_does_not_change_with_publish_time():
    snapshot = {
        'obd': {
            'connected': True,
            'vehicle': {'VIN': 'TESTVIN'},
            'supportedSignals': [{'name': 'RPM'}],
        }
    }
    assert metadata_body(snapshot) == metadata_body(snapshot)
    assert 'timestamp' not in metadata_body(snapshot)


def test_memory_outbox_coalesces_metadata_and_expires_old_messages():
    outbox = MemoryOutbox(max_age_seconds=60, publish_seconds=3)
    outbox.put('metadata', {'messageId': 'old'}, retain=True, key='metadata')
    outbox.put('metadata', {'messageId': 'new'}, retain=True, key='metadata')
    assert len(outbox) == 1
    assert outbox.peek()['message']['messageId'] == 'new'
    outbox.items[0]['queuedMonotonic'] = time.monotonic() - 61
    outbox.prune()
    assert len(outbox) == 0
    assert outbox.dropped == 1


def test_memory_outbox_is_bounded_and_drops_oldest():
    outbox = MemoryOutbox(max_age_seconds=1, publish_seconds=1)
    for index in range(outbox.max_messages + 3):
        outbox.put('telemetry', {'messageId': str(index)})
    assert len(outbox) == outbox.max_messages
    assert outbox.dropped == 3


def test_mqtt_worker_configures_secure_client_publishes_and_shuts_down(monkeypatch):
    fake = FakeClient()
    constructor = {}

    def client_factory(*args, **kwargs):
        constructor['args'] = args
        constructor['kwargs'] = kwargs
        return fake

    monkeypatch.setattr(mqtt_client.mqtt, 'Client', client_factory)
    state = vehicle_state()

    mqtt_client.worker(mqtt_settings(), state, ControlledStop(waits_until_stop=2))

    assert constructor['kwargs']['client_id'] == 'roadnode-pi-PROTO-001'
    assert constructor['kwargs']['clean_session'] is True
    assert constructor['kwargs']['protocol'] == mqtt.MQTTv311
    assert constructor['kwargs']['callback_api_version'] == mqtt.CallbackAPIVersion.VERSION2
    assert fake.credentials == ('roadnode-user', 'secret')
    assert fake.tls['cert_reqs'] is not None
    assert fake.reconnect_delay == (1, 30)
    assert fake.max_inflight == 1
    assert fake.max_queued > 0
    assert fake.connect == ('broker.example.test', 8883, 60)
    assert fake.loop_started and fake.loop_stopped and fake.disconnected
    assert fake.lifecycle[-2:] == ['disconnect', 'loop_stop']

    will_topic, will, will_qos, will_retain = fake.will
    assert will_topic == mqtt_settings().mqtt_status_topic
    assert (will['online'], will['reason']) == (False, 'connection-lost')
    assert (will_qos, will_retain) == (1, True)

    messages = [item[1] for item in fake.published]
    types = [message['messageType'] for message in messages]
    assert types.count('TELEMETRY') == 2
    assert types.count('VEHICLE_METADATA') == 1
    assert types.count('DTC_EVENT') == 1
    assert types.count('DEVICE_STATUS') == 2
    assert messages[0]['online'] is True
    assert messages[-1]['reason'] == 'clean-shutdown'
    assert messages[-1]['online'] is False
    assert fake.published[-1][4].waited is True

    metadata_publish = next(item for item in fake.published if item[1]['messageType'] == 'VEHICLE_METADATA')
    dtc_publish = next(item for item in fake.published if item[1]['messageType'] == 'DTC_EVENT')
    assert metadata_publish[:1] == (mqtt_settings().mqtt_metadata_topic,)
    assert metadata_publish[2:4] == (1, True)
    assert dtc_publish[0] == mqtt_settings().mqtt_dtc_topic
    assert dtc_publish[1]['capturedAt'] == '2026-08-19T12:01:00+00:00'

    sequences = [message['sequence'] for message in messages]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    mqtt_state = state.snapshot()['mqtt']
    assert mqtt_state['connected'] is True
    assert mqtt_state['bufferedMessages'] == 0
    assert mqtt_state['lastDtcEventSeqQueued'] == 4
    assert mqtt_state['lastMessageId'].startswith('PROTO-001:')


def test_mqtt_worker_buffers_in_memory_while_offline(monkeypatch):
    fake = FakeClient(connect=False)
    monkeypatch.setattr(mqtt_client.mqtt, 'Client', lambda *args, **kwargs: fake)
    state = vehicle_state()

    mqtt_client.worker(mqtt_settings(), state, ControlledStop())

    assert fake.published == []
    assert fake.loop_stopped and fake.disconnected
    mqtt_state = state.snapshot()['mqtt']
    assert mqtt_state['connected'] is False
    assert mqtt_state['bufferedMessages'] == 3
    assert mqtt_state['lastDtcEventSeqQueued'] == 4


def test_mqtt_worker_keeps_message_buffered_without_puback(monkeypatch):
    class MissingAckClient(FakeClient):
        def publish(self, topic, payload, qos, retain):
            message = json.loads(payload)
            info = FakePublishInfo(
                mqtt.MQTT_ERR_SUCCESS,
                published=message['messageType'] != 'TELEMETRY',
            )
            self.published.append((topic, message, qos, retain, info))
            return info

    fake = MissingAckClient()
    monkeypatch.setattr(mqtt_client.mqtt, 'Client', lambda *args, **kwargs: fake)
    state = vehicle_state()

    mqtt_client.worker(mqtt_settings(), state, ControlledStop(waits_until_stop=2))

    telemetry_messages = [
        item for item in fake.published if item[1]['messageType'] == 'TELEMETRY'
    ]
    assert len(telemetry_messages) == 1
    telemetry = telemetry_messages[0]
    assert telemetry[4].waited is True
    mqtt_state = state.snapshot()['mqtt']
    assert mqtt_state['lastPublishOk'] is False
    assert mqtt_state['bufferedMessages'] == 4


def test_mqtt_worker_returns_without_creating_client_when_disabled(monkeypatch):
    def fail_client(*_args, **_kwargs):
        raise AssertionError('MQTT client should not be created')

    monkeypatch.setattr(mqtt_client.mqtt, 'Client', fail_client)
    state = vehicle_state()
    mqtt_client.worker(
        replace(mqtt_settings(), mqtt_enabled=False), state, ControlledStop()
    )

    mqtt_state = state.snapshot()['mqtt']
    assert mqtt_state['enabled'] is False
    assert mqtt_state['connected'] is False
    assert mqtt_state['bufferedMessages'] == 0
