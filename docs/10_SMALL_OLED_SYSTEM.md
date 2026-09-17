# Small OLED System

The optional 1.3-inch 128×64 monochrome OLED (`OLED_DRIVER=sh1106` or `ssd1306`)
is a glanceable status and debugging display. Configuration still happens in the
LAN web app.

Everything is drawn with a built-in 5×7 pixel font and 7×7 icons
(`oled_font.py`), so text stays sharp on a 1-bit display and every page fits
seven full rows of data.

## Rotating pages

The pages rotate in this order. The main dashboard shows for
`OLED_DASHBOARD_SECONDS` (**60 s**), every other page for `OLED_PAGE_SECONDS`
(**20 s**), so one full cycle takes 3 min 20 s:

| # | Page | Time |
|---|---|---|
| 1 | Dashboard | 60 s |
| 2 | Overview | 20 s |
| 3 | OBD-II | 20 s |
| 4 | GPS | 20 s |
| 5 | IMU | 20 s |
| 6 | Cloud & network | 20 s |
| 7 | Raspberry Pi health | 20 s |
| 8 | Web app QR code | 20 s (skipped without a network address) |

Pages 1-7 share the status row on top: icons for OBD, GPS (with satellite
count), cloud, Wi-Fi, Bluetooth and IMU (steady = working, blinking = problem,
`-` = disabled or not used). The dashboard shows the Pi temperature on the
right; the other pages show dots marking which page is up.

### 1. Dashboard

```text
🚗 📍9 ☁ WiFi BT IMU          47°C
 72   ⟳2450 🌡91°
      ⚡13.9V 💧64%
KM/H  ⚠0 ⇪0            ACTIVE
📍5.6037,-0.1870     241°SW
WiFi ROADNODE-WIFI    BT OBDII
IMU ✓ 0.04G        18% 2H14M
192.168.1.42:8080
```

Speed (from OBD, or GPS labelled `GPS` when OBD is down), RPM, coolant,
voltage, fuel, stored trouble codes, cloud queue, driving mode, position,
heading, Wi-Fi and Bluetooth names, IMU state and g-force, CPU and uptime. The
bottom line takes turns between the web address and any problems, for example
`!OBD BT I/O ERROR`, `!CLOUD TLS CERT FAILED`, `!NO NETWORK - JOIN WIFI`,
`!IMU CAL 64% KEEP STILL`.

### 2. Overview

```text
🚗OBD ✓            BT 2450RPM
📍GPS ✓        9 SAT HDOP 0.9
IMU ✓                   0.04G
☁CLOUD ✓         Q0 SENT 1200
WiFi ✓          ROADNODE-WIFI
BT ✓                    OBDII
🌡PI ✓               18% 47°C
```

One health line per part of the system. When something is wrong the mark
turns to ✗ and the right side says why (`NO ECU/IGN OFF`, `NO FIX 3 SAT`,
`TIMEOUT`, `NO NETWORK`, `!LOW VOLTS`).

### 3. OBD-II

```text
🚗OBD-II ✓                BT
⟳RPM 2450             SPD 72
LOAD 34%             THR 18%
🌡COOL 91°            INT 32°
💧FUEL 64%            MAF 5.2
⚡13.9V                ⚠DTC 0
VIN 1HGCM82633A004352
```

Engine speed, vehicle speed, engine load, throttle, coolant and intake
temperature, fuel level, mass air flow, voltage (ECU, or the adapter's own
reading when the ECU does not report it) and stored DTC count. The last row
shows the VIN (or protocol), or the connection error when OBD is down.

### 4. GPS

```text
📍GPS ✓ FIX            9 SAT
LAT 5.603712        14:32:05
LON -0.187012       HDOP 0.9
SPD 71KM/H            241°SW
ALT 34M               NMEA ✓
/DEV/SERIAL0            9600
ACCURACY EXCELLENT
```

Fix, satellites, latitude/longitude to six decimals, fix time (UTC), HDOP,
speed, heading with compass point, altitude, whether NMEA sentences are
arriving, serial port and baud, and an accuracy grade from HDOP. Position,
altitude and HDOP are hidden once the fix is lost. The last row explains
problems: port closed, no NMEA data (check the TX wire), or needs sky view.

### 5. IMU

```text
IMU ✓ MPU6050           0X68
ACCEL M/S2             0.04G
X+0.12 Y-0.03 Z+0.01
GYRO RAD/S              36°C
X+0.00 Y-0.01 Z+0.00
CAL ✓              X-FORWARD
EVENTS NONE
```

Linear acceleration and gyro on all three axes, resultant g, sensor
temperature, I2C address, calibration state (or progress while calibrating)
with mounting orientation, and active driving events (`IMPACT`, `BRAKE`,
`ACCEL`, `CORNER`) or the sensor error.

### 6. Cloud & network

```text
☁CLOUD ✓                  ⇪0
OBD2.RAGNOGROUP.COM
8883 TLS           SENT 1200
REPLAY 0            REJECT 0
WiFi ROADNODE-WIFI  ROADNODE
192.168.1.42:8080
DROPPED 0        OUTBOX 0.0M
```

EMQX broker, port and TLS, frames sent, replayed and rejected (or the
connection error), queue count, Wi-Fi network, hostname, web address, dropped
frames and outbox size.

### 7. Raspberry Pi health

```text
PI ROADNODE         UP 2H14M
CPU 18%       [███░░░░░░░░]
RAM 49%       [█████░░░░░░]
SD 33%        [████░░░░░░░]
🌡47°C         [██████░░░░░]
RAM 204/416M     SD 4.8/15G
LOAD 0.42 /4           PWR ✓
```

Bar graphs for CPU, RAM, SD card use and CPU temperature (full at 85 °C, where
the Pi throttles), RAM and SD used/total, uptime, 1-minute load average with
the core count, and power status from `vcgencmd get_throttled`:

| Status | Meaning |
|---|---|
| `PWR ✓` | No power or thermal problems since boot |
| `!LOW VOLTS` | Under-voltage right now: use a better supply or cable |
| `!THROTTLED` / `!HOT LIMIT` / `!CPU CAPPED` | The Pi is slowing down now |
| `LOW V SEEN` / `THROTL SEEN` | It happened earlier since boot |

### 8. Web app QR code

A scannable `http://<pi-ip>:<WEB_PORT>` code on the left, and the address and
Wi-Fi name on the right. It is also shown at boot for `OLED_ACCESS_SECONDS`
(20 s, `0` to skip) and for 60 s on request: **System → Show web app QR on
display** in the web app, or `telemetry oled-qr --seconds 60`.

## Alerts

A possible impact or coolant ≥ 110 °C draws an inverted banner over the top
three data rows of whichever page is showing (`IMPACT` / `HOT 112°`). The
status row and the lower rows stay visible. Alerts are never hidden behind the
QR screen.

## Burn-in

The image shifts one pixel sideways every 30 seconds.

## Testing without the car

```bash
telemetry oled-preview --out oled-preview   # enlarged PNGs of every page and failure case
sudo systemctl stop car-telemetry.service
telemetry oled-test --driver sh1106         # draws the same screens on the display
telemetry oled-test --driver ssd1306
sudo systemctl start car-telemetry.service
```
