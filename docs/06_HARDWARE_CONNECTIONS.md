# Hardware Connections

## GPS
GPS TX → Pi GPIO15 / physical pin 10. Optional GPS RX → GPIO14 / pin 8. GPS is read from `/dev/serial0`.

## I2C
OLED and MPU6050 share GPIO2/SDA physical pin 3 and GPIO3/SCL physical pin 5. Typical addresses are OLED `0x3C` and MPU6050 `0x68`.

## USB ELM327
Vehicle OBD-II → USB ELM327 → USB OTG adapter → Pi Zero 2 W USB/data port. The Pi is powered separately through PWR IN during Prototype 1.

## Bluetooth ELM327
No GPIO wiring. The Pi's onboard Bluetooth connects to the adapter/emulator and the root RFCOMM service creates `/dev/rfcomm0`.

## Shutdown button
GPIO4 / physical pin 7 → normally-open pushbutton → GND.
