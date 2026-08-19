from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import obd

from .config import Settings
from .obd_transport import resolve
from .state import DeviceState
from .vehicle_profiles import ProfileStore

CORE_FALLBACK = (
    "RPM",
    "SPEED",
    "COOLANT_TEMP",
    "ENGINE_LOAD",
    "THROTTLE_POS",
    "CONTROL_MODULE_VOLTAGE",
    "FUEL_LEVEL",
    "INTAKE_TEMP",
    "MAF",
)
EXCLUDED_LIVE_PREFIXES = ("PIDS_", "MIDS_", "DTC_")
EXCLUDED_LIVE = {
    "STATUS",
    "FREEZE_DTC",
    "O2_SENSORS",
    "O2_SENSORS_ALT",
    "GET_DTC",
    "CLEAR_DTC",
    "GET_CURRENT_DTC",
    "VIN",
    "VIN_MESSAGE_COUNT",
    "CALIBRATION_ID",
    "CALIBRATION_ID_MESSAGE_COUNT",
    "CVN",
    "CVN_MESSAGE_COUNT",
    "ELM_VERSION",
    "ELM_VOLTAGE",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serializable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "magnitude") and hasattr(value, "units"):
        magnitude = value.magnitude
        try:
            magnitude = float(magnitude)
        except Exception:
            magnitude = str(magnitude)
        return {"value": magnitude, "unit": str(value.units)}
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return [serializable(item) for item in value]
    if isinstance(value, list):
        return [serializable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    return str(value)


def command_meta(command) -> dict[str, Any]:
    raw = (
        command.command.decode("ascii", errors="ignore")
        if isinstance(command.command, (bytes, bytearray))
        else str(command.command)
    )
    mode = None
    if len(raw) >= 2 and raw[:2].isdigit():
        try:
            mode = int(raw[:2])
        except ValueError:
            pass
    return {
        "name": command.name,
        "description": command.desc,
        "command": raw,
        "mode": mode,
    }


def is_live_candidate(command) -> bool:
    if command.name in EXCLUDED_LIVE or command.name.startswith(EXCLUDED_LIVE_PREFIXES):
        return False
    raw = (
        command.command.decode("ascii", errors="ignore")
        if isinstance(command.command, (bytes, bytearray))
        else str(command.command)
    )
    return raw.startswith("01")


def _normalize_vin(value: Any) -> str | None:
    if isinstance(value, str):
        vin = value.strip().replace(" ", "")
        return vin or None
    if isinstance(value, list):
        joined = "".join(str(item) for item in value).strip().replace(" ", "")
        return joined or None
    return None


def _normalize_dtc_list(value: Any) -> list[dict[str, str]]:
    if not value:
        return []
    if isinstance(value, tuple) and len(value) >= 1 and isinstance(value[0], str):
        value = [value]
    result: list[dict[str, str]] = []
    if not isinstance(value, (list, tuple)):
        return result
    for item in value:
        if isinstance(item, (list, tuple)) and item:
            code = str(item[0]).strip()
            description = str(item[1]).strip() if len(item) > 1 and item[1] is not None else ""
            if code:
                result.append({"code": code, "description": description})
        elif isinstance(item, str) and item.strip():
            result.append({"code": item.strip(), "description": ""})
    return result


def _normalize_freeze_dtc(value: Any) -> dict[str, str] | None:
    items = _normalize_dtc_list(value)
    return items[0] if items else None


class OBDService:
    def __init__(self, settings: Settings, state: DeviceState):
        self.s = settings
        self.state = state
        self.lock = threading.RLock()
        self.connection = None
        self.transport_override: str | None = None
        self.store = ProfileStore(settings.vehicle_profile_dir)
        self.vehicle_key = settings.vehicle_id
        self.profile = self.store.load(self.vehicle_key)
        self.user_selected = set(self.profile.get("selectedSignals", []))
        self.dtc_events: deque[dict[str, Any]] = deque(maxlen=max(10, settings.dtc_max_events))
        self.dtc_event_seq = 0
        self._last_dtc_sets: dict[str, set[str]] = {"stored": set(), "currentCycle": set()}

    def set_transport(self, kind: str) -> None:
        if kind not in {"auto", "usb", "bluetooth"}:
            raise ValueError("transport must be auto, usb, or bluetooth")
        self.transport_override = kind
        self.reconnect()

    def reconnect(self) -> None:
        with self.lock:
            if self.connection:
                try:
                    self.connection.stop()
                except Exception:
                    pass
                try:
                    self.connection.close()
                except Exception:
                    pass
            self.connection = None

    def _query(self, connection, command, *, force: bool = False):
        response = obd.OBD.query(connection, command, force=force)
        if response is None or response.is_null():
            return None
        return response.value

    def _query_static(self, connection) -> dict[str, Any]:
        metadata: dict[str, Any] = {}

        for name in ("ELM_VERSION", "ELM_VOLTAGE"):
            try:
                value = self._query(connection, obd.commands[name], force=True)
                if value is not None:
                    metadata[name] = serializable(value)
            except Exception:
                pass

        for name in ("OBD_COMPLIANCE", "FUEL_TYPE"):
            try:
                command = obd.commands[name]
                if connection.supports(command):
                    value = self._query(connection, command, force=False)
                    if value is not None:
                        metadata[name] = serializable(value)
            except Exception:
                pass

        # VIN is Mode 09. Try it once on connection even when the adapter's support
        # discovery is incomplete; failure is harmless and VIN remains optional.
        try:
            value = self._query(connection, obd.commands.VIN, force=True)
            vin = _normalize_vin(value)
            if vin:
                metadata["VIN"] = vin
        except Exception:
            vin = None

        for name in ("CALIBRATION_ID", "CVN"):
            try:
                command = obd.commands[name]
                if connection.supports(command):
                    value = self._query(connection, command, force=False)
                    if value is not None:
                        metadata[name] = serializable(value)
            except Exception:
                pass

        vin = metadata.get("VIN")
        if isinstance(vin, str) and vin.strip():
            self.vehicle_key = vin.strip()
            self.profile = self.store.load(self.vehicle_key)
            self.user_selected = set(self.profile.get("selectedSignals", []))

        metadata.update(
            {
                "vehicleKey": self.vehicle_key,
                "protocolId": connection.protocol_id(),
                "protocolName": connection.protocol_name(),
                "port": connection.port_name(),
                "discoveredAt": utc_now(),
            }
        )
        return metadata

    def _callback(self, name: str, meta: dict[str, Any]):
        def callback(response):
            try:
                value = None if response.is_null() else serializable(response.value)
                self.state.merge_nested(
                    "obd",
                    "signals",
                    {
                        name: {
                            **meta,
                            "value": value,
                            "updatedAt": time.time(),
                        }
                    },
                )
            except Exception as exc:
                self.state.merge("obd", {"lastCallbackError": str(exc)})

        return callback

    def _configure_watches(self, connection) -> None:
        supported = {
            command.name: command
            for command in connection.supported_commands
            if command is not None
        }

        live_meta = [command_meta(command) for command in supported.values() if is_live_candidate(command)]
        live_meta.sort(key=lambda item: item["name"])

        core = [
            name
            for name in (self.s.obd_core_signals or CORE_FALLBACK)
            if name in supported and is_live_candidate(supported[name])
        ]
        extras = [
            name
            for name in sorted(self.user_selected)
            if name in supported and is_live_candidate(supported[name])
        ]
        selected: list[str] = []
        for name in core + extras:
            if name not in selected:
                selected.append(name)

        with connection.paused():
            connection.unwatch_all()
            for name in selected:
                command = supported[name]
                connection.watch(command, callback=self._callback(name, command_meta(command)))

        all_supported = [command_meta(command) for command in supported.values()]
        all_supported.sort(key=lambda item: (item.get("mode") is None, item.get("mode") or 0, item["name"]))

        self.profile.update(
            {
                "vehicleKey": self.vehicle_key,
                "selectedSignals": sorted(self.user_selected),
                "supportedSignals": sorted(item["name"] for item in live_meta),
                "updatedAt": utc_now(),
            }
        )
        self.store.save(self.vehicle_key, self.profile)

        self.state.merge(
            "obd",
            {
                "supportedSignals": live_meta,
                "supportedCommandsAll": all_supported,
                "coreSignals": core,
                "selectedSignals": selected,
                "userSelectedSignals": extras,
                "supportedCount": len(live_meta),
                "supportedCommandCount": len(all_supported),
            },
        )

    def select_signal(self, name: str, selected: bool = True) -> None:
        name = name.upper().strip()
        with self.lock:
            if not self.connection:
                raise RuntimeError("OBD is not connected")
            supported = {
                item["name"]
                for item in self.state.snapshot().get("obd", {}).get("supportedSignals", [])
            }
            if name not in supported:
                raise ValueError(f"{name} is not a supported live signal")
            if name in self.s.obd_core_signals and not selected:
                raise ValueError("Core signals cannot be removed")
            if selected:
                self.user_selected.add(name)
            else:
                self.user_selected.discard(name)
            self.profile.update(
                {
                    "vehicleKey": self.vehicle_key,
                    "selectedSignals": sorted(self.user_selected),
                    "supportedSignals": sorted(supported),
                    "updatedAt": utc_now(),
                }
            )
            self.store.save(self.vehicle_key, self.profile)
            self._configure_watches(self.connection)

    def _append_dtc_event(self, event: str, scope: str, code: str = "", description: str = "", **extra) -> dict[str, Any]:
        self.dtc_event_seq += 1
        item = {
            "seq": self.dtc_event_seq,
            "timestamp": utc_now(),
            "event": event,
            "scope": scope,
            "code": code,
            "description": description,
            "vehicleKey": self.vehicle_key,
            **extra,
        }
        self.dtc_events.append(item)
        return item

    def _detect_dtc_changes(self, scope: str, current: list[dict[str, str]]) -> None:
        previous_codes = self._last_dtc_sets.get(scope, set())
        current_by_code = {item["code"]: item for item in current}
        current_codes = set(current_by_code)

        for code in sorted(current_codes - previous_codes):
            item = current_by_code[code]
            self._append_dtc_event("DTC_ADDED", scope, code, item.get("description", ""))
        for code in sorted(previous_codes - current_codes):
            self._append_dtc_event("DTC_REMOVED", scope, code)

        self._last_dtc_sets[scope] = current_codes

    def refresh_dtcs(self) -> dict[str, Any]:
        with self.lock:
            if not self.connection:
                raise RuntimeError("OBD is not connected")

            stored: list[dict[str, str]] = []
            current: list[dict[str, str]] = []
            freeze: dict[str, str] | None = None
            errors: dict[str, str] = {}

            with self.connection.paused():
                for name, target in (("GET_DTC", "stored"), ("GET_CURRENT_DTC", "current")):
                    try:
                        value = self._query(self.connection, obd.commands[name], force=True)
                        normalized = _normalize_dtc_list(value)
                        if target == "stored":
                            stored = normalized
                        else:
                            current = normalized
                    except Exception as exc:
                        errors[name] = str(exc)

                try:
                    command = obd.commands.FREEZE_DTC
                    value = self._query(
                        self.connection,
                        command,
                        force=not self.connection.supports(command),
                    )
                    freeze = _normalize_freeze_dtc(value)
                except Exception as exc:
                    errors["FREEZE_DTC"] = str(exc)

            self._detect_dtc_changes("stored", stored)
            self._detect_dtc_changes("currentCycle", current)

            result = {
                "stored": stored,
                "currentCycle": current,
                "freezeFrameCode": freeze,
                "storedCount": len(stored),
                "currentCycleCount": len(current),
                "lastScanAt": utc_now(),
                "errors": errors,
            }
            self.state.merge(
                "obd",
                {
                    "dtc": result,
                    "dtcEvents": list(self.dtc_events),
                    "lastDtcEventSeq": self.dtc_event_seq,
                },
            )
            return result

    def _rpm_value(self) -> float | None:
        rpm = (
            self.state.snapshot()
            .get("obd", {})
            .get("signals", {})
            .get("RPM", {})
            .get("value")
        )
        if isinstance(rpm, dict):
            rpm = rpm.get("value")
        try:
            return float(rpm) if rpm is not None else None
        except (TypeError, ValueError):
            return None

    def clear_dtcs(self, confirmation: str) -> Any:
        if confirmation != "CLEAR_DTC_CONFIRMED":
            raise PermissionError("confirmation token missing")

        rpm = self._rpm_value()
        if self.s.dtc_clear_require_engine_off:
            if rpm is None:
                raise RuntimeError("Cannot verify that the engine is off because RPM is unavailable")
            if rpm > 0:
                raise RuntimeError("Engine appears to be running. Stop the engine before clearing DTCs")

        with self.lock:
            if not self.connection:
                raise RuntimeError("OBD is not connected")
            try:
                with self.connection.paused():
                    value = self._query(self.connection, obd.commands.CLEAR_DTC, force=True)
                self._append_dtc_event("DTC_CLEAR_REQUESTED", "all", result="success")
                self.state.merge(
                    "obd",
                    {
                        "lastDtcClearAt": utc_now(),
                        "dtcEvents": list(self.dtc_events),
                        "lastDtcEventSeq": self.dtc_event_seq,
                    },
                )
                time.sleep(1.0)
                self.refresh_dtcs()
                return serializable(value)
            except Exception as exc:
                self._append_dtc_event("DTC_CLEAR_REQUESTED", "all", result="failed", error=str(exc))
                self.state.merge(
                    "obd",
                    {
                        "dtcEvents": list(self.dtc_events),
                        "lastDtcEventSeq": self.dtc_event_seq,
                    },
                )
                raise

    def _connect_once(self) -> None:
        transport = resolve(self.s, self.transport_override)
        self.state.merge(
            "obd",
            {
                "enabled": True,
                "connecting": True,
                "connected": False,
                "transport": transport.kind,
                "port": transport.port,
                "error": None,
            },
        )

        connection = obd.Async(
            portstr=transport.port,
            baudrate=self.s.obd_baud,
            protocol=self.s.obd_protocol,
            fast=self.s.obd_fast,
            timeout=self.s.obd_timeout,
            delay_cmds=self.s.obd_async_loop_delay,
        )
        if not connection.is_connected():
            try:
                connection.close()
            except Exception:
                pass
            raise RuntimeError(f"python-OBD did not reach CAR_CONNECTED (status={connection.status()})")

        self.connection = connection
        vehicle = self._query_static(connection)
        self.profile.update({"vehicleKey": self.vehicle_key, "vehicle": vehicle, "updatedAt": utc_now()})
        self.store.save(self.vehicle_key, self.profile)
        self._configure_watches(connection)

        self.state.merge(
            "obd",
            {
                "connecting": False,
                "connected": True,
                "status": str(connection.status()),
                "protocolId": connection.protocol_id(),
                "protocolName": connection.protocol_name(),
                "port": connection.port_name(),
                "transport": transport.kind,
                "vehicle": vehicle,
                "vehicleProfileKey": self.vehicle_key,
            },
        )
        connection.start()

    def run(self, stop_event: threading.Event) -> None:
        if not self.s.obd_enabled:
            self.state.merge("obd", {"enabled": False})
            return

        next_dtc_scan = 0.0
        while not stop_event.is_set():
            try:
                if self.connection is None:
                    self._connect_once()
                    next_dtc_scan = 0.0

                if self.connection and not self.connection.is_connected():
                    raise RuntimeError("vehicle/ELM connection lost")

                now = time.monotonic()
                if self.connection and now >= next_dtc_scan:
                    try:
                        self.refresh_dtcs()
                    except Exception as exc:
                        self.state.merge("obd", {"dtcError": str(exc)})
                    next_dtc_scan = now + max(10.0, self.s.dtc_scan_seconds)

                stop_event.wait(1.0)
            except Exception as exc:
                self.state.merge(
                    "obd",
                    {
                        "connecting": False,
                        "connected": False,
                        "error": str(exc),
                    },
                )
                self.reconnect()
                stop_event.wait(self.s.obd_reconnect_seconds)

        self.reconnect()
