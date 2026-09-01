# File Structure

```text
roadnode/
|-- README.md
|-- pyproject.toml
|-- config/
|   `-- telemetry.env.example
|-- src/car_telemetry/
|   |-- engine.py
|   |-- frame_builder.py
|   |-- publisher.py
|   |-- outbox.py
|   |-- gps.py
|   |-- imu.py
|   |-- imu_calibration.py
|   |-- obd_service.py
|   |-- obd_transport.py
|   |-- oled.py
|   |-- system_monitor.py
|   |-- api_server.py
|   |-- web_app.py
|   |-- config.py
|   `-- web_static/
|-- scripts/
|   |-- install.sh
|   |-- update.sh
|   |-- install-cloud-mqtt-ca.sh
|   `-- obd-link.sh
|-- systemd/
|-- tests/
`-- docs/
```

The telemetry engine and LAN web app are separate systemd processes.
`obd-link.sh` runs as root only to maintain the Bluetooth RFCOMM link. The
engine builds v2 frames into SQLite and `publisher.py` sends them to EMQX.
