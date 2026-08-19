#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "============================================================"
echo " Car Telemetry Headless Installer"
echo " Project: ${PROJECT_DIR}"
echo " User:    ${USER_NAME}"
echo "============================================================"

sudo apt update
sudo apt install -y \
  git \
  python3-venv \
  python3-pip \
  python3-dev \
  i2c-tools \
  bluez \
  bluetooth \
  rfkill \
  avahi-daemon \
  libjpeg-dev \
  zlib1g-dev \
  libopenjp2-7 \
  libtiff6

sudo raspi-config nonint do_ssh 0 || true
sudo raspi-config nonint do_i2c 0 || true
sudo raspi-config nonint do_serial_hw 0 || true
sudo raspi-config nonint do_serial_cons 1 || true
sudo usermod -aG dialout,i2c,bluetooth "$USER_NAME"

python3 -m venv --system-site-packages "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip setuptools wheel
"$PROJECT_DIR/.venv/bin/pip" install -e "$PROJECT_DIR"

if [[ ! -f "$PROJECT_DIR/config/telemetry.env" ]]; then
  cp "$PROJECT_DIR/config/telemetry.env.example" "$PROJECT_DIR/config/telemetry.env"
fi

sed \
  -e "s|__USER__|$USER_NAME|g" \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  "$PROJECT_DIR/systemd/car-telemetry.service.template" \
  | sudo tee /etc/systemd/system/car-telemetry.service >/dev/null

sed \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  "$PROJECT_DIR/systemd/car-telemetry-obd-link.service.template" \
  | sudo tee /etc/systemd/system/car-telemetry-obd-link.service >/dev/null

sed \
  -e "s|__USER__|$USER_NAME|g" \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  "$PROJECT_DIR/systemd/car-telemetry-web.service.template" \
  | sudo tee /etc/systemd/system/car-telemetry-web.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable bluetooth.service avahi-daemon.service
sudo systemctl enable car-telemetry-obd-link.service car-telemetry.service car-telemetry-web.service

sudo ln -sf "$PROJECT_DIR/.venv/bin/telemetry" /usr/local/bin/telemetry

# Keep the safe shutdown button on GPIO4 / physical pin 7.
BOOT=/boot/firmware/config.txt
[[ -f "$BOOT" ]] || BOOT=/boot/config.txt
OVERLAY='dtoverlay=gpio-shutdown,gpio_pin=4,active_low=1,gpio_pull=up,debounce=1000'
grep -Fqx "$OVERLAY" "$BOOT" || echo "$OVERLAY" | sudo tee -a "$BOOT" >/dev/null

cat <<MSG

Installed.

Reboot once:
  sudo reboot

After reboot:
  telemetry status
  telemetry web-url

Then open the shown URL from a phone/laptop on the same network.
MSG
