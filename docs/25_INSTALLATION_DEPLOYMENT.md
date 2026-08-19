# Installation and Deployment

Raspberry Pi OS Lite/headless is recommended.

```bash
chmod +x scripts/*.sh
./scripts/install.sh
sudo reboot
```

The installer enables SSH, UART and I2C, installs Bluetooth/Avahi/Python dependencies, creates the virtual environment, and enables the engine, RFCOMM and web services.

After reboot:

```bash
telemetry web-url
telemetry status
```
