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
    harsh_accel_mps2: float
    harsh_brake_mps2: float
    harsh_corner_mps2: float
    impact_g: float

    oled_enabled: bool
    oled_address: int
    oled_width: int
    oled_height: int

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
    obd_core_signals: tuple[str, ...]

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
    bluetooth_scan_seconds: int

    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_topic: str
    mqtt_dtc_topic: str
    mqtt_metadata_topic: str
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    mqtt_ca_cert: str
    mqtt_client_cert: str
    mqtt_client_key: str
    mqtt_publish_seconds: float

    status_file: str


def settings(explicit: str | None = None) -> Settings:
    load_env(explicit)

    baud_raw = os.getenv("OBD_BAUD", "auto").strip().lower()
    baud = None if baud_raw == "auto" else int(baud_raw)

    protocol_raw = os.getenv("OBD_PROTOCOL", "auto").strip()
    protocol = None if protocol_raw.lower() == "auto" else protocol_raw

    core = tuple(
        item.strip().upper()
        for item in os.getenv(
            "OBD_CORE_SIGNALS",
            "RPM,SPEED,COOLANT_TEMP,ENGINE_LOAD,THROTTLE_POS,CONTROL_MODULE_VOLTAGE,FUEL_LEVEL,INTAKE_TEMP,MAF",
        ).split(",")
        if item.strip()
    )

    vehicle_id = os.getenv("VEHICLE_ID", "VEH-001")

    return Settings(
        device_id=os.getenv("DEVICE_ID", "PROTO-001"),
        vehicle_id=vehicle_id,
        prototype_stage=int(os.getenv("PROTOTYPE_STAGE", "1")),
        gps_enabled=_bool("GPS_ENABLED", True),
        gps_port=os.getenv("GPS_PORT", "/dev/serial0"),
        gps_baud=int(os.getenv("GPS_BAUD", "9600")),
        imu_enabled=_bool("IMU_ENABLED", True),
        imu_address=_int_auto(os.getenv("IMU_ADDRESS", "0x68")),
        imu_rate_hz=float(os.getenv("IMU_RATE_HZ", "20")),
        imu_calibration_samples=int(os.getenv("IMU_CALIBRATION_SAMPLES", "150")),
        harsh_accel_mps2=float(os.getenv("HARSH_ACCEL_MPS2", "3.0")),
        harsh_brake_mps2=float(os.getenv("HARSH_BRAKE_MPS2", "-3.0")),
        harsh_corner_mps2=float(os.getenv("HARSH_CORNER_MPS2", "3.5")),
        impact_g=float(os.getenv("IMPACT_G", "2.5")),
        oled_enabled=_bool("OLED_ENABLED", True),
        oled_address=_int_auto(os.getenv("OLED_ADDRESS", "0x3C")),
        oled_width=int(os.getenv("OLED_WIDTH", "128")),
        oled_height=int(os.getenv("OLED_HEIGHT", "64")),
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
        obd_core_signals=core,
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
        web_state_refresh_seconds=float(os.getenv("WEB_STATE_REFRESH_SECONDS", "0.5")),
        bluetooth_scan_seconds=int(os.getenv("BLUETOOTH_SCAN_SECONDS", "10")),
        mqtt_enabled=_bool("MQTT_ENABLED", False),
        mqtt_host=os.getenv("MQTT_HOST", "").strip(),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_topic=os.getenv("MQTT_TOPIC", f"vehicles/{vehicle_id}/telemetry"),
        mqtt_dtc_topic=os.getenv("MQTT_DTC_TOPIC", f"vehicles/{vehicle_id}/dtc"),
        mqtt_metadata_topic=os.getenv("MQTT_METADATA_TOPIC", f"vehicles/{vehicle_id}/metadata"),
        mqtt_username=os.getenv("MQTT_USERNAME", ""),
        mqtt_password=os.getenv("MQTT_PASSWORD", ""),
        mqtt_tls=_bool("MQTT_TLS", False),
        mqtt_ca_cert=os.path.expanduser(os.getenv("MQTT_CA_CERT", "")),
        mqtt_client_cert=os.path.expanduser(os.getenv("MQTT_CLIENT_CERT", "")),
        mqtt_client_key=os.path.expanduser(os.getenv("MQTT_CLIENT_KEY", "")),
        mqtt_publish_seconds=float(os.getenv("MQTT_PUBLISH_SECONDS", "2")),
        status_file=os.path.expanduser(
            os.getenv("STATUS_FILE", "~/.local/state/car-telemetry/status.json")
        ),
    )
