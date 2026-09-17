# Command Reference

```bash
telemetry status
telemetry web-url
telemetry obd-ports
telemetry obd-catalog
telemetry vin
telemetry obd-transport auto
telemetry obd-reconnect
telemetry dtc-refresh
telemetry dtc-clear --confirm
telemetry oled-test --driver sh1106
telemetry oled-test --driver ssd1306
telemetry oled-qr --seconds 60
telemetry oled-preview --out oled-preview
telemetry bluetooth-scan --seconds 10
telemetry bluetooth-pair --mac AA:BB:CC:DD:EE:FF --pin 1234
telemetry bluetooth-use-elm --mac AA:BB:CC:DD:EE:FF
telemetry benchmark --seconds 600 --web-clients 5
telemetry logs -f
```

Most setup operations can also be performed from the LAN web app.

## Switching the Bluetooth OBD adapter

```bash
telemetry obd-profile list        # known profiles
telemetry obd-profile current     # active profile, pairing, rfcomm0 binding, ELM327 state
telemetry obd-profile obd2        # physical ELM327  00:10:CC:4F:36:03, RFCOMM 1
telemetry obd-profile android     # ELM327 Emulator  EC:46:2C:93:7E:F4, RFCOMM 7
telemetry obd-profile android --channel 7   # skip SDP discovery
telemetry obd-profile android --reboot      # save, then cold-boot test
```

Run it as the RoadNode user (not with `sudo`); it calls `sudo` itself for
`systemctl` and `rfcomm`. A switch:

1. Enables Bluetooth, powers the controller, pairs if needed (the physical
   ELM327 tries PIN `1234`, then `1111`, then `0000`) and trusts the device.
2. Discovers the Serial Port channel with `sdptool search --bdaddr MAC SP`.
   The physical adapter falls back to channel 1 if SDP fails; the Android
   profile aborts, since no SPP service means the emulator app is not running.
3. Stops `car-telemetry.service` and `car-telemetry-obd-link.service`, runs
   `rfcomm release all`, and rewrites `OBD_TRANSPORT`, `OBD_MAC`,
   `OBD_RFCOMM_CHANNEL` and `OBD_BLUETOOTH_CANDIDATES` in `telemetry.env`.
4. Starts the link service, confirms `/dev/rfcomm0` is bound to the new MAC and
   channel, restarts the engine, and waits for python-OBD to connect.

Failures in steps 1–2 leave the running profile untouched.
