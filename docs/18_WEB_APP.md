# LAN Web App

The web UI is a lightweight responsive HTML/CSS/JavaScript application served by FastAPI/Uvicorn. No React, Chromium kiosk or local graphical desktop is required.

Pages:

- Dashboard — speed, RPM, temperatures, fuel, VIN and connection health
- Signals — discovered standard live PIDs and selection controls
- Diagnostics — stored/current/freeze DTCs, history and protected clear
- Setup — Auto/USB/Bluetooth transport and Bluetooth management
- System — CPU, RAM, temperature, disk, IP and uptime

Live state is pushed to the browser with a WebSocket.
