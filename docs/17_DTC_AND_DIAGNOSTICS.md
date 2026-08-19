# DTC and Diagnostics

The engine automatically scans DTCs at startup/connection and periodically (`DTC_SCAN_SECONDS`, default 60s).

Collected:

- stored DTCs (`GET_DTC`)
- current-cycle DTCs (`GET_CURRENT_DTC`)
- freeze-frame DTC (`FREEZE_DTC`) when available
- add/remove/clear audit events

The web Diagnostics page can trigger a scan. Clearing requires the exact confirmation token from the UI and, by default, the engine must be able to confirm RPM is zero. Clear events are recorded and can be published to the DTC MQTT topic.
