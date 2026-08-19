# MQTT and Cloud

Normal telemetry is published to `MQTT_TOPIC` and contains current GPS, IMU, watched OBD values, current DTC state, driver events and device health without repeating huge command catalogs.

Vehicle/VIN metadata is published to `MQTT_METADATA_TOPIC` when it changes. DTC add/remove/clear events are published separately to `MQTT_DTC_TOPIC`.
