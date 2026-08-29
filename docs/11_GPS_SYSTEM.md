# GPS System

Prototype 1 uses `/dev/serial0` (previously verified as `ttyS0`) at the configured baud rate. NMEA is parsed for position, speed, heading, altitude, satellites and fix state. GPS remains independent of OBD/cloud failures.

RMC date/time and GGA time are preserved as the observation time; receive time is only the fallback. Normalized GPS output uses `fix`, `headingDeg`, and `altitudeM` with `source=gps.nmea`, explicit quality, and a freshness allowance. A no-fix sentence emits `fix=false` without stale coordinates.
