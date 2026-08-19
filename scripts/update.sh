#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
git pull --ff-only
"$PROJECT_DIR/.venv/bin/pip" install -e "$PROJECT_DIR"
sudo systemctl restart car-telemetry-obd-link.service
sudo systemctl restart car-telemetry.service
sudo systemctl restart car-telemetry-web.service
telemetry status || true
