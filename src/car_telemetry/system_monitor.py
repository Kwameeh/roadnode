from __future__ import annotations

import os
import shutil
import socket
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .observations import (
    DEVICE_MAX_AGE_MS,
    ObservationWriter,
    observation_meta,
    utc_now,
)
from .state import DeviceState


def _meminfo() -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        for raw in Path('/proc/meminfo').read_text().splitlines():
            key, rest = raw.split(':', 1)
            result[key] = float(rest.strip().split()[0]) / 1024.0
    except Exception:
        pass
    return result


def _cpu_snapshot() -> tuple[int, int] | None:
    try:
        fields = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle
    except Exception:
        return None


def _cpu_percent(before, after) -> float | None:
    if not before or not after:
        return None
    total = after[0] - before[0]
    idle = after[1] - before[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 1)


def _temperature() -> float | None:
    paths = (
        Path('/sys/class/thermal/thermal_zone0/temp'),
        Path('/sys/devices/virtual/thermal/thermal_zone0/temp'),
    )
    for path in paths:
        try:
            return round(float(path.read_text().strip()) / 1000.0, 1)
        except Exception:
            pass
    return None


def _ip_address() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(('8.8.8.8', 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return None


def _software_version() -> str:
    try:
        return version("car-telemetry")
    except PackageNotFoundError:
        return "development"


def worker(
    state: DeviceState,
    observations: ObservationWriter,
    stop: threading.Event,
) -> None:
    previous = _cpu_snapshot()
    while not stop.is_set():
        current = _cpu_snapshot()
        mem = _meminfo()
        disk = shutil.disk_usage('/')
        observed_at = utc_now()
        payload = {
            'hostname': socket.gethostname(),
            'ipAddress': _ip_address(),
            'cpuPercent': _cpu_percent(previous, current),
            'cpuCount': os.cpu_count(),
            'temperatureC': _temperature(),
            'memoryTotalMb': round(mem.get('MemTotal', 0.0), 1) if mem else None,
            'memoryAvailableMb': round(mem.get('MemAvailable', 0.0), 1) if mem else None,
            'memoryUsedMb': (
                round(mem.get('MemTotal', 0.0) - mem.get('MemAvailable', 0.0), 1)
                if mem
                else None
            ),
            'diskTotalGb': round(disk.total / (1024**3), 2),
            'diskFreeGb': round(disk.free / (1024**3), 2),
            'loadAverage': list(os.getloadavg()) if hasattr(os, 'getloadavg') else None,
            'observedAt': observed_at,
            'source': 'device.os',
            'quality': 'valid',
            'maxAgeMs': DEVICE_MAX_AGE_MS,
        }
        try:
            payload['uptimeSeconds'] = float(Path('/proc/uptime').read_text().split()[0])
        except Exception:
            pass
        state.merge('system', payload)
        mqtt_state = state.snapshot().get('mqtt', {})
        device_observation = {
            'temperatureC': payload.get('temperatureC'),
            'network': 'connected' if payload.get('ipAddress') else 'offline',
            'queueDepth': max(0, int(mqtt_state.get('bufferedMessages', 0) or 0)),
            'softwareVersion': _software_version(),
            **observation_meta(
                observed_at=observed_at,
                source='device.os',
                quality='valid',
                max_age_ms=DEVICE_MAX_AGE_MS,
            ),
        }
        observations.update_device(
            {key: value for key, value in device_observation.items() if value is not None}
        )
        previous = current
        stop.wait(2.0)
