# Limitations and Security

Standard supported PIDs vary by vehicle. VIN/other Mode 09 support may vary. Manufacturer-specific signals require explicit custom commands. Physical ELM327 clones vary in quality.

The Prototype 1 web app is designed for a trusted LAN and currently has no user authentication. Do not expose port 8080 directly to the public internet. DTC clear is protected in the engine but remains a diagnostic operation that should be used deliberately.
