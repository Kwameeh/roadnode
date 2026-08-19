# Internal Engine API

The telemetry engine exposes a localhost-only JSON control API on `127.0.0.1:8765` by default. The network-facing web service proxies requests to it.

It covers state, signals, vehicle metadata, DTCs, OBD transport, Bluetooth scan/pair/use, reconnect and protected DTC clearing. The internal API should not be exposed directly to the LAN.
