# LAN Web App

The web UI is a lightweight responsive HTML/CSS/JavaScript application served by FastAPI/Uvicorn. No React, Chromium kiosk or local graphical desktop is required.

Pages:

- Dashboard — speed, RPM, temperatures, fuel, VIN and connection health
- Signals — discovered standard live PIDs and selection controls
- Diagnostics — stored/current/freeze DTCs, history and protected clear
- Setup — Auto/USB/Bluetooth transport and Bluetooth management
- System — CPU, RAM, temperature, disk, IP and uptime

Live state is read from the engine's in-memory localhost API once at up to 5 Hz and fanned out to every browser through a WebSocket. Every frame carries the engine source and last-success time, so a healthy WebSocket cannot hide a stalled engine. The browser shows `Live`, `Reconnecting`, `Engine stale` or `Stale`, reconnects with bounded backoff, and falls back to `/api/state` polling without reloading the page. The disk status file is only a startup/diagnostic fallback.
