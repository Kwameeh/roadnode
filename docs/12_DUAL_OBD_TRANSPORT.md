# Dual OBD Transport

`OBD_TRANSPORT=auto|usb|bluetooth`.

Auto mode prefers a detected USB serial ELM327 and otherwise falls back to `/dev/rfcomm0`. USB paths may be `/dev/serial/by-id/*`, `/dev/ttyUSB*` or `/dev/ttyACM*`.

Both transports feed the same python-OBD service, so PID discovery, VIN, DTC and live-signal logic are shared.
