# Web Boot and Access

`car-telemetry.service`, `car-telemetry-obd-link.service` and `car-telemetry-web.service` are enabled at boot.

Use:

```bash
telemetry web-url
```

Typical address:

```text
http://<hostname>.local:8080
```

`avahi-daemon` is installed so `.local` access is available on compatible LAN clients. The web app is intended for a trusted local network in Prototype 1.
