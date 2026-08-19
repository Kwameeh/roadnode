# Bluetooth Setup from the Web App

The browser does not talk to the ELM327 directly. It calls the Pi web API, which asks the engine to control BlueZ.

Workflow:

1. Open Setup → Bluetooth.
2. Scan.
3. Pair the ELM327/Android emulator; enter a PIN such as `1234`/`0000` only when required by that device.
4. Select **Use as ELM**.
5. The Pi discovers the Serial Port/RFCOMM channel, saves MAC/channel, and the root RFCOMM service creates `/dev/rfcomm0`.
6. python-OBD reconnects through Bluetooth.

USB remains independently supported.
