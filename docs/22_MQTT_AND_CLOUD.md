# LavinMQ MQTT and Cloud Ingestion

The Pi publishes MQTT 3.1.1 messages to a CloudAMQP-hosted LavinMQ instance. Copy the exact hostname, MQTT port, username, password and TLS settings from CloudAMQP's **MQTT Details** panel. Credentials belong only in the ignored `config/telemetry.env` file.

## Client identities

Each simultaneous client needs a distinct ID. Recommended values:

- Pi publisher: `roadnode-pi-PROTO-001`
- Cloud/MongoDB ingestor: `roadnode-ingestor-prod-01`

The ingestor should use a stable client ID, QoS 1 subscriptions and `clean_session=false`. LavinMQ can then retain its session while the server is temporarily offline. Do not connect browsers directly to MQTT; doing so exposes credentials and consumes another delivery for every message.

## Topics

- `roadnode/v1/vehicles/{vehicleId}/telemetry` — QoS 1, not retained
- `roadnode/v1/vehicles/{vehicleId}/metadata` — QoS 1, retained
- `roadnode/v1/vehicles/{vehicleId}/dtc` — QoS 1, not retained
- `roadnode/v1/vehicles/{vehicleId}/status` — QoS 1, retained, with an offline last will

Every payload includes `schemaVersion`, `messageId`, `messageType`, `deviceId`, `vehicleId`, `sessionId`, `sequence`, `capturedAt` and `sentAt`. The cloud ingestor should create a unique MongoDB index on `messageId`, add its own `receivedAt`, and use `capturedAt` when ordering replayed data.

Telemetry contains current GPS, IMU, watched OBD values, DTC state, driver events and device health. Metadata is published only when it changes. DTC events use their original capture time. Status reports clean and unexpected disconnects.

## Offline behavior

The Pi keeps at most 60 seconds of messages in RAM. It drains the oldest messages first after reconnecting, removes each QoS 1 item only after the broker acknowledges it, and records any dropped-message count in local state. The queue is never written to disk and disappears on reboot.

## Free-plan quota

CloudAMQP's free Loyal Lemming plan currently allows two million counted messages per month. Both publication and subscriber delivery count. At one telemetry message every three seconds and one subscriber, a 30-day month uses about 1.728 million counted messages before low-volume status, metadata, DTC and retry traffic. Use one MQTT ingestor and fan remote dashboards out from that server.
