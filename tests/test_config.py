from car_telemetry.config import settings


CONFIG_KEYS = (
    'DEVICE_ID',
    'VEHICLE_ID',
    'OLED_DRIVER',
    'OLED_ADDRESS',
    'OLED_PAGE_SECONDS',
    'OLED_CONTRAST',
    'MQTT_ENABLED',
    'MQTT_HOST',
    'MQTT_PORT',
    'MQTT_CLIENT_ID',
    'MQTT_TOPIC',
    'MQTT_DTC_TOPIC',
    'MQTT_METADATA_TOPIC',
    'MQTT_STATUS_TOPIC',
    'MQTT_TLS',
    'MQTT_PUBLISH_SECONDS',
    'MQTT_BUFFER_SECONDS',
    'WEB_STATE_REFRESH_SECONDS',
    'WEB_HEARTBEAT_SECONDS',
    'WEB_FALLBACK_POLL_SECONDS',
    'TELEMETRY_ENV',
)


def clear_config(monkeypatch):
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_realtime_oled_and_mqtt_defaults(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    monkeypatch.chdir(tmp_path)

    configured = settings()

    assert configured.oled_driver == 'sh1106'
    assert configured.oled_address == 0x3C
    assert configured.oled_page_seconds == 3
    assert configured.web_state_refresh_seconds == 0.2
    assert configured.web_heartbeat_seconds == 5
    assert configured.web_fallback_poll_seconds == 1
    assert configured.mqtt_enabled is False
    assert configured.mqtt_port == 8883
    assert configured.mqtt_tls is True
    assert configured.mqtt_publish_seconds == 3
    assert configured.mqtt_buffer_seconds == 60
    assert configured.mqtt_client_id == 'roadnode-pi-PROTO-001'
    assert configured.mqtt_topic == 'roadnode/v1/vehicles/VEH-001/telemetry'


def test_explicit_env_overrides_and_normalizes_cloud_configuration(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    env_file = tmp_path / 'telemetry.env'
    env_file.write_text(
        '\n'.join(
            (
                'DEVICE_ID="Pi / Accra #1"',
                'VEHICLE_ID=GH-42',
                'OLED_DRIVER=SSD1306',
                'OLED_ADDRESS=0x3D',
                'OLED_PAGE_SECONDS=4.5',
                'OLED_CONTRAST=999',
                'MQTT_ENABLED=yes',
                'MQTT_HOST=broker.example.test',
                'MQTT_TLS=off',
                'MQTT_PUBLISH_SECONDS=0.01',
                'MQTT_BUFFER_SECONDS=0',
                'WEB_STATE_REFRESH_SECONDS=0.05',
            )
        ),
        encoding='utf-8',
    )

    configured = settings(str(env_file))

    assert configured.device_id == 'Pi / Accra #1'
    assert configured.vehicle_id == 'GH-42'
    assert configured.oled_driver == 'ssd1306'
    assert configured.oled_address == 0x3D
    assert configured.oled_page_seconds == 4.5
    assert configured.oled_contrast == 255
    assert configured.mqtt_enabled is True
    assert configured.mqtt_host == 'broker.example.test'
    assert configured.mqtt_tls is False
    assert configured.mqtt_publish_seconds == 0.2
    assert configured.mqtt_buffer_seconds == 1.0
    assert configured.mqtt_client_id == 'roadnode-pi-Pi-Accra-1'
    assert configured.mqtt_topic == 'roadnode/v1/vehicles/GH-42/telemetry'
    assert configured.mqtt_status_topic == 'roadnode/v1/vehicles/GH-42/status'
    assert configured.web_state_refresh_seconds == 0.05


def test_process_environment_wins_over_env_file(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    env_file = tmp_path / 'telemetry.env'
    env_file.write_text('MQTT_HOST=file-broker\n', encoding='utf-8')
    monkeypatch.setenv('MQTT_HOST', 'process-broker')

    assert settings(str(env_file)).mqtt_host == 'process-broker'
