# Software Architecture

Two main services run independently:

```text
car-telemetry.service
  collectors -> normalized observation store -> one-second v2 frame builder
       |                    |
       +-> local state      +-> SQLite outbox -> MQTT v2 publisher (TLS/QoS 1)
  OLED + python-OBD + DTC/VIN + v1 MQTT + internal localhost API

car-telemetry-web.service
  FastAPI/Uvicorn + static HTML/CSS/JS + WebSocket
```

A third root service, `car-telemetry-obd-link.service`, maintains Bluetooth RFCOMM when Bluetooth OBD is configured.

The web server reads live state and proxies control operations to the engine's localhost-only API. If the web app restarts, telemetry collection continues.

Collectors depend on a narrow observation-writer interface. The pure frame builder depends on the corresponding read-only interface. Calibration persistence and MQTT transport are adapters, so sensor acquisition does not block on disk or network I/O.
