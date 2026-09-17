# Bluetooth Setup from the Web App

The browser does not talk to the ELM327 directly. It calls the Pi web API, which asks the engine to control BlueZ.

Workflow:

1. Open Setup → Bluetooth.
2. Scan.
3. Pair the ELM327/Android emulator; enter a PIN only when the device asks (ELM327 clones use `1234` or `1111`).
4. Select **Use as ELM**.
5. The Pi discovers the Serial Port/RFCOMM channel, saves MAC/channel, and the root RFCOMM service creates `/dev/rfcomm0`.
6. python-OBD reconnects through Bluetooth.

USB remains independently supported.

## From the command line

For the two known adapters, one command does the pairing, channel discovery, `telemetry.env` update and `/dev/rfcomm0` rebinding:

```bash
telemetry obd-profile android     # Android ELM327 Emulator (start its server first)
telemetry obd-profile obd2        # physical ELM327
telemetry obd-profile current
```

See [12_DUAL_OBD_TRANSPORT.md](12_DUAL_OBD_TRANSPORT.md#bluetooth-profiles) for what a switch does and how to troubleshoot it.
