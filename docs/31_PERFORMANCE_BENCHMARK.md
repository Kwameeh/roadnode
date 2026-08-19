# Performance Benchmark

The benchmark now represents the **headless web architecture**, not the removed touchscreen.

It simulates concurrently:

- 20 Hz IMU
- GPS
- heavy live OBD callback flow
- VIN/DTC state and periodic DTC changes
- MQTT payload serialization
- optional OLED rendering
- status-file writes
- event logging
- FastAPI/Uvicorn library footprint
- live state/API/WebSocket serialization for multiple browser clients
- an extra touched RAM reserve

Run:

```bash
telemetry benchmark --seconds 120 --web-clients 5
```

Longer test:

```bash
telemetry benchmark --seconds 600 --stress 1 --web-clients 5
```

Stress/headroom test:

```bash
telemetry benchmark --seconds 300 --stress 2 --web-clients 10
```

The report includes process RSS, minimum system available RAM, system CPU, temperature, Raspberry Pi throttling flags, worker deadline misses and web-stream p95 work time. It writes `benchmark-report.json` by default.

PASS/WARN/FAIL thresholds intentionally keep RAM and scheduling headroom rather than merely checking whether the process survived.
