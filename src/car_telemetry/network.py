"""Local NetworkManager controls, independent of the telemetry engine."""
from __future__ import annotations

import http.client
import os
import subprocess
import threading
import time
from typing import Any


class NetworkError(RuntimeError):
    pass


def fields(line: str) -> list[str]:
    """Split nmcli terse output (colons and backslashes are escaped)."""
    result, value, escaped = [], '', False
    for char in line:
        if escaped:
            value += char
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            result.append(value)
            value = ''
        else:
            value += char
    result.append(value)
    return result


def nmcli(*args: str, password: str | None = None, timeout: int = 15) -> str:
    command = ['nmcli', '--terse', '--escape', 'yes', '--wait', str(timeout)]
    if password is not None:
        command.append('--ask')
    try:
        result = subprocess.run(
            command + list(args), input=(password + '\n') if password is not None else '',
            capture_output=True, text=True, encoding='utf-8', timeout=timeout + 5,
            env={**os.environ, 'LC_ALL': 'C.UTF-8'},
        )
    except FileNotFoundError:
        raise NetworkError('Wi-Fi management requires NetworkManager (nmcli) on the Pi.') from None
    except subprocess.TimeoutExpired:
        raise NetworkError('Network operation timed out. Check connection status before retrying.') from None
    except OSError:
        raise NetworkError('Could not run NetworkManager on the Pi.') from None
    if result.returncode:
        # Never return subprocess output: prompts/errors can contain credentials.
        if result.returncode == 4:
            message = 'Could not connect. Check the password, signal strength, and network availability.'
        else:
            message = 'NetworkManager could not complete the request. Check the Wi-Fi radio and service permissions on the Pi.'
        raise NetworkError(message)
    return result.stdout.strip('\r\n')


def internet_status() -> str:
    """Verify outbound HTTPS; a LAN address or MQTT connection is not proof."""
    connection = http.client.HTTPSConnection('connectivitycheck.gstatic.com', timeout=4)
    try:
        connection.request('GET', '/generate_204')
        return 'online' if connection.getresponse().status == 204 else 'limited'
    except (OSError, http.client.HTTPException):
        return 'offline'
    finally:
        connection.close()


class NetworkManager:
    def __init__(self):
        self._operation_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cached: dict[str, Any] = {}
        self._checked = 0.0
        self._operation: dict[str, Any] = {'state': 'idle'}

    def operation(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._operation)

    def _set_operation(self, **value):
        with self._state_lock:
            self._operation = value

    def _access_points(self, rescan: bool = False) -> list[dict[str, Any]]:
        output = nmcli('--fields', 'IN-USE,SSID,SIGNAL,SECURITY,DEVICE',
                       'device', 'wifi', 'list', '--rescan', 'yes' if rescan else 'no', timeout=20)
        networks: dict[tuple[str, str, str], dict[str, Any]] = {}
        for line in output.splitlines():
            row = fields(line)
            if len(row) != 5 or not row[1]:
                continue
            active, ssid, signal, security, interface = row
            item = {'ssid': ssid, 'signal': int(signal) if signal.isdigit() else 0,
                    'security': security if security != '--' else '',
                    'interface': interface, 'connected': active == '*',
                    'supported': security in ('', '--') or (
                        'WPA' in security and '802.1X' not in security and 'EAP' not in security)}
            key = (interface, ssid, security)
            old = networks.get(key)
            if old is None or (item['connected'], item['signal']) > (old['connected'], old['signal']):
                networks[key] = item
        return sorted(networks.values(), key=lambda item: (not item['connected'], -item['signal'], item['ssid']))

    def scan(self) -> dict[str, Any]:
        if not self._operation_lock.acquire(blocking=False):
            raise NetworkError('A Wi-Fi operation is already in progress.')
        try:
            if nmcli('radio', 'wifi') != 'enabled':
                nmcli('radio', 'wifi', 'on')
            return {'networks': self._access_points(rescan=True)}
        finally:
            self._operation_lock.release()

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            if time.monotonic() - self._checked >= 10 or not self._cached:
                data: dict[str, Any] = {'available': False, 'wifiEnabled': False, 'interfaces': [],
                                        'wifi': [], 'internet': 'unknown', 'error': None}
                try:
                    devices = nmcli('--fields', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status')
                    data['available'] = True
                    data['wifiEnabled'] = nmcli('radio', 'wifi') == 'enabled'
                    for line in devices.splitlines():
                        row = fields(line)
                        if len(row) != 4 or row[1] not in ('wifi', 'ethernet', 'gsm'):
                            continue
                        interface, kind, state, name = row
                        addresses = nmcli('--get-values', 'IP4.ADDRESS,IP6.ADDRESS', 'device', 'show', interface)
                        data['interfaces'].append({'interface': interface, 'type': kind, 'state': state,
                                                   'connection': name,
                                                   'addresses': [':'.join(fields(line)) for line in addresses.splitlines()]})
                    data['wifi'] = [item for item in self._access_points() if item['connected']]
                except NetworkError as exc:
                    data['error'] = str(exc)
                data['internet'] = internet_status()
                data['checkedAt'] = time.time()
                self._cached, self._checked = data, time.monotonic()
            return {**self._cached, 'operation': self.operation()}

    def reserve_connect(self, ssid: str, interface: str) -> dict[str, Any]:
        if not self._operation_lock.acquire(blocking=False):
            raise NetworkError('A Wi-Fi operation is already in progress.')
        self._set_operation(state='connecting', ssid=ssid, interface=interface, error=None)
        return self.operation()

    def connect(self, ssid: str, interface: str, password: str):
        """Run after the HTTP response, so losing the browser cannot cancel it."""
        try:
            nmcli('radio', 'wifi', 'on')
            nmcli('device', 'wifi', 'connect', ssid, 'ifname', interface,
                  password=password, timeout=45)
            self._set_operation(state='connected', ssid=ssid, interface=interface, error=None)
        except NetworkError as exc:
            self._set_operation(state='failed', ssid=ssid, interface=interface, error=str(exc))
        finally:
            with self._status_lock:
                self._checked = 0.0
            self._operation_lock.release()
