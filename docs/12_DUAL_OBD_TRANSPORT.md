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
3. stops `car-telemetry` and `car-telemetry-obd-link`, runs `rfcomm release all` and checks the old binding is gone (otherwise it names the program holding `/dev/rfcomm0`, restarts the previous profile and stops), and writes `OBD_ENABLED`, `OBD_TRANSPORT=bluetooth`, `OBD_BLUETOOTH_PORT`, `OBD_MAC`, `OBD_RFCOMM_CHANNEL` and `OBD_BLUETOOTH_CANDIDATES=MAC@channel` to `telemetry.env`;
4. starts the link service, checks `/dev/rfcomm0` is bound to the new MAC and channel, restarts the engine and waits up to 45 s for python-OBD to connect.

If step 1 or 2 fails, nothing is stopped or changed.

Options: `--channel N` skips SDP discovery; `--reboot` saves the profile and reboots to test a cold start; `--verify-seconds N` changes the ELM327 wait.

### Any adapter by MAC address, and saved profiles

Besides `obd2` and `android`, you can switch to any Bluetooth ELM327 by its
address. Leave out `--channel` and the RFCOMM channel is found automatically;
add `--name` to save it so you can switch by name next time:

```bash
bluetoothctl devices                                   # find the adapter's MAC
telemetry obd-profile --mac AA:BB:CC:DD:EE:FF          # switch, channel found automatically
telemetry obd-profile --mac AA:BB:CC:DD:EE:FF --channel 2   # switch with a channel you choose
telemetry obd-profile --mac AA:BB:CC:DD:EE:FF --name mycar  # switch and save as "mycar"
telemetry obd-profile mycar                            # next time: switch by name

telemetry obd-profile add mycar --mac AA:BB:CC:DD:EE:FF [--channel 2]   # save without switching
telemetry obd-profile add mycar --mac AA:BB:CC:DD:EE:FF --pin 6789 --label "Blue OBDLink"
telemetry obd-profile remove mycar
telemetry obd-profile list                             # built-in and saved profiles
```

- Names are 1-32 lower-case letters, digits, `-` or `_`, and cannot be `obd2`,
  `android`, `list`, `current`, `add` or `remove`.
- Saving with `--name` stores the channel that was used. A profile saved with
  `add` and no `--channel` discovers the channel on every switch.
- Pairing tries PIN `1234`, `1111`, then `0000` unless you give `--pin`
  (repeatable). Phones ask for confirmation on screen instead.
- Saved profiles live in `OBD_PROFILES_FILE`
  (`~/.local/share/car-telemetry/obd-profiles.json`), outside the Git checkout,
  so updates do not remove them.
- If `/dev/rfcomm0` cannot be released because another program holds it open,
  the switch stops, names that program, and restarts the previous profile.
  ModemManager is a common cause: `sudo systemctl disable --now ModemManager`.

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
