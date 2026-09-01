from car_telemetry.config import settings


CONFIG_KEYS = (
    'DEVICE_ID',
    'VEHICLE_ID',
    'IMU_ORIENTATION',
    'IMU_CALIBRATION_FILE',
    'IMU_CALIBRATION_MAX_AGE_DAYS',
    'IMU_CALIBRATION_SETTLE_SECONDS',
    'IMU_CALIBRATION_RETRY_SECONDS',
    'OLED_DRIVER',
    'OLED_ADDRESS',
    'OLED_PAGE_SECONDS',
    'OLED_CONTRAST',
    'MQTT_ENABLED',
    'MQTT_HOST',
    'MQTT_PORT',
    'MQTT_TLS',
    'OUTBOX_FILE',
    'OUTBOX_MAX_BYTES',
    'OUTBOX_MAX_AGE_SECONDS',
    'OUTBOX_BATCH_SIZE',
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
    assert configured.imu_orientation == 'x-forward-y-left-z-up'
    assert configured.imu_calibration_max_age_days == 90
    assert configured.imu_calibration_settle_seconds == 2
    assert configured.imu_calibration_retry_seconds == 5
    assert configured.oled_address == 0x3C
    assert configured.oled_page_seconds == 3
    assert configured.web_state_refresh_seconds == 0.2
    assert configured.web_heartbeat_seconds == 5
    assert configured.web_fallback_poll_seconds == 1
    assert configured.mqtt_enabled is False
    assert configured.mqtt_port == 8883
    assert configured.mqtt_tls is True


def test_explicit_env_overrides_and_normalizes_cloud_configuration(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    env_file = tmp_path / 'telemetry.env'
    env_file.write_text(
        '\n'.join(
            (
                'DEVICE_ID="Pi / Accra #1"',
                'VEHICLE_ID=GH-42',
                'OLED_DRIVER=SSD1306',
                'IMU_ORIENTATION=Y-FORWARD-X-RIGHT-Z-UP',
                f'IMU_CALIBRATION_FILE={tmp_path / "imu.json"}',
                'IMU_CALIBRATION_MAX_AGE_DAYS=30',
                'IMU_CALIBRATION_SETTLE_SECONDS=-1',
                'IMU_CALIBRATION_RETRY_SECONDS=0',
                'OLED_ADDRESS=0x3D',
                'OLED_PAGE_SECONDS=4.5',
                'OLED_CONTRAST=999',
                'MQTT_ENABLED=yes',
                'MQTT_HOST=broker.example.test',
                'MQTT_TLS=off',
                'WEB_STATE_REFRESH_SECONDS=0.05',
            )
        ),
        encoding='utf-8',
    )

    configured = settings(str(env_file))

    assert configured.device_id == 'Pi / Accra #1'
    assert configured.vehicle_id == 'GH-42'
    assert configured.oled_driver == 'ssd1306'
    assert configured.imu_orientation == 'y-forward-x-right-z-up'
    assert configured.imu_calibration_file == str(tmp_path / 'imu.json')
    assert configured.imu_calibration_max_age_days == 30
    assert configured.imu_calibration_settle_seconds == 0
    assert configured.imu_calibration_retry_seconds == 1
    assert configured.oled_address == 0x3D
    assert configured.oled_page_seconds == 4.5
    assert configured.oled_contrast == 255
    assert configured.mqtt_enabled is True
    assert configured.mqtt_host == 'broker.example.test'
    assert configured.mqtt_tls is False
    assert configured.web_state_refresh_seconds == 0.05


def test_process_environment_wins_over_env_file(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    env_file = tmp_path / 'telemetry.env'
    env_file.write_text('MQTT_HOST=file-broker\n', encoding='utf-8')
    monkeypatch.setenv('MQTT_HOST', 'process-broker')

    assert settings(str(env_file)).mqtt_host == 'process-broker'


def test_outbox_defaults_and_lower_bounds(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    monkeypatch.chdir(tmp_path)

    configured = settings()

    assert configured.outbox_file.endswith('outbox.sqlite3')
    assert configured.outbox_max_bytes == 268435456
    assert configured.outbox_max_age_seconds == 86400
    assert configured.outbox_batch_size == 50


def test_outbox_overrides_are_clamped(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('OUTBOX_FILE', str(tmp_path / 'queue.sqlite3'))
    monkeypatch.setenv('OUTBOX_MAX_BYTES', '1')
    monkeypatch.setenv('OUTBOX_MAX_AGE_SECONDS', '1')
    monkeypatch.setenv('OUTBOX_BATCH_SIZE', '0')

    configured = settings()

    assert configured.outbox_file == str(tmp_path / 'queue.sqlite3')
    assert configured.outbox_max_bytes == 1_048_576, 'a 1-byte queue is not usable'
    assert configured.outbox_max_age_seconds == 60
    assert configured.outbox_batch_size == 1
