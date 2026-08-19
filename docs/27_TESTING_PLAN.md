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
10. MQTT telemetry/metadata/DTC events
11. benchmark with multiple simulated web clients
12. physical vehicle validation
