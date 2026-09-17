# Small OLED System

The optional 1.3-inch 128×64 monochrome OLED (`OLED_DRIVER=sh1106` or `ssd1306`)
is a glanceable status and debugging display. Configuration still happens in the
LAN web app.

## One constant dashboard

Nothing rotates. Every value from the old five pages is on one screen, drawn
with a built-in 5×7 pixel font and 7×7 icons (`oled_font.py`), so text stays sharp
on a 1-bit display.

```text
row  content
 0   🚗 OBD · 📍GPS + satellites · ☁ cloud · WiFi · BT · IMU axes        Pi °C
1-2  SPEED (double size)      RPM · coolant °  /  volts · fuel %
 3   KM/H (or GPS)            ⚠ stored DTCs · ⇪ outbox queue        ACTIVE/IDLE
 4   📍 lat,lon                                            heading ° + compass
 5   WiFi SSID                                                  BT device name
 6   IMU ✓ g-force / CAL % / ✗ state                           CPU % · uptime
 7   web app address, taking turns every 3 s with any problem
```

Status icons: steady = working, blinking = problem, `-` = disabled or not used.

Speed comes from OBD; if OBD is down but GPS has a fix, GPS speed is shown and
labelled `GPS`.

### Problem line (row 7)

Problems are listed most important first and alternate with the web address:

| Message | Meaning |
|---|---|
| `!NO NETWORK - JOIN WIFI` | No IP address |
| `!OBD BT I/O ERROR`, `!OBD NO RFCOMM0 LINK`, `!OBD NO ECU - IGNITION ON?` | OBD not connected, with the reason |
| `!CLOUD TLS CERT FAILED`, `!CLOUD AUTH REJECTED`, `!CLOUD DNS FAILED`, `!CLOUD TIMEOUT` | EMQX publisher not connected |
| `!GPS PORT NOT OPEN`, `!GPS NO DATA - CHECK TX` | GPS serial problems |
| `!IMU CAL 64% KEEP STILL` | IMU calibration running or invalid |

Cloud status comes from the engine's `publisher` section and the queue from
`frame.queueDepth`, the same values `telemetry status` prints.

### Alerts

A possible impact or coolant ≥ 110 °C draws an inverted banner over rows 1–3
(`IMPACT` / `HOT 112°`). The status row, location, links and problem line stay
visible. Alerts are never hidden behind the QR screen.

## Web app QR code

The QR screen shows a scannable `http://<pi-ip>:<WEB_PORT>` code on the left
and the address and Wi-Fi name on the right. It appears:

- at boot, for `OLED_ACCESS_SECONDS` (20 s by default, `0` to skip);
- on request for 60 s: **System → Show web app QR on display** in the web app,
  or `telemetry oled-qr --seconds 60`.

Without a network address there is no link, so the dashboard stays up.

## Burn-in

The image shifts one pixel sideways every 30 seconds.

## Testing without the car

```bash
telemetry oled-preview --out oled-preview   # enlarged PNGs of every scenario
sudo systemctl stop car-telemetry.service
telemetry oled-test --driver sh1106         # draws the same scenarios on the display
telemetry oled-test --driver ssd1306
sudo systemctl start car-telemetry.service
```

`OLED_PAGE_SECONDS` is no longer used and can be removed from `telemetry.env`.
