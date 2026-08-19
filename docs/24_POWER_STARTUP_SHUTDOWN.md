# Power, Startup and Shutdown

Prototype 1 keeps the Pi powered from a power bank. A USB ELM327 connects through the USB/data OTG port; do not connect raw vehicle 12V to the Pi.

The optional safe-shutdown pushbutton uses GPIO4 / physical pin 7. Always allow Linux to shut down cleanly before removing prototype power.
