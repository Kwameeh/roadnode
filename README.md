# RoadNode Edge

RoadNode Edge runs on a Raspberry Pi Zero 2 W. It collects GPS, MPU6050 IMU,
ELM327 OBD-II, Pi health, and optional OLED data; builds durable v2 telemetry
frames; and publishes them to the RoadNode cloud through EMQX.

The edge uses exactly one configuration file:

```text
config/telemetry.env
```

Create it only from `config/telemetry.env.example`. There is no separate cloud,
self-hosted, legacy MQTT, or credential-JSON configuration path.

## Production services

- Admin: <https://admin.obd2.ragnogroup.com>
- API health: <https://api.obd2.ragnogroup.com/health/live>
- EMQX dashboard: <https://emqx.obd2.ragnogroup.com>
- MQTT endpoint: `mqtt.obd2.ragnogroup.com:8883` with TLS
- Device topic: `roadnode/v2/devices/{DEVICE_ID}/frame`

In Cloudflare, `mqtt.obd2.ragnogroup.com` must be **DNS only** (grey cloud).
Cloudflare may proxy the three HTTPS sites, but its normal HTTP proxy does not
proxy raw MQTT on port 8883.

## 1. Prepare the cloud records in the Admin UI

1. Sign in to <https://admin.obd2.ragnogroup.com>.
2. Open **Vehicles**, click **Create vehicle**, and create `VEH-001`.
3. Open **Devices**, click **Create device**, and enter the Pi's serial number.
4. Copy all values shown once after creation:
   - device UUID;
   - MQTT username;
   - MQTT password.
5. Open the device, choose **Assign vehicle**, select `VEH-001`, enter the
   current start time and a reason, preview the assignment, then confirm it.
6. In the EMQX dashboard, create a password-authentication user whose username
   and password exactly match the Admin values.
7. Give that user publish permission only for
   `roadnode/v2/devices/{DEVICE_ID}/+` and deny all other device namespaces.

The current checked-in example already contains:

```text
DEVICE_ID=fc8c2be1-efda-4cae-a7e5-5990682f236b
VEHICLE_ID=VEH-001
MQTT_USERNAME=device-rn-0001
```

Only `MQTT_PASSWORD` still needs the password shown by Admin.

## 2. Clone and install on the Pi

```bash
cd ~
git clone https://github.com/Kwameeh/roadnode.git
cd roadnode
chmod +x scripts/*.sh
./scripts/install.sh
```

The installer creates the virtual environment, installs dependencies, copies
the one env example if needed, and installs the systemd services.

## 3. Install the MQTT CA without using Docker

Run this from the RoadNode repository on the Pi:

```bash
cd ~/roadnode
./scripts/install-cloud-mqtt-ca.sh
```

The script downloads the public RoadNode MQTT CA, verifies it, and installs it
as `/etc/roadnode/mqtt-ca.crt`. It never downloads a CA private key or server
private key.

Verify it:

```bash
sudo test -s /etc/roadnode/mqtt-ca.crt && echo "MQTT CA installed"
openssl x509 -in /etc/roadnode/mqtt-ca.crt -noout -subject -issuer -dates
```

## 4. Configure the one env file

```bash
cd ~/roadnode
cp -n config/telemetry.env.example config/telemetry.env
nano config/telemetry.env
```

Set the password returned by Admin and keep these production values exactly:

```text
MQTT_ENABLED=true
MQTT_HOST=mqtt.obd2.ragnogroup.com
MQTT_PORT=8883
MQTT_USERNAME=device-rn-0001
MQTT_PASSWORD=THE_PASSWORD_SHOWN_BY_ADMIN
MQTT_TLS=true
MQTT_CA_CERT=/etc/roadnode/mqtt-ca.crt
```

Do not type angle brackets around a real value. For example, enter
`MQTT_PASSWORD=abc123`, not `MQTT_PASSWORD=<abc123>`.

The example file documents every available setting in sections for identity,
GPS, IMU, OLED, OBD, local API/UI, MQTT, durable outbox, and runtime state.

## 5. Connect the ELM327

1. Pair the phone/ELM327 with the Pi.
2. Start the services and open the local RoadNode page shown by `telemetry
   web-url`.
3. Open **Setup / Bluetooth**, scan, select the ELM327, and connect it.
4. Confirm `/dev/rfcomm0` exists:

```bash
ls -l /dev/rfcomm0
```

If it does not exist, inspect the link service:

```bash
sudo systemctl restart car-telemetry-obd-link.service
sudo journalctl -u car-telemetry-obd-link.service -n 100 --no-pager
```

## 6. Start and verify the complete flow

Place the Pi and IMU on a completely still surface, then run:

```bash
sudo systemctl restart car-telemetry-obd-link.service
sudo systemctl restart car-telemetry.service
sudo systemctl restart car-telemetry-web.service
telemetry status
```

Expected status:

- `agent`: `running`
- `imu.calibrated`: `true`
- `gps.serialOpen`: `true` (a valid fix needs outdoor satellite visibility)
- `obd.connected`: `true` when `/dev/rfcomm0` or USB ELM327 is present
- `publisher.connected`: `true`
- `publisher.published`: increasing
- `frame.queueDepth`: decreasing after the cloud connection recovers

If the Pi is moved during IMU calibration, the service now waits and retries
automatically. Keep it still until `imu.calibrationState` becomes `valid`.

Watch live logs:

```bash
sudo journalctl -u car-telemetry.service -f
```

In EMQX, open **Clients**, select the device UUID client, and confirm traffic on
`roadnode/v2/devices/fc8c2be1-efda-4cae-a7e5-5990682f236b/frame`. In Admin,
open the device and confirm its last-frame time keeps advancing.

## Updating an existing Pi

```bash
cd ~/roadnode
git pull --ff-only origin main
./scripts/update.sh
```

`git pull` does not replace the ignored `config/telemetry.env`. Compare it with
the updated example and remove obsolete MQTT fields yourself. For this release,
make sure port `1883` and `MQTT_TLS=false` are gone; use port `8883` with TLS as
shown above.

## Useful commands

```bash
telemetry status
telemetry web-url
telemetry obd-ports
telemetry obd-catalog
telemetry vin
telemetry dtc-refresh
telemetry oled-test --driver sh1106
telemetry bluetooth-scan --seconds 10
telemetry logs -f
```

More detail is available in `docs/00_DOCUMENTATION_INDEX.md`.
