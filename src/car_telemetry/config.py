from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_auto(value: str) -> int:
    return int(value, 0)


def env_candidates(explicit: str | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    override = os.getenv("TELEMETRY_ENV")
    if override:
        paths.append(Path(override).expanduser())
    paths.extend(
        [
            Path.cwd() / "config" / "telemetry.env",
            Path.home() / "car-telemetry" / "config" / "telemetry.env",
            Path("/etc/car-telemetry/telemetry.env"),
        ]
    )
    return paths


def find_env(explicit: str | None = None) -> Path | None:
    return next((path for path in env_candidates(explicit) if path.exists()), None)


def load_env(explicit: str | None = None) -> Path | None:
    path = find_env(explicit)
    if path is None:
        return None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return path


def set_env_values(values: dict[str, str], explicit: str | None = None) -> Path:
    path = find_env(explicit)
    if path is None:
        raise FileNotFoundError(
            "telemetry.env was not found. Run ./scripts/install.sh before changing persistent settings."
        )

    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = {str(k): str(v) for k, v in values.items()}
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        output.append("")
        output.append("# Updated by car-telemetry")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    for key, value in values.items():
        os.environ[str(key)] = str(value)
    return path


@dataclass(frozen=True)
class Settings:
    device_id: str
    vehicle_id: str
    prototype_stage: int

    gps_enabled: bool
    gps_port: str
    gps_baud: int

    imu_enabled: bool
    imu_address: int
    imu_rate_hz: float
    imu_calibration_samples: int
    imu_orientation: str
    imu_calibration_file: str
    imu_calibration_max_age_days: int
    imu_calibration_settle_seconds: float
    imu_calibration_retry_seconds: float
    harsh_accel_mps2: float
    harsh_brake_mps2: float
    harsh_corner_mps2: float
    impact_g: float

    oled_enabled: bool
    oled_driver: str
    oled_i2c_bus: int
    oled_address: int
    oled_width: int
    oled_height: int
    oled_rotation: int
    oled_page_seconds: float
    oled_contrast: int

    obd_enabled: bool
    obd_transport: str
    obd_usb_port: str
    obd_bluetooth_port: str
    obd_mac: str
    obd_rfcomm_channel: int
    obd_baud: int | None
    obd_protocol: str | None
    obd_fast: bool
    obd_timeout: float
    obd_async_loop_delay: float
    obd_reconnect_seconds: float
    obd_round_trip_seconds: float

    dtc_scan_seconds: float
    dtc_max_events: int
    dtc_clear_require_engine_off: bool
    vehicle_profile_dir: str

    api_host: str
    api_port: int

    web_enabled: bool
    web_host: str
    web_port: int
    web_state_refresh_seconds: float
    web_heartbeat_seconds: float
    web_fallback_poll_seconds: float
    bluetooth_scan_seconds: int

    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    mqtt_ca_cert: str
    outbox_file: str
    outbox_max_bytes: int
    outbox_max_age_seconds: int
    outbox_batch_size: int

    status_file: str


def settings(explicit: str | None = None) -> Settings:
    load_env(explicit)

    baud_raw = os.getenv("OBD_BAUD", "auto").strip().lower()
    baud = None if baud_raw == "auto" else int(baud_raw)

    protocol_raw = os.getenv("OBD_PROTOCOL", "auto").strip()
    protocol = None if protocol_raw.lower() == "auto" else protocol_raw

    vehicle_id = os.getenv("VEHICLE_ID", "VEH-001")
    device_id = os.getenv("DEVICE_ID", "PROTO-001")
    return Settings(
        device_id=device_id,
        vehicle_id=vehicle_id,
        prototype_stage=int(os.getenv("PROTOTYPE_STAGE", "1")),
        gps_enabled=_bool("GPS_ENABLED", True),
        gps_port=os.getenv("GPS_PORT", "/dev/serial0"),
        gps_baud=int(os.getenv("GPS_BAUD", "9600")),
        imu_enabled=_bool("IMU_ENABLED", True),
        imu_address=_int_auto(os.getenv("IMU_ADDRESS", "0x68")),
        imu_rate_hz=float(os.getenv("IMU_RATE_HZ", "20")),
        imu_calibration_samples=int(os.getenv("IMU_CALIBRATION_SAMPLES", "150")),
        imu_orientation=os.getenv(
            "IMU_ORIENTATION", "x-forward-y-left-z-up"
        ).strip().lower(),
        imu_calibration_file=os.path.expanduser(
            os.getenv(
                "IMU_CALIBRATION_FILE",
                "~/.local/share/car-telemetry/imu-calibration.json",
            )
        ),
        imu_calibration_max_age_days=max(
            1, int(os.getenv("IMU_CALIBRATION_MAX_AGE_DAYS", "90"))
        ),
        imu_calibration_settle_seconds=max(
            0.0, float(os.getenv("IMU_CALIBRATION_SETTLE_SECONDS", "2"))
        ),
        imu_calibration_retry_seconds=max(
            1.0, float(os.getenv("IMU_CALIBRATION_RETRY_SECONDS", "5"))
        ),
        harsh_accel_mps2=float(os.getenv("HARSH_ACCEL_MPS2", "3.0")),
        harsh_brake_mps2=float(os.getenv("HARSH_BRAKE_MPS2", "-3.0")),
        harsh_corner_mps2=float(os.getenv("HARSH_CORNER_MPS2", "3.5")),
        impact_g=float(os.getenv("IMPACT_G", "2.5")),
        oled_enabled=_bool("OLED_ENABLED", True),
        oled_driver=os.getenv("OLED_DRIVER", "sh1106").strip().lower(),
        oled_i2c_bus=int(os.getenv("OLED_I2C_BUS", "1")),
        oled_address=_int_auto(os.getenv("OLED_ADDRESS", "0x3C")),
        oled_width=int(os.getenv("OLED_WIDTH", "128")),
        oled_height=int(os.getenv("OLED_HEIGHT", "64")),
        oled_rotation=int(os.getenv("OLED_ROTATION", "0")),
        oled_page_seconds=float(os.getenv("OLED_PAGE_SECONDS", "3")),
        oled_contrast=max(0, min(255, int(os.getenv("OLED_CONTRAST", "160")))),
        obd_enabled=_bool("OBD_ENABLED", True),
        obd_transport=os.getenv("OBD_TRANSPORT", "auto").strip().lower(),
        obd_usb_port=os.getenv("OBD_USB_PORT", "auto").strip(),
        obd_bluetooth_port=os.getenv("OBD_BLUETOOTH_PORT", "/dev/rfcomm0").strip(),
        obd_mac=os.getenv("OBD_MAC", "").strip().upper(),
        obd_rfcomm_channel=int(os.getenv("OBD_RFCOMM_CHANNEL", "1")),
        obd_baud=baud,
        obd_protocol=protocol,
        obd_fast=_bool("OBD_FAST", True),
        obd_timeout=float(os.getenv("OBD_TIMEOUT", "0.2")),
        obd_async_loop_delay=float(os.getenv("OBD_ASYNC_LOOP_DELAY", "0.10")),
        obd_reconnect_seconds=float(os.getenv("OBD_RECONNECT_SECONDS", "4")),
        # The core signal list is a product decision in `signal_policy`, not a
        # deployment tunable; what varies per install is how fast the adapter
        # answers, which is what bounds the optional signals an owner may add.
        obd_round_trip_seconds=max(
            0.01, float(os.getenv("OBD_ROUND_TRIP_SECONDS", "0.08"))
        ),
        dtc_scan_seconds=float(os.getenv("DTC_SCAN_SECONDS", "60")),
        dtc_max_events=int(os.getenv("DTC_MAX_EVENTS", "100")),
        dtc_clear_require_engine_off=_bool("DTC_CLEAR_REQUIRE_ENGINE_OFF", True),
        vehicle_profile_dir=os.path.expanduser(
            os.getenv("VEHICLE_PROFILE_DIR", "~/.local/share/car-telemetry/vehicles")
        ),
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        api_port=int(os.getenv("API_PORT", "8765")),
        web_enabled=_bool("WEB_ENABLED", True),
        web_host=os.getenv("WEB_HOST", "0.0.0.0"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        web_state_refresh_seconds=float(os.getenv("WEB_STATE_REFRESH_SECONDS", "0.2")),
        web_heartbeat_seconds=float(os.getenv("WEB_HEARTBEAT_SECONDS", "5")),
        web_fallback_poll_seconds=float(os.getenv("WEB_FALLBACK_POLL_SECONDS", "1")),
        bluetooth_scan_seconds=int(os.getenv("BLUETOOTH_SCAN_SECONDS", "10")),
        mqtt_enabled=_bool("MQTT_ENABLED", False),
        mqtt_host=os.getenv("MQTT_HOST", "").strip(),
        mqtt_port=int(os.getenv("MQTT_PORT", "8883")),
        mqtt_username=os.getenv("MQTT_USERNAME", ""),
        mqtt_password=os.getenv("MQTT_PASSWORD", ""),
        mqtt_tls=_bool("MQTT_TLS", True),
        mqtt_ca_cert=os.path.expanduser(os.getenv("MQTT_CA_CERT", "")),
        outbox_file=os.path.expanduser(
            os.getenv("OUTBOX_FILE", "~/.local/share/car-telemetry/outbox.sqlite3")
        ),
        outbox_max_bytes=max(
            1_048_576, int(os.getenv("OUTBOX_MAX_BYTES", str(256 * 1024 * 1024)))
        ),
        outbox_max_age_seconds=max(
            60, int(os.getenv("OUTBOX_MAX_AGE_SECONDS", str(24 * 60 * 60)))
        ),
        outbox_batch_size=max(1, int(os.getenv("OUTBOX_BATCH_SIZE", "50"))),
        status_file=os.path.expanduser(
            os.getenv("STATUS_FILE", "~/.local/state/car-telemetry/status.json")
        ),
    )
