# Dual OBD Transport

`OBD_TRANSPORT=auto|usb|bluetooth`.

Auto mode prefers a detected USB serial ELM327 and otherwise falls back to `/dev/rfcomm0`. USB paths may be `/dev/serial/by-id/*`, `/dev/ttyUSB*` or `/dev/ttyACM*`.

Both transports feed the same python-OBD service, so PID discovery, VIN, DTC and live-signal logic are shared.

## Bluetooth link

`car-telemetry-obd-link.service` runs `scripts/obd-link.sh` as root. Every 5 seconds, if `/dev/rfcomm0` does not exist, it binds the first working entry of `OBD_BLUETOOTH_CANDIDATES`. When that list is empty it uses `OBD_MAC`/`OBD_RFCOMM_CHANNEL`.

Candidate entries are comma-separated; use `MAC@channel` when the RFCOMM channel is known and a bare `MAC` when RoadNode should discover the Serial Port channel with SDP.

The candidate list takes priority over `OBD_MAC`, and an existing `/dev/rfcomm0` is never rebound. Changing only `OBD_MAC` therefore does not switch devices; use the profile switcher below.

## Bluetooth profiles

Two known adapters are built in:

| Profile | Device | MAC | RFCOMM | Pairing |
|---|---|---|---|---|
| `obd2` | Physical ELM327 | `00:10:CC:4F:36:03` | 1 | PIN `1234`, then `1111`, then `0000` |
| `android` | Android ELM327 Emulator app | `EC:46:2C:93:7E:F4` | 7 | Confirm on the phone |

Switch with one command, as the RoadNode user (not `sudo`):

```bash
telemetry obd-profile android     # phone running the ELM327 Emulator server
telemetry obd-profile obd2        # physical ELM327 adapter
telemetry obd-profile current     # what is bound now, and whether the ELM327 answers
telemetry obd-profile list
```

A switch:

1. enables and powers Bluetooth, pairs the device if needed and trusts it;
2. finds the Serial Port channel with `sdptool search --bdaddr MAC SP` (`obd2` falls back to channel 1; `android` stops if the emulator server is not running);
3. stops `car-telemetry` and `car-telemetry-obd-link`, runs `rfcomm release all`, and writes `OBD_ENABLED`, `OBD_TRANSPORT=bluetooth`, `OBD_BLUETOOTH_PORT`, `OBD_MAC`, `OBD_RFCOMM_CHANNEL` and `OBD_BLUETOOTH_CANDIDATES=MAC@channel` to `telemetry.env`;
4. starts the link service, checks `/dev/rfcomm0` is bound to the new MAC and channel, restarts the engine and waits up to 45 s for python-OBD to connect.

If step 1 or 2 fails, nothing is stopped or changed.

Options: `--channel N` skips SDP discovery; `--reboot` saves the profile and reboots to test a cold start; `--verify-seconds N` changes the ELM327 wait.

### Switching to the phone

1. On the phone: Bluetooth on, open the ELM327 Emulator app and start its server.
2. On the Pi: `telemetry obd-profile android`.
3. Check: `telemetry obd-profile current` shows `Profile: android` and `RFCOMM: ready`, and `rfcomm` shows `rfcomm0: EC:46:2C:93:7E:F4 channel 7`.

| Problem | Fix |
|---|---|
| `Serial Port service not found` | Start the emulator server in the app and run the command again |
| Pairing fails | `bluetoothctl remove EC:46:2C:93:7E:F4`, remove the Pi on the phone, retry |
| Channel changed | `sdptool search --bdaddr EC:46:2C:93:7E:F4 SP`, then `telemetry obd-profile android --channel N` |
| `rfcomm0` still points at the old device | Run the switch again; it releases every binding first |
| No data after binding | `sudo journalctl -u car-telemetry-obd-link.service -n 50 --no-pager`, `telemetry logs -n 50` |
