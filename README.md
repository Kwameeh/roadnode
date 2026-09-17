# RoadNode Edge

RoadNode Edge is the in-vehicle collector. It runs headless on a Raspberry Pi
Zero 2 W and:

- reads **GPS** (UART), an **MPU6050 IMU** (I2C) and an **ELM327 OBD-II**
  adapter (USB or Bluetooth) through python-OBD;
- builds one-second v2 telemetry frames, stores them in a durable SQLite
  outbox, and publishes them to the RoadNode cloud over **MQTT/TLS (EMQX)**;
- serves a **local web app** on the LAN for setup (Bluetooth, Wi-Fi, OBD
  signals, VIN/DTC) and live data;
- optionally drives a small **OLED** status display and a **safe-shutdown
  button**.

Everything is configured from one file: `config/telemetry.env`.

## Contents

1. [How it fits together](#1-how-it-fits-together)
2. [Hardware and wiring](#2-hardware-and-wiring)
3. [Prepare the SD card](#3-prepare-the-sd-card)
4. [Install RoadNode on the Pi](#4-install-roadnode-on-the-pi)
5. [Provision the device in the cloud](#5-provision-the-device-in-the-cloud)
6. [Configure `telemetry.env`](#6-configure-telemetryenv)
7. [GPS setup](#7-gps-setup)
8. [IMU (MPU6050) setup and calibration](#8-imu-mpu6050-setup-and-calibration)
9. [OLED display setup](#9-oled-display-setup)
10. [OBD-II setup: USB or Bluetooth](#10-obd-ii-setup-usb-or-bluetooth)
11. [Switching Bluetooth OBD profiles](#11-switching-bluetooth-obd-profiles)
12. [Local web app and Wi-Fi](#12-local-web-app-and-wi-fi)
13. [Start and verify the full data path](#13-start-and-verify-the-full-data-path)
14. [Updating an existing Pi](#14-updating-an-existing-pi)
15. [Command cheat sheet](#15-command-cheat-sheet)
16. [Troubleshooting](#16-troubleshooting)
17. [Development and tests](#17-development-and-tests)
18. [Further documentation](#18-further-documentation)

---

## 1. How it fits together

```text
 GPS ──UART──┐
 MPU6050 ─I2C┤                        ┌─ SQLite outbox ─ MQTT/TLS ─► EMQX ─► worker ─► MongoDB ─► Admin / owner app
 OLED ───I2C─┤   car-telemetry        │
             ├─► .service (engine) ───┼─ status.json  ─► telemetry status
 ELM327 USB ─┤   python-OBD           │
             │                        └─ local API :8765 ◄─ car-telemetry-web.service (:8080) ◄─ phone/laptop browser
 ELM327 BT ──┘
     ▲
     └── /dev/rfcomm0 ◄── car-telemetry-obd-link.service (root, obd-link.sh)
```

Three systemd services are installed and enabled at boot:

| Service | Runs as | Purpose |
|---|---|---|
| `car-telemetry.service` | your user | Engine: GPS, IMU, OBD, OLED, frames, outbox, MQTT publisher, local API |
| `car-telemetry-obd-link.service` | root | Keeps `/dev/rfcomm0` bound to the configured Bluetooth ELM327 |
| `car-telemetry-web.service` | your user | LAN web app on `WEB_PORT` (default `8080`) |

Runtime files:

| Path | Contents |
|---|---|
| `config/telemetry.env` | The only configuration file (git-ignored) |
| `~/.local/state/car-telemetry/status.json` | Live engine status (what `telemetry status` prints) |
| `~/.local/share/car-telemetry/outbox.sqlite3` | Frames waiting for MQTT acknowledgement |
| `~/.local/share/car-telemetry/imu-calibration.json` | Saved IMU calibration |
| `~/.local/share/car-telemetry/vehicles/` | Per-vehicle OBD signal profiles |
| `/etc/roadnode/mqtt-ca.crt` | Public CA used to verify the MQTT broker |

## 2. Hardware and wiring

### Parts (Prototype 1)

- Raspberry Pi Zero 2 W with a microSD card (16 GB or larger)
- GPS module with UART output (for example a NEO-6M) and antenna
- MPU6050 IMU breakout
- Optional 1.3" 128×64 OLED, SH1106 or SSD1306, I2C
- ELM327 OBD-II adapter: **USB** (plus a micro-USB OTG adapter) or **Bluetooth**
- Power bank for the Pi (do **not** connect raw vehicle 12 V to the Pi)
- Optional normally-open pushbutton for safe shutdown

### Pin map

Physical pin numbers on the 40-pin header:

| Device | Device pin | Pi pin | Pi function |
|---|---|---|---|
| GPS | VCC | 1 (3.3 V) or 2 (5 V, check your module) | Power |
| GPS | GND | 6 | Ground |
| GPS | TX | **10** | GPIO15 / UART RX |
| GPS | RX (optional) | 8 | GPIO14 / UART TX |
| MPU6050 | VCC | 1 | 3.3 V |
| MPU6050 | GND | 9 | Ground |
| MPU6050 | SDA | **3** | GPIO2 / SDA |
| MPU6050 | SCL | **5** | GPIO3 / SCL |
| OLED | VCC / GND | 17 / 14 | 3.3 V / Ground |
| OLED | SDA / SCL | 3 / 5 | Shared I2C bus with the IMU |
| Shutdown button | one leg | **7** | GPIO4 |
| Shutdown button | other leg | 39 | Ground |

Expected I2C addresses: MPU6050 `0x68`, OLED `0x3C`.

OBD connection options:

- **USB:** car OBD-II port → USB ELM327 → OTG adapter → the Pi's **USB/data**
  port (not PWR IN).
- **Bluetooth:** no wiring. The Pi's onboard Bluetooth connects to the adapter
  and the link service creates `/dev/rfcomm0`.

## 3. Prepare the SD card

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager.
2. In the Imager's settings, set:
   - hostname (for example `roadnode`)
   - username and password
   - Wi-Fi SSID/password and country
   - **Enable SSH**
3. Boot the Pi and connect:

   ```bash
   ssh <user>@roadnode.local
   ```

4. Update the OS:

   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```

## 4. Install RoadNode on the Pi

RoadNode lives in the `roadnode/` folder of the platform repository:

```bash
sudo apt install -y git
cd ~
git clone https://github.com/Kwameeh/roadnode-platform.git
cd roadnode-platform/roadnode
chmod +x scripts/*.sh
./scripts/install.sh
sudo reboot
```

> Older installs may live in `~/roadnode`. Wherever it is, the project
> directory is the one shown by `systemctl cat car-telemetry.service`. The
> rest of this README calls it `$ROADNODE`:
>
> ```bash
> export ROADNODE=~/roadnode-platform/roadnode
> ```

`install.sh` does all of this:

- installs `git`, Python venv/dev packages, `i2c-tools`, BlueZ, `rfkill`,
  Avahi (`.local` names) and imaging libraries;
- enables **SSH, I2C and the hardware UART**, and turns off the serial login
  console so the GPS can use `/dev/serial0`;
- adds your user to `dialout`, `i2c` and `bluetooth`;
- installs NetworkManager and polkit rules (`scripts/setup-network.sh`) so the
  web app can manage Wi-Fi;
- creates `.venv/` and installs RoadNode in editable mode;
- copies `config/telemetry.env.example` to `config/telemetry.env` if it does
  not exist yet;
- writes and enables the three systemd services;
- links the CLI to `/usr/local/bin/telemetry`;
- adds the GPIO4 safe-shutdown overlay to `/boot/firmware/config.txt`.

The reboot is required for group membership, UART and I2C changes.

## 5. Provision the device in the cloud

Each Pi needs its own device identity. The full operator runbook is
[`docs/platform/NEW_DEVICE_SETUP.md`](../docs/platform/NEW_DEVICE_SETUP.md);
the short version:

1. Sign in to <https://admin.obd2.ragnogroup.com> and complete MFA.
2. **Vehicles → Add vehicle** (skip if the vehicle already exists).
3. **Devices → provision a Pi collector**. Copy the one-time values:
   **Device ID** (UUID), **MQTT username**, **MQTT password**.
4. Assign the device to the vehicle.
5. On the device page, wait for **Broker access: READY** (authentication
   present and exact frame ACL). Admin configures EMQX for you.

Then install the broker CA on the Pi:

```bash
cd $ROADNODE
./scripts/install-cloud-mqtt-ca.sh
sudo test -s /etc/roadnode/mqtt-ca.crt && echo "MQTT CA installed"
openssl x509 -in /etc/roadnode/mqtt-ca.crt -noout -subject -issuer -dates
```

Only the public CA certificate is downloaded. Never copy `ca.key` or
`server.key` to a Pi.

Production endpoints:

| Service | Address |
|---|---|
| Admin | <https://admin.obd2.ragnogroup.com> |
| API health | <https://api.obd2.ragnogroup.com/health/live> |
| MQTT | `mqtt.obd2.ragnogroup.com:8883` (TLS, must be DNS-only in Cloudflare) |
| Topic | `roadnode/v2/devices/{DEVICE_ID}/frame` (derived automatically) |

## 6. Configure `telemetry.env`

Save the cloud identity without echoing the password:

```bash
cd $ROADNODE
.venv/bin/python -c '
from getpass import getpass
from pathlib import Path
from car_telemetry.config import set_env_values
values = {
    "DEVICE_ID": input("Pi Device ID from Admin: ").strip(),
    "VEHICLE_ID": input("Assigned Vehicle ID: ").strip(),
    "MQTT_USERNAME": input("MQTT username from Admin: ").strip(),
    "MQTT_PASSWORD": getpass("MQTT password from Admin: "),
    "MQTT_ENABLED": "true",
    "MQTT_HOST": "mqtt.obd2.ragnogroup.com",
    "MQTT_PORT": "8883",
    "MQTT_TLS": "true",
    "MQTT_CA_CERT": "/etc/roadnode/mqtt-ca.crt",
}
if any(not v or "\n" in v or "\r" in v for v in values.values()):
    raise SystemExit("Every value must be nonempty and on one line; nothing was changed.")
path = Path("config/telemetry.env").resolve()
path.chmod(0o600)
set_env_values(values, explicit=str(path))
print("Saved", path)
'
```

Or edit it by hand with `nano config/telemetry.env`. Write real values with no
angle brackets: `MQTT_PASSWORD=abc123`, not `MQTT_PASSWORD=<abc123>`.

Key settings (every option is documented in `config/telemetry.env.example`):

| Section | Setting | Default | Notes |
|---|---|---|---|
| Identity | `DEVICE_ID`, `VEHICLE_ID` | — | From Admin |
| GPS | `GPS_ENABLED`, `GPS_PORT`, `GPS_BAUD` | `true`, `/dev/serial0`, `9600` | |
| IMU | `IMU_ENABLED`, `IMU_ADDRESS`, `IMU_RATE_HZ` | `true`, `0x68`, `20` | |
| IMU | `IMU_ORIENTATION` | `x-forward-y-left-z-up` | See [§8](#8-imu-mpu6050-setup-and-calibration) |
| IMU events | `HARSH_ACCEL_MPS2`, `HARSH_BRAKE_MPS2`, `HARSH_CORNER_MPS2`, `IMPACT_G` | `3.0`, `-3.0`, `3.5`, `2.5` | Event thresholds |
| OLED | `OLED_ENABLED`, `OLED_DRIVER`, `OLED_ADDRESS` | `true`, `sh1106`, `0x3C` | `ssd1306` also supported |
| OLED | `OLED_DASHBOARD_SECONDS`, `OLED_PAGE_SECONDS`, `OLED_ACCESS_SECONDS` | `60`, `20`, `20` | Dashboard time; time for each other page; QR code at boot |
| OBD | `OBD_TRANSPORT` | `auto` | `auto`, `usb` or `bluetooth` |
| OBD | `OBD_MAC`, `OBD_RFCOMM_CHANNEL`, `OBD_BLUETOOTH_CANDIDATES` | physical ELM327 | See [§10](#10-obd-ii-setup-usb-or-bluetooth) |
| OBD | `OBD_BAUD`, `OBD_PROTOCOL`, `OBD_FAST` | `auto`, `auto`, `true` | |
| Web | `WEB_PORT` | `8080` | |
| MQTT | `MQTT_HOST`, `MQTT_PORT`, `MQTT_TLS`, `MQTT_CA_CERT` | production values | Keep port `8883` with TLS |
| Outbox | `OUTBOX_MAX_BYTES`, `OUTBOX_MAX_AGE_SECONDS` | 256 MiB, 24 h | Offline buffer limits |

After any manual edit, restart the engine:

```bash
sudo systemctl restart car-telemetry.service
```

## 7. GPS setup

The installer enables the UART. Check it after the reboot:

```bash
ls -l /dev/serial0                         # should point to ttyS0 on a Zero 2 W
sudo systemctl stop car-telemetry.service  # free the port for a raw read
timeout 5 cat /dev/serial0                 # should print $GPRMC / $GPGGA lines
sudo systemctl start car-telemetry.service
```

- No output: check TX→pin 10, the baud rate (`GPS_BAUD`), and that the serial
  console is off (`sudo raspi-config` → Interface Options → Serial Port: login
  shell **No**, hardware **Yes**).
- NMEA lines with empty coordinates are normal indoors. A fix needs clear sky
  and can take several minutes from a cold start.

In `telemetry status`, `gps.serialOpen` shows the port opened and `gps.validFix`
shows a valid position.

## 8. IMU (MPU6050) setup and calibration

Check the I2C bus:

```bash
sudo i2cdetect -y 1        # expect 68 (IMU) and 3c (OLED)
```

If `68` is missing, check SDA→pin 3, SCL→pin 5, 3.3 V and ground.

**Orientation.** Mount the board flat and tell RoadNode which way it points:

| `IMU_ORIENTATION` | Board X axis | Board Y axis |
|---|---|---|
| `x-forward-y-left-z-up` (default) | toward the front of the car | toward the driver's left |
| `x-backward-y-right-z-up` | toward the rear | toward the right |
| `y-forward-x-right-z-up` | toward the right | toward the front |
| `y-backward-x-left-z-up` | toward the left | toward the rear |

**Calibration.** On startup the engine records `IMU_CALIBRATION_SAMPLES`
(150) samples. The Pi must be **completely still**. If it moves, the engine
waits `IMU_CALIBRATION_RETRY_SECONDS` and tries again. The result is saved to
`IMU_CALIBRATION_FILE` and reused for `IMU_CALIBRATION_MAX_AGE_DAYS` (90).

`imu.calibrationState` in `telemetry status` can be:

| State | Meaning |
|---|---|
| `running` | Collecting samples: keep still |
| `valid` | Calibrated and in use |
| `missing` | No saved calibration yet |
| `stale` | Older than the maximum age: it will recalibrate |
| `invalid` | Wrong orientation, tampered file, or motion during sampling |

To force a fresh calibration (for example after remounting the Pi):

```bash
rm ~/.local/share/car-telemetry/imu-calibration.json
sudo systemctl restart car-telemetry.service   # then keep the Pi still
```

## 9. OLED display setup

Test the display without the engine running:

```bash
sudo systemctl stop car-telemetry.service
telemetry oled-test --driver sh1106
telemetry oled-test --driver ssd1306
sudo systemctl start car-telemetry.service
```

Whichever driver draws correctly goes in `OLED_DRIVER`. Set
`OLED_ENABLED=false` if no display is fitted.

The display rotates through eight pages in a small pixel font with icons.
The main dashboard shows for **60 s** (`OLED_DASHBOARD_SECONDS`), every other
page for **20 s** (`OLED_PAGE_SECONDS`):

| Page | Shows |
|---|---|
| 1. Dashboard (60 s) | Speed (big), RPM, coolant, volts, fuel, DTCs, cloud queue, drive mode, position, heading, Wi-Fi/BT names, IMU, CPU, uptime, Pi temperature, web address alternating with problems |
| 2. Overview | One health line each for OBD, GPS, IMU, cloud, Wi-Fi, Bluetooth and the Pi, with the reason when something is wrong |
| 3. OBD-II | RPM, speed, load, throttle, coolant, intake temp, fuel, MAF, voltage, DTC count, VIN or connection error |
| 4. GPS | Fix, satellites, lat/lon (6 decimals), UTC fix time, HDOP, speed, heading, altitude, NMEA arriving, port/baud, accuracy grade or problem |
| 5. IMU | Acceleration X/Y/Z, gyro X/Y/Z, resultant g, sensor temperature, I2C address, calibration and orientation, driving events or sensor error |
| 6. Cloud & network | EMQX broker, port/TLS, sent/replayed/rejected or error, queue, Wi-Fi, hostname, web address, dropped frames, outbox size |
| 7. Pi health | Bar graphs for CPU, RAM, SD card and temperature; RAM and SD used/total; uptime; load average; power/throttling status (`!LOW VOLTS` means a weak power supply) |
| 8. QR code | Scan to open the local web app (skipped without a network address) |

An impact or coolant ≥ 110 °C shows an inverted banner on whichever page is up.

**QR code:** the QR page opens the local web app (`http://<pi-ip>:8080`). It
also shows for `OLED_ACCESS_SECONDS` (20 s) after boot. Show it right away for
60 s with **System → Show web app QR on display** in the web app, or:

```bash
telemetry oled-qr
```

Preview every screen as PNGs on any computer:

```bash
telemetry oled-preview --out oled-preview
```

Full details: [`docs/10_SMALL_OLED_SYSTEM.md`](docs/10_SMALL_OLED_SYSTEM.md).

## 10. OBD-II setup: USB or Bluetooth

Both transports feed the same python-OBD service, so VIN, DTCs and live signals
work identically.

### USB ELM327

```bash
telemetry obd-ports          # lists /dev/serial/by-id/*, /dev/ttyUSB*, /dev/ttyACM*
```

Set `OBD_TRANSPORT=usb` (or leave `auto`: a detected USB adapter wins over
Bluetooth) and restart `car-telemetry.service`. If the adapter is missing, run
`dmesg | tail` after plugging it in and check that your user is in `dialout`.

### Bluetooth ELM327 or Android emulator

Known adapters:

| Profile | Device | MAC | RFCOMM | PIN |
|---|---|---|---|---|
| `obd2` | Physical ELM327 | `00:10:CC:4F:36:03` | 1 | `1234` or `1111` |
| `android` | Android ELM327 Emulator app | `EC:46:2C:93:7E:F4` | 7 | Confirm on the phone |

**Easiest:** use `telemetry obd-profile` ([§11](#11-switching-bluetooth-obd-profiles)).
It pairs, trusts, finds the channel, rewrites the env file and rebinds
`/dev/rfcomm0` in one step.

**From the web app:** open **Setup → Bluetooth**, scan, pair (enter the PIN
only if asked), then **Use as ELM**.

**By hand**, for a new adapter that has no profile:

```bash
bluetoothctl
  power on
  agent on
  default-agent
  scan on                      # wait for the adapter to appear
  scan off
  pair AA:BB:CC:DD:EE:FF       # PIN 1234 or 1111 for most ELM327 clones
  trust AA:BB:CC:DD:EE:FF
  quit

sdptool search --bdaddr AA:BB:CC:DD:EE:FF SP   # note "Channel: N"
telemetry bluetooth-use-elm --mac AA:BB:CC:DD:EE:FF --channel N
```

How the Bluetooth link works: `car-telemetry-obd-link.service` runs
`scripts/obd-link.sh` as root. Every 5 seconds, if `/dev/rfcomm0` is missing,
it binds the first entry that works from `OBD_BLUETOOTH_CANDIDATES` (a
comma-separated list of `MAC@channel`, or a bare `MAC` to discover the
channel). If that list is empty it uses `OBD_MAC`/`OBD_RFCOMM_CHANNEL`. The
candidate list takes priority, so update both when changing adapters by hand.

## 11. Switching Bluetooth OBD profiles

Switch between the physical ELM327 and the Android emulator without editing
files:

```bash
telemetry obd-profile list        # show profiles; the active one is marked
telemetry obd-profile current     # active profile, pairing, rfcomm0, ELM327 state
telemetry obd-profile obd2        # physical ELM327
telemetry obd-profile android     # Android emulator (start the emulator app first)
telemetry obd-profile android --channel 7   # skip SDP discovery
telemetry obd-profile obd2 --reboot         # save, then reboot to test cold start
```

Run it as your normal user, **not** with `sudo`. It calls `sudo` itself where
needed.

What a switch does:

1. Enables and powers Bluetooth, scans if the device is unknown, pairs if
   needed (the physical ELM327 tries PIN `1234`, then `1111`, then `0000`),
   and trusts the device.
2. Finds the Serial Port channel with `sdptool search --bdaddr MAC SP`. If
   that fails, `obd2` falls back to channel 1. `android` stops and tells you
   to start the emulator app.
3. Stops `car-telemetry` and `car-telemetry-obd-link`, runs
   `rfcomm release all`, and writes `OBD_TRANSPORT=bluetooth`, `OBD_MAC`,
   `OBD_RFCOMM_CHANNEL` and `OBD_BLUETOOTH_CANDIDATES` to `telemetry.env`.
4. Starts the link service, confirms `/dev/rfcomm0` points at the new MAC and
   channel, restarts the engine, and waits up to 45 s for the ELM327 to answer.

If step 1 or 2 fails, nothing is stopped or changed.

## 12. Local web app and Wi-Fi

```bash
telemetry web-url
# http://roadnode.local:8080
# http://192.168.1.42:8080
```

Open either URL from a phone or laptop on the same network, or scan the QR
code the OLED shows at boot. The web app provides:

- live GPS, IMU and OBD data (WebSocket stream);
- **Setup → Bluetooth**: scan, pair, disconnect, forget, use as ELM;
- **Setup → Wi-Fi & Internet**: scan, connect, and an internet check;
- OBD transport selection, signal selection, VIN, and DTC read/clear;
- **System → Cloud (EMQX)**: connection status and error, and the exact broker,
  client ID, username and topic the Pi publishes to, plus queued/sent/dropped
  frames. Use it to confirm the Pi is talking to `mqtt.obd2.ragnogroup.com:8883`
  (TLS) as its own device ID;
- **System → Show web app QR on display**.

**Wi-Fi notes**

- Requires NetworkManager (installed by `install.sh`/`update.sh`).
- Leave the password blank to reuse saved credentials. Open and WPA-Personal
  networks are supported; enterprise and WEP networks must be set up on the Pi.
- After switching networks, join the same network on your phone and reopen
  `http://<hostname>.local:8080`.
- "Online" means an HTTPS 204 from Google's connectivity check. "No internet"
  or "Limited access" is shown otherwise.

From the shell:

```bash
nmcli device wifi list
sudo nmcli device wifi connect "MyHotspot" password "secret"
nmcli connection show
```

The web app has no login and is meant for a trusted LAN. Anyone who can reach
it can change the Pi's connections.

## 13. Start and verify the full data path

Place the Pi on a still surface (for IMU calibration), then:

```bash
sudo systemctl restart car-telemetry-obd-link.service
sudo systemctl restart car-telemetry.service
sudo systemctl restart car-telemetry-web.service
telemetry status
```

Healthy status:

| Field | Expected |
|---|---|
| `agent` | `running` |
| `gps.serialOpen` | `true` (`gps.validFix` needs sky view) |
| `imu.calibrationState` | `valid` |
| `obd.connected` | `true` once the ELM327 reaches the car/emulator |
| `publisher.connected` | `true` (also **System → Cloud** in the web app) |
| `publisher.published` | increasing |
| `frame.queueDepth` | low, or falling after a reconnect |

Quick one-line publisher check (run it twice, ~10 s apart):

```bash
python3 -c 'import json,pathlib;d=json.load(open(pathlib.Path.home()/".local/state/car-telemetry/status.json"));p=d.get("publisher",{});f=d.get("frame",{});print({"agent":d.get("agent"),"connected":p.get("connected"),"published":p.get("published"),"error":p.get("error"),"queueDepth":f.get("queueDepth")})'
```

In Admin, open the device: the last-frame time should keep advancing and
**Worker ingestion** should be healthy. A trip only starts after real movement
or engine-on, so an idle bench test shows device health but no trip.

## 14. Updating an existing Pi

```bash
cd $ROADNODE
git pull --ff-only
./scripts/update.sh
```

`update.sh` pulls, reinstalls the package, refreshes the NetworkManager
permissions, restarts all three services and prints `telemetry status`.

`config/telemetry.env` is never overwritten. After an update, compare it with
the example for new or removed settings:

```bash
diff <(grep -o '^[A-Z_]*=' config/telemetry.env.example | sort) \
     <(grep -o '^[A-Z_]*=' config/telemetry.env | sort)
```

If a systemd template changed, re-run `./scripts/install.sh`. It keeps your
existing env file.

## 15. Command cheat sheet

### RoadNode CLI

| Command | What it does |
|---|---|
| `telemetry status` | Full live status JSON |
| `telemetry web-url` | Local web app URLs |
| `telemetry logs -f` | Follow engine logs (`-n 200` for more history) |
| `telemetry obd-profile list \| current \| obd2 \| android` | Bluetooth OBD profiles ([§11](#11-switching-bluetooth-obd-profiles)) |
| `telemetry obd-ports` | USB candidates, Bluetooth port and the selected port |
| `telemetry obd-transport auto\|usb\|bluetooth` | Change transport on the running engine and save it |
| `telemetry obd-reconnect` | Force python-OBD to reconnect |
| `telemetry obd-discover-bt --mac MAC` | Print the ELM327 RFCOMM channel from SDP |
| `telemetry obd-bind-bt --mac MAC --channel N` | Bind `/dev/rfcomm0` manually |
| `telemetry obd-catalog` | Every python-OBD command (mode, PID, name) |
| `telemetry vin` | VIN read from the vehicle |
| `telemetry dtc-refresh` | Re-read trouble codes |
| `telemetry dtc-clear --confirm` | Clear trouble codes (**engine off**) |
| `telemetry bluetooth-scan --seconds 10` | Scan through the engine |
| `telemetry bluetooth-pair --mac MAC --pin 1234` | Pair through the engine |
| `telemetry bluetooth-use-elm --mac MAC [--channel N]` | Save a Bluetooth ELM327 |
| `telemetry oled-test --driver sh1106\|ssd1306` | Draw every page and failure case on the display (stop the engine first) |
| `telemetry oled-qr [--seconds 60]` | Show the web app QR code on the display |
| `telemetry oled-preview --out DIR` | Save enlarged PNGs of every display screen |
| `telemetry benchmark --seconds 120 --web-clients 5` | Load test; writes `benchmark-report.json` |

The engine-backed commands (`obd-reconnect`, `obd-transport`, `dtc-*`,
`bluetooth-*`) need `car-telemetry.service` running.

### Services

```bash
systemctl status car-telemetry car-telemetry-obd-link car-telemetry-web --no-pager
sudo systemctl restart car-telemetry.service
sudo systemctl stop car-telemetry.service
sudo journalctl -u car-telemetry.service -f
sudo journalctl -u car-telemetry-obd-link.service -n 100 --no-pager
sudo journalctl -u car-telemetry-web.service -n 100 --no-pager
sudo journalctl -u car-telemetry.service -b     # since this boot
systemctl cat car-telemetry.service             # shows the project directory
```

### Bluetooth and RFCOMM

```bash
bluetoothctl show                     # controller powered?
bluetoothctl devices Paired
bluetoothctl info MAC                 # Paired / Trusted / Connected
bluetoothctl remove MAC               # forget, then pair again
rfkill list                           # soft/hard blocked?
sudo rfkill unblock bluetooth
sdptool search --bdaddr MAC SP        # Serial Port channel
rfcomm                                # current bindings, e.g. rfcomm0: MAC channel 1 clean
sudo rfcomm release all
ls -l /dev/rfcomm0
```

### Talk to an ELM327 directly

Stop the engine first so it is not using the port:

```bash
sudo systemctl stop car-telemetry.service
sudo apt install -y minicom
minicom -D /dev/rfcomm0 -b 38400      # or /dev/ttyUSB0
#   ATZ      reset        ATI    version     ATRV   battery voltage
#   ATSP0    auto protocol       0100   supported PIDs     010C   RPM
#   Exit: Ctrl-A then X
sudo systemctl start car-telemetry.service
```

### Hardware checks

```bash
sudo i2cdetect -y 1                  # 68 = MPU6050, 3c = OLED
ls -l /dev/serial0                   # GPS UART
vcgencmd measure_temp
vcgencmd get_throttled               # 0x0 = no under-voltage or throttling
free -h
df -h /
```

### Network and cloud

```bash
hostname -I
nmcli device status
curl -sI https://api.obd2.ragnogroup.com/health/live
openssl s_client -connect mqtt.obd2.ragnogroup.com:8883 \
  -CAfile /etc/roadnode/mqtt-ca.crt -brief </dev/null   # expect "Verification: OK"
```

### Power

```bash
sudo shutdown -h now      # or press the GPIO4 button; wait for the green LED to stop
sudo reboot
```

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `telemetry: command not found` | Installer not run | `./scripts/install.sh` |
| `telemetry.env was not found` | Running with `sudo`, or unusual install path | Run as your user, or `export TELEMETRY_ENV=$ROADNODE/config/telemetry.env` |
| `gps.serialOpen` false | UART disabled or console on serial | `raspi-config` serial settings, reboot |
| GPS open but no fix | No sky view / cold start | Go outdoors, wait several minutes |
| IMU `invalid` / stuck `running` | Motion during calibration, wiring | Keep still; `i2cdetect -y 1`; delete the calibration file and restart |
| OLED blank | Wrong driver or address | `telemetry oled-test` with both drivers; check `i2cdetect` |
| Web app Cloud badge offline | Publisher not connected | **System → Cloud (EMQX)** shows the broker, topic and error |
| `/dev/rfcomm0` missing | Link service failing or wrong MAC | `journalctl -u car-telemetry-obd-link -n 100`; `telemetry obd-profile current` |
| `rfcomm0` bound to the wrong device | Stale binding / candidate list | `telemetry obd-profile <id>` |
| `Input/output error` on rfcomm0 | Adapter unreachable, wrong channel, emulator not running, or port in use | Start the emulator / power the adapter, then `telemetry obd-profile <id>` |
| Pairing fails | Wrong PIN or old pairing | `bluetoothctl remove MAC`, retry with `1234`, then `1111` |
| `obd.connected` false with rfcomm0 bound | Ignition off / no ECU | Turn the ignition on; test with `minicom` (`ATZ`, `0100`) |
| `publisher.connected` false | DNS, port 8883, CA, credentials | `openssl s_client` check above; re-check `MQTT_*`; Admin broker access READY |
| `published` stuck, queue growing | Broker rejects the publish (ACL) | Admin → device → **Verify / Repair broker access** |
| Admin shows nothing | Device not assigned to a vehicle | Assign it in Admin |
| Web page unreachable | Different network, `.local` unsupported | Use the IP from `telemetry web-url` or the OLED |
| Random resets / slowdowns | Under-voltage | `vcgencmd get_throttled`; use a better power bank or cable |

## 17. Development and tests

On a development machine (no Pi hardware needed):

```bash
cd roadnode
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[test]"        # on non-Pi machines hardware libs may fail; tests below still run with PYTHONPATH
PYTHONPATH=src python -m pytest -q tests
```

Useful locations:

| Path | Purpose |
|---|---|
| `src/car_telemetry/engine.py` | Engine entry point (`telemetry-engine`) |
| `src/car_telemetry/cli.py` | `telemetry` CLI |
| `src/car_telemetry/obd_service.py`, `obd_transport.py` | python-OBD and USB/Bluetooth selection |
| `src/car_telemetry/bluetooth.py`, `obd_profiles.py` | BlueZ helpers and profile switching |
| `src/car_telemetry/frame_builder.py`, `outbox.py`, `publisher.py` | v2 frames, SQLite outbox, MQTT |
| `src/car_telemetry/web_app.py`, `web_static/` | LAN web app |
| `scripts/` | Install, update, CA, network, and RFCOMM link scripts |
| `systemd/` | Service templates |
| `../contracts/mqtt/v2/` | Shared frame schema and fixtures |

Regenerate the code reference after significant changes:

```bash
python scripts/generate-code-reference.py
```

## 18. Further documentation

- [`docs/00_DOCUMENTATION_INDEX.md`](docs/00_DOCUMENTATION_INDEX.md): all RoadNode edge docs
- [`docs/platform/NEW_DEVICE_SETUP.md`](../docs/platform/NEW_DEVICE_SETUP.md): full provisioning runbook, including Android collectors and credential rotation
- [`docs/22_MQTT_AND_CLOUD.md`](docs/22_MQTT_AND_CLOUD.md): MQTT, topics and offline behaviour
- [`docs/15_CORE_AND_OPTIONAL_SIGNALS.md`](docs/15_CORE_AND_OPTIONAL_SIGNALS.md): which OBD signals are collected
- [`docs/31_PERFORMANCE_BENCHMARK.md`](docs/31_PERFORMANCE_BENCHMARK.md): benchmark thresholds
- [`../docs/contracts/EDGE_MQTT_V2_CONTRACT.md`](../docs/contracts/EDGE_MQTT_V2_CONTRACT.md): frame contract
