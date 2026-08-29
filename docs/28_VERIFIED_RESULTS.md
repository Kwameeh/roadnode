# Verified Results

Previously verified on Prototype 1 hardware:

- GPS UART device and NMEA reception
- Pi Zero 2 W onboard Bluetooth pairing
- Android ELM327 emulator Serial Port SDP service
- RFCOMM serial communication
- ELM327 command/response and simulated OBD data

The new python-OBD headless-web/VIN/DTC revision still needs to be deployed and validated on the Pi and then on a real vehicle.

Verified in automated tests on the development host:

- normalized GPS/OBD/IMU/device timestamps, units, source, quality, and frame-window selection
- deterministic calibration identity, persistence, orientation, tamper detection, and stale/invalid states
- MQTT v2 full/partial/idle/replay/drop contract validation
- one-second combined frame projection with 20 ordered IMU samples

Physical orientation/calibration repeatability and sustained 20 Hz sampling still require Pi Zero 2 W and installed-vehicle evidence.
