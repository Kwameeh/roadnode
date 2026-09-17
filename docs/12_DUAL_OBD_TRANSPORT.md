# Dual OBD Transport

`OBD_TRANSPORT=auto|usb|bluetooth`.

Auto mode prefers a detected USB serial ELM327 and otherwise falls back to `/dev/rfcomm0`. USB paths may be `/dev/serial/by-id/*`, `/dev/ttyUSB*` or `/dev/ttyACM*`.

Bluetooth can be configured with a single `OBD_MAC`/`OBD_RFCOMM_CHANNEL` pair or with an ordered `OBD_BLUETOOTH_CANDIDATES` list. Candidate entries are comma-separated; use `MAC@channel` when the RFCOMM channel is known and a bare `MAC` when RoadNode should discover the Serial Port channel with SDP.

Prototype default:

```env
OBD_MAC=00:10:CC:4F:36:03
OBD_RFCOMM_CHANNEL=1
OBD_BLUETOOTH_CANDIDATES=00:10:CC:4F:36:03@1,EC:46:2C:93:7E:F4
```

The ELM327 adapter stays first. The paired phone can be used as an emulator/fallback target, but it may not expose an ELM327-compatible Serial Port service.

Both transports feed the same python-OBD service, so PID discovery, VIN, DTC and live-signal logic are shared.
