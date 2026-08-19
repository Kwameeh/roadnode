# Software Architecture

Two main services run independently:

```text
car-telemetry.service
  GPS + IMU + OLED + python-OBD + DTC/VIN + MQTT + internal localhost API

car-telemetry-web.service
  FastAPI/Uvicorn + static HTML/CSS/JS + WebSocket
```

A third root service, `car-telemetry-obd-link.service`, maintains Bluetooth RFCOMM when Bluetooth OBD is configured.

The web server reads live state and proxies control operations to the engine's localhost-only API. If the web app restarts, telemetry collection continues.
