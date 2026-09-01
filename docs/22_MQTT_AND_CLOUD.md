# EMQX MQTT and Cloud Ingestion

RoadNode Edge has one MQTT implementation: MQTT 5 over TLS to the production
EMQX broker. All settings and credentials come from `config/telemetry.env`.

## Connection

```text
Host: mqtt.obd2.ragnogroup.com
Port: 8883
TLS: required
CA: /etc/roadnode/mqtt-ca.crt
Authentication: per-device username/password
Client ID: DEVICE_ID
```

Install the CA with `./scripts/install-cloud-mqtt-ca.sh`. Cloudflare must keep
the MQTT hostname DNS-only. The application does not fall back to plaintext,
v1 vehicle topics, or a separate credential JSON file.

## Topic and payload

Each device publishes only to:

```text
roadnode/v2/devices/{DEVICE_ID}/frame
```

Frames use QoS 1, are not retained, and carry MQTT 5 JSON content properties.
Each frame includes a stable `messageId`, `deviceId`, `bootId`, sequence,
capture/send time, replay flag, dropped-message counters, telemetry, and IMU
samples. EMQX authorization should deny subscriptions and restrict publishing
to that device's own namespace.

The cloud worker subscribes to `roadnode/v2/devices/+/+`, validates each frame,
and writes it to MongoDB idempotently by `messageId`.

## Offline behavior

Frames are written to the SQLite outbox before publishing. A row is deleted
only after MQTT PUBACK. After an outage, the publisher reconnects with backoff,
replays the oldest frames first, preserves their identity and capture time, and
marks them as replayed. Default retention is 256 MiB or 24 hours.

## Healthy status

`telemetry status` should show `publisher.connected=true`, an increasing
`publisher.published`, and a falling `frame.queueDepth` after reconnecting.
EMQX should show the device UUID as the client ID and the Admin-created device
username as the authenticated user.
