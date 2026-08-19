#!/usr/bin/env bash
set -u

ENV_FILE="${1:-}"
if [[ -z "$ENV_FILE" ]]; then
  echo "Usage: obd-link.sh /path/to/telemetry.env" >&2
  exit 2
fi

cleanup() {
  rfcomm release rfcomm0 >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

while true; do
  if [[ ! -f "$ENV_FILE" ]]; then
    sleep 5
    continue
  fi

  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  rfkill unblock bluetooth >/dev/null 2>&1 || true
  systemctl start bluetooth.service >/dev/null 2>&1 || true
  bluetoothctl power on >/dev/null 2>&1 || true

  MODE="${OBD_TRANSPORT:-auto}"
  MAC="${OBD_MAC:-}"
  CHANNEL="${OBD_RFCOMM_CHANNEL:-1}"
  ENABLED="${OBD_ENABLED:-true}"

  if [[ "${ENABLED,,}" != "true" ]]; then
    cleanup
    sleep 5
    continue
  fi

  # In USB-only mode RFCOMM is unnecessary. In auto mode, USB wins while present.
  if [[ "$MODE" == "usb" ]] || { [[ "$MODE" == "auto" ]] && { compgen -G '/dev/ttyUSB*' >/dev/null || compgen -G '/dev/ttyACM*' >/dev/null || compgen -G '/dev/serial/by-id/*' >/dev/null; }; }; then
    cleanup
    sleep 5
    continue
  fi

  if [[ -n "$MAC" ]]; then
    bluetoothctl trust "$MAC" >/dev/null 2>&1 || true
    bluetoothctl connect "$MAC" >/dev/null 2>&1 || true
    if [[ ! -e /dev/rfcomm0 ]]; then
      rfcomm bind rfcomm0 "$MAC" "$CHANNEL" >/dev/null 2>&1 || true
    fi
  fi

  sleep 5
done
