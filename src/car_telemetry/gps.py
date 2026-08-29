from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pynmea2
import serial

from .config import Settings
from .observations import GPS_MAX_AGE_MS, ObservationWriter, observation_meta, utc_iso
from .state import DeviceState


def _nmea_time(message, received_at: datetime) -> datetime:
    timestamp = getattr(message, "timestamp", None)
    datestamp = getattr(message, "datestamp", None)
    if timestamp is None:
        return received_at
    date = datestamp or received_at.date()
    candidate = datetime.combine(date, timestamp, tzinfo=timezone.utc)
    if datestamp is None:
        if candidate - received_at > timedelta(hours=12):
            candidate -= timedelta(days=1)
        elif received_at - candidate > timedelta(hours=12):
            candidate += timedelta(days=1)
    return candidate


def parse_observation(
    line: str,
    *,
    received_at: datetime | None = None,
) -> tuple[dict, dict | None]:
    received = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        message = pynmea2.parse(line)
    except Exception:
        return {}, None

    legacy: dict = {}
    normalized: dict | None = None
    if isinstance(message, pynmea2.types.talker.RMC):
        valid = getattr(message, "status", "") == "A"
        legacy["validFix"] = valid
        normalized = {"fix": valid}
        if valid:
            legacy["latitude"] = float(message.latitude)
            legacy["longitude"] = float(message.longitude)
            normalized.update(
                latitude=float(message.latitude),
                longitude=float(message.longitude),
            )
            if getattr(message, "spd_over_grnd", None) not in (None, ""):
                speed = round(float(message.spd_over_grnd) * 1.852, 2)
                legacy["speedKph"] = speed
                normalized["speedKph"] = speed
            if getattr(message, "true_course", None) not in (None, ""):
                heading = round(float(message.true_course), 2)
                legacy["headingDegrees"] = heading
                normalized["headingDeg"] = heading
    elif isinstance(message, pynmea2.types.talker.GGA):
        quality = int(getattr(message, "gps_qual", 0) or 0)
        valid = quality > 0
        legacy["validFix"] = valid
        normalized = {"fix": valid}
        if valid:
            legacy["latitude"] = float(message.latitude)
            legacy["longitude"] = float(message.longitude)
            normalized.update(
                latitude=float(message.latitude),
                longitude=float(message.longitude),
            )
        if getattr(message, "num_sats", None) not in (None, ""):
            satellites = int(message.num_sats)
            legacy["satellites"] = satellites
            normalized["satellites"] = satellites
        if getattr(message, "altitude", None) not in (None, ""):
            altitude = round(float(message.altitude), 2)
            legacy["altitudeMeters"] = altitude
            normalized["altitudeM"] = altitude
        if getattr(message, "horizontal_dil", None) not in (None, ""):
            hdop = round(float(message.horizontal_dil), 2)
            legacy["hdop"] = hdop
            normalized["hdop"] = hdop

    if normalized is not None:
        observed_at = utc_iso(_nmea_time(message, received))
        normalized.update(
            observation_meta(
                observed_at=observed_at,
                source="gps.nmea",
                quality="valid" if normalized["fix"] else "invalid",
                max_age_ms=GPS_MAX_AGE_MS,
            )
        )
        legacy["observedAt"] = observed_at
    return legacy, normalized


def parse(line: str) -> dict:
    """Backward-compatible local-state parser."""

    legacy, _ = parse_observation(line)
    return legacy


def worker(
    settings: Settings,
    state: DeviceState,
    observations: ObservationWriter,
    stop: threading.Event,
) -> None:
    state.merge(
        "gps",
        {"enabled": settings.gps_enabled, "port": settings.gps_port, "baud": settings.gps_baud},
    )
    if not settings.gps_enabled:
        return
    while not stop.is_set():
        try:
            with serial.Serial(settings.gps_port, settings.gps_baud, timeout=1) as port:
                state.merge("gps", {"serialOpen": True, "error": None})
                while not stop.is_set():
                    raw = port.readline()
                    if not raw:
                        continue
                    line = raw.decode("ascii", errors="ignore").strip()
                    if not line.startswith("$"):
                        continue
                    received_at = datetime.now(timezone.utc)
                    legacy, normalized = parse_observation(line, received_at=received_at)
                    if normalized is None:
                        continue
                    state.merge(
                        "gps",
                        {"received": True, "lastDataUnix": time.time(), **legacy},
                    )
                    observations.update_gps(normalized)
        except Exception as exc:
            state.merge("gps", {"serialOpen": False, "error": str(exc)})
            stop.wait(3)
