# Vehicle Profiles

When VIN is available, it becomes the preferred profile key. A profile stores vehicle metadata, supported signals and the user's optional signal selections. If VIN cannot be read, the configured fallback vehicle ID is used.

Profiles live under `VEHICLE_PROFILE_DIR` and are not meant to be committed to Git.
