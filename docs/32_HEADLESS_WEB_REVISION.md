# Headless Web Revision

This revision removes the 3.5-inch local touchscreen and graphical desktop from Prototype 1.

Major changes:

- separate LAN web service (`car-telemetry-web.service`)
- responsive browser dashboard for phone/laptop
- 5 Hz in-memory WebSocket live state updates with reconnect and HTTP fallback
- browser-driven Bluetooth scan, pair, trust/use-as-ELM workflow through Pi BlueZ
- Auto/USB/Bluetooth OBD transport controls
- VIN collected at vehicle connection when available
- automatic periodic stored/current/freeze-frame DTC collection
- DTC add/remove/clear event history
- versioned LavinMQ telemetry, vehicle metadata, DTC event and retained status topics
- system CPU/RAM/temperature/disk monitoring in the web app
- performance benchmark changed from touchscreen rendering to multiple simulated web clients
- Raspberry Pi OS Lite/headless deployment recommended
