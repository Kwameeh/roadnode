from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .config import Settings
from .state import DeviceState


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


def _metadata_payload(settings: Settings, snapshot: dict) -> dict:
    obd_state = snapshot.get('obd', {})
    vehicle = obd_state.get('vehicle', {})
    return {
        'messageType': 'VEHICLE_METADATA',
        'deviceId': settings.device_id,
        'vehicleId': settings.vehicle_id,
        'vehicleProfileKey': obd_state.get('vehicleProfileKey'),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'vehicle': vehicle,
        'transport': obd_state.get('transport'),
        'protocolId': obd_state.get('protocolId'),
        'protocolName': obd_state.get('protocolName'),
        'supportedSignals': [item.get('name') for item in obd_state.get('supportedSignals', [])],
        'coreSignals': obd_state.get('coreSignals', []),
        'selectedSignals': obd_state.get('selectedSignals', []),
    }


def worker(settings: Settings, state: DeviceState, stop: threading.Event):
    state.merge(
        'mqtt',
        {
            'enabled': settings.mqtt_enabled,
            'connected': False,
            'topic': settings.mqtt_topic,
            'dtcTopic': settings.mqtt_dtc_topic,
            'metadataTopic': settings.mqtt_metadata_topic,
        },
    )
    if not settings.mqtt_enabled or not settings.mqtt_host:
        return

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings.device_id,
        protocol=mqtt.MQTTv311,
    )
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    if settings.mqtt_tls:
        client.tls_set(
            ca_certs=settings.mqtt_ca_cert or None,
            certfile=settings.mqtt_client_cert or None,
            keyfile=settings.mqtt_client_key or None,
            cert_reqs=ssl.CERT_REQUIRED,
        )

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        state.merge('mqtt', {'connected': int(reason_code) == 0, 'connectReason': str(reason_code)})

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
        state.merge('mqtt', {'connected': False, 'disconnectReason': str(reason_code)})

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(1, 30)
    client.connect_async(settings.mqtt_host, settings.mqtt_port, 60)
    client.loop_start()

    last_dtc_seq = 0
    last_metadata_hash = ''

    try:
        while not stop.is_set():
            snapshot = state.snapshot()
            obd_state = snapshot.get('obd', {})

            payload = {
                'messageType': 'TELEMETRY',
                'deviceId': settings.device_id,
                'vehicleId': settings.vehicle_id,
                'vehicleProfileKey': obd_state.get('vehicleProfileKey'),
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'gps': snapshot.get('gps', {}),
                'imu': snapshot.get('imu', {}),
                'obd': _telemetry_obd(obd_state),
                'events': snapshot.get('events', {}),
                'system': snapshot.get('system', {}),
            }

            if snapshot.get('mqtt', {}).get('connected'):
                info = client.publish(
                    settings.mqtt_topic,
                    json.dumps(payload, separators=(',', ':'), default=str),
                    qos=1,
                )

                metadata = _metadata_payload(settings, snapshot)
                metadata_json = json.dumps(metadata, sort_keys=True, separators=(',', ':'), default=str)
                metadata_hash = hashlib.sha256(metadata_json.encode('utf-8')).hexdigest()
                if metadata_hash != last_metadata_hash and obd_state.get('connected'):
                    client.publish(settings.mqtt_metadata_topic, metadata_json, qos=1, retain=True)
                    last_metadata_hash = metadata_hash

                for event in obd_state.get('dtcEvents', []):
                    try:
                        seq = int(event.get('seq', 0))
                    except Exception:
                        seq = 0
                    if seq > last_dtc_seq:
                        dtc_payload = {
                            'messageType': 'DTC_EVENT',
                            'deviceId': settings.device_id,
                            'vehicleId': settings.vehicle_id,
                            **event,
                        }
                        client.publish(
                            settings.mqtt_dtc_topic,
                            json.dumps(dtc_payload, separators=(',', ':'), default=str),
                            qos=1,
                        )
                        last_dtc_seq = max(last_dtc_seq, seq)

                state.merge(
                    'mqtt',
                    {
                        'lastPublishOk': info.rc == mqtt.MQTT_ERR_SUCCESS,
                        'lastPublishAt': time.time(),
                        'lastDtcEventSeqPublished': last_dtc_seq,
                    },
                )

            stop.wait(settings.mqtt_publish_seconds)
    finally:
        client.loop_stop()
        client.disconnect()
