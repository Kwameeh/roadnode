# Small OLED System

The optional 1.3-inch 128×64 monochrome OLED is a glanceable vehicle display. It does not show a QR code and it is not the interactive interface; configuration remains in the LAN web application.

`OLED_DRIVER` selects `sh1106` (default) or `ssd1306`. Both use the same I²C wiring and Pillow renderer through `luma.oled`. The display is cleared at startup and shutdown.

Pages rotate every `OLED_PAGE_SECONDS` (three seconds by default):

- Driving: large speed, RPM and coolant temperature
- Location: GPS fix, satellites, heading and coordinates
- Vehicle health: voltage, coolant, fuel and DTC count
- Connectivity: OBD, GPS, LavinMQ, queue depth and the Pi address

Impact and high-coolant warnings temporarily override the carousel. A one-pixel alternating offset reduces burn-in.

Test the hardware without starting the complete telemetry engine:

```bash
telemetry oled-test --driver sh1106
telemetry oled-test --driver ssd1306
```

If both commands render correctly but a QR code later returns, another process is writing to the same display and must be stopped.
