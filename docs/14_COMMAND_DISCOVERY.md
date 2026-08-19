# Command Discovery

After a vehicle connection, the engine uses python-OBD's `supported_commands` as the standard capability list understood by both the library and vehicle. It stores the complete command metadata plus a filtered list of selectable live Mode 01 signals.

Manufacturer-specific proprietary signals are not automatically discovered unless custom command packs are added later.
