# Testing Plan

Recommended order:

1. boot + SSH + web app
2. GPS raw/parsed data
3. I2C OLED/MPU6050
4. Bluetooth web scan/pair with Android ELM327 emulator
5. python-OBD connection and supported-command discovery
6. VIN attempt
7. live core signals and optional-signal selection
8. automatic/manual DTC scans
9. USB ELM327 transport
10. persisted IMU calibration identity, stale/invalid recovery, and configured orientation
11. one-second v2 frames with 20 ordered IMU samples and original GPS/OBD/device timestamps
12. MQTT telemetry/metadata/DTC events
13. benchmark with multiple simulated web clients
14. physical vehicle validation
