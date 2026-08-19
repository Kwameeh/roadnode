# Car Telemetry — Headless Web Edition

Prototype 1 runs on a **Raspberry Pi Zero 2 W** without a local touchscreen. The Pi collects vehicle, GPS and IMU data, optionally drives the small OLED, publishes MQTT telemetry, and hosts a lightweight web app for phones/laptops on the same network.

## OBD

The OBD layer uses **python-OBD 0.7.3** and supports:

- USB ELM327 (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/serial/by-id/*`)
- Bluetooth ELM327 / Android ELM327 emulator through RFCOMM (`/dev/rfcomm0`)
- automatic transport selection
- supported-command discovery
- core live signals plus user-selected optional signals
- VIN collection when available
- stored DTCs, current-cycle DTCs and freeze-frame DTC
- protected DTC clearing

## Web app

After installation and reboot:

```bash
telemetry web-url
```

Open the shown address from a phone/laptop on the same Wi-Fi/LAN, normally:

```text
http://<pi-hostname>.local:8080
```

The web app contains Dashboard, Signals, Diagnostics, Setup/Bluetooth and System pages. Live engine state is pushed over a WebSocket at up to 5 Hz; connection health and automatic HTTP fallback are built in, so the page does not need to be refreshed.

## OLED

The optional 1.3-inch 128×64 OLED rotates through driving, GPS, vehicle-health and connectivity pages every three seconds. SH1106 and SSD1306 controllers are supported.

```bash
telemetry oled-test --driver sh1106
# If the image is offset or garbled:
telemetry oled-test --driver ssd1306
```

Set the working controller in `config/telemetry.env` with `OLED_DRIVER=sh1106` or `OLED_DRIVER=ssd1306`.

## LavinMQ cloud telemetry

Create a LavinMQ instance in CloudAMQP, then copy the MQTT hostname, port, username and password from its **MQTT Details** panel into `config/telemetry.env`. Never commit that file.

```text
MQTT_ENABLED=true
MQTT_HOST=<hostname from CloudAMQP>
MQTT_PORT=<secure MQTT port from CloudAMQP>
MQTT_CLIENT_ID=roadnode-pi-PROTO-001
MQTT_USERNAME=<username from CloudAMQP>
MQTT_PASSWORD=<password from CloudAMQP>
MQTT_TLS=true
MQTT_PUBLISH_SECONDS=3
MQTT_BUFFER_SECONDS=60
```

The three-second interval keeps one Pi publisher plus one cloud subscriber within the free plan's two-million-message monthly quota under normal operation. See `docs/22_MQTT_AND_CLOUD.md` for the topic and MongoDB ingestion contract.

## Install

```bash
git clone https://github.com/YOUR_USERNAME/car-telemetry.git
cd car-telemetry
chmod +x scripts/*.sh
./scripts/install.sh
sudo reboot
```

## Useful commands

```bash
telemetry status
telemetry web-url
telemetry obd-ports
telemetry obd-catalog
telemetry vin
telemetry dtc-refresh
telemetry oled-test --driver sh1106
telemetry bluetooth-scan --seconds 10
telemetry benchmark --seconds 600 --web-clients 5
telemetry logs -f
```

See `docs/00_DOCUMENTATION_INDEX.md`.
