# Small OLED System

The optional 1.3-inch 128×64 monochrome OLED (`OLED_DRIVER=sh1106` or `ssd1306`)
is a glanceable status and debugging display. Configuration still happens in the
LAN web app.

Everything is drawn with a built-in 5×7 pixel font and 7×7 icons
(`oled_font.py`), so text stays sharp on a 1-bit display and every page fits
seven full rows of data.

## Rotating pages

Five pages rotate every `OLED_PAGE_SECONDS` (**20 s** by default; 20–30 s
leaves time to read each one). Every page has the same status row on top:

```text
🚗 📍9 ☁ WiFi BT IMU            ■····
```

- icons for OBD, GPS (with satellite count), cloud, Wi-Fi, Bluetooth and IMU:
  steady = working, blinking = problem, `-` = disabled or not used;
- the dots on the right show which of the five pages is up.

### 1. Overview

```text
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

### 2. OBD-II

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

### 3. GPS

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

### 4. IMU

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

### 5. System and cloud

```text
☁CLOUD ✓                  ⇪0
OBD2.RAGNOGROUP.COM
8883 TLS           SENT 1200
WiFi ROADNODE-WIFI  ROADNODE
192.168.1.42:8080
CPU 18% 47°C       UP 2H14M
RAM 204/416M     SD 4.8/15G
```

EMQX broker, port and TLS, frames sent and queued (or the connection error),
Wi-Fi network and hostname, web address, CPU load and temperature, uptime,
memory used/total and SD card used/total.

## Alerts

A possible impact or coolant ≥ 110 °C draws an inverted banner over the top
three data rows of whichever page is showing (`IMPACT` / `HOT 112°`). The
status row and the lower rows stay visible. Alerts are never hidden behind the
QR screen.

## Web app QR code

The QR screen shows a scannable `http://<pi-ip>:<WEB_PORT>` code on the left
and the address and Wi-Fi name on the right. It is not part of the rotation. It
appears:

- at boot, for `OLED_ACCESS_SECONDS` (20 s by default, `0` to skip);
- on request for 60 s: **System → Show web app QR on display** in the web app,
  or `telemetry oled-qr --seconds 60`.

Without a network address there is no link, so the pages keep rotating.

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
