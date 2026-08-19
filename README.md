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

The web app contains Dashboard, Signals, Diagnostics, Setup/Bluetooth and System pages.

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
telemetry bluetooth-scan --seconds 10
telemetry benchmark --seconds 600 --web-clients 5
telemetry logs -f
```

See `docs/00_DOCUMENTATION_INDEX.md`.
