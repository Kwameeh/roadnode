#!/usr/bin/env bash
set -euo pipefail

API_URL="${ROADNODE_API_URL:-https://api.obd2.ragnogroup.com}"
TARGET="/etc/roadnode/mqtt-ca.crt"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "${API_URL%/}/v1/device/mqtt-ca" -o "$tmp"
grep -q -- '-----BEGIN CERTIFICATE-----' "$tmp"

sudo install -d -m 0755 /etc/roadnode
sudo install -o root -g root -m 0644 "$tmp" "$TARGET"
echo "Installed MQTT CA at $TARGET"
echo "Next: set MQTT credentials in ~/roadnode/config/telemetry.env and restart car-telemetry.service."
