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

discover_channel() {
  local mac="$1"
  sdptool browse "$mac" 2>/dev/null |
    sed -n '/ELM327/,/Channel:/p;/"Serial Port"/,/Channel:/p' |
    sed -n 's/.*Channel:[[:space:]]*\([0-9][0-9]*\).*/\1/p' |
    head -n 1
}

try_candidate() {
  local token="$1"
  local fallback_channel="$2"
  local mac="${token%@*}"
  local channel=""
  if [[ "$token" == *"@"* ]]; then
    channel="${token##*@}"
  else
    channel="$(discover_channel "$mac")"
  fi
  if [[ -z "$channel" ]]; then
    channel="$fallback_channel"
  fi

  bluetoothctl trust "$mac" >/dev/null 2>&1 || true
  bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  cleanup
  rfcomm bind rfcomm0 "$mac" "$channel" >/dev/null 2>&1 || return 1
  [[ -e /dev/rfcomm0 ]]
}

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
  CANDIDATES="${OBD_BLUETOOTH_CANDIDATES:-}"
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

  if [[ -z "$CANDIDATES" && -n "$MAC" ]]; then
    CANDIDATES="${MAC}@${CHANNEL}"
  fi

  if [[ -n "$CANDIDATES" && ! -e /dev/rfcomm0 ]]; then
    IFS=',' read -r -a TOKENS <<< "$CANDIDATES"
    for token in "${TOKENS[@]}"; do
      token="$(echo "$token" | xargs)"
      [[ -z "$token" ]] && continue
      if try_candidate "$token" "$CHANNEL"; then
        break
      fi
    fi
  fi

  sleep 5
done
