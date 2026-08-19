# GPS System

Prototype 1 uses `/dev/serial0` (previously verified as `ttyS0`) at the configured baud rate. NMEA is parsed for position, speed, heading, altitude, satellites and fix state. GPS remains independent of OBD/cloud failures.
