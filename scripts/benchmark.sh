#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ -x .venv/bin/telemetry ]]; then
  exec .venv/bin/telemetry benchmark "$@"
fi
exec python3 -m car_telemetry.benchmark "$@"
