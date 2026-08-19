# File Structure

```text
car-telemetry/
├── README.md
├── pyproject.toml
├── config/
│   └── telemetry.env.example
├── src/car_telemetry/
│   ├── engine.py
│   ├── web_app.py
│   ├── api_server.py
│   ├── engine_client.py
│   ├── obd_service.py
│   ├── obd_transport.py
│   ├── bluetooth.py
│   ├── gps.py
│   ├── imu.py
│   ├── oled.py
│   ├── mqtt_client.py
│   ├── system_monitor.py
│   ├── vehicle_profiles.py
│   ├── state.py
│   ├── config.py
│   ├── common.py
│   ├── cli.py
│   ├── benchmark.py
│   └── web_static/
│       ├── index.html
│       ├── styles.css
│       └── app.js
├── scripts/
│   ├── install.sh
│   ├── update.sh
│   ├── obd-link.sh
│   └── benchmark.sh
├── systemd/
│   ├── car-telemetry.service.template
│   ├── car-telemetry-obd-link.service.template
│   └── car-telemetry-web.service.template
├── tests/
│   └── test_pure.py
├── docs/
│   ├── 00_DOCUMENTATION_INDEX.md
│   ├── 01_PROJECT_OVERVIEW.md
│   ├── ...
│   ├── 32_HEADLESS_WEB_REVISION.md
│   ├── FILE_STRUCTURE.md
│   └── CODE_REFERENCE.md
└── .github/workflows/ci.yml
```

The telemetry engine and LAN web app are separate systemd processes. `obd-link.sh` runs as root only to maintain Bluetooth RFCOMM.
