from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bluetooth
from .config import Settings, set_env_values, settings as load_settings
from .obd_transport import resolve, usb_candidates


class APIServer:
    def __init__(self, settings: Settings, state, obd_service, stop_event):
        self.settings = settings
        self.host = settings.api_host
        self.port = settings.api_port
        self.state = state
        self.obd = obd_service
        self.stop_event = stop_event
        self.server = None

    def handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, status: int, payload):
                raw = json.dumps(payload, default=str).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def body(self):
                try:
                    length = int(self.headers.get('Content-Length', '0') or 0)
                    return json.loads(self.rfile.read(length) or b'{}')
                except Exception:
                    return {}

            def do_GET(self):
                snapshot = outer.state.snapshot()
                if self.path == '/state':
                    return self.send_json(200, snapshot)
                if self.path == '/signals':
                    obd = snapshot.get('obd', {})
                    # The policy document is authoritative; the flat lists stay
                    # for older readers of this local API.
                    return self.send_json(
                        200,
                        {
                            'supported': obd.get('supportedSignals', []),
                            'core': obd.get('coreSignals', []),
                            'selected': obd.get('selectedSignals', []),
                            'userSelected': obd.get('userSelectedSignals', []),
                            'policy': outer.obd.signals.ui_document(),
                        },
                    )
                if self.path == '/vehicle':
                    obd = snapshot.get('obd', {})
                    return self.send_json(
                        200,
                        {
                            'vehicle': obd.get('vehicle', {}),
                            'vehicleProfileKey': obd.get('vehicleProfileKey'),
                            'protocolName': obd.get('protocolName'),
                            'transport': obd.get('transport'),
                        },
                    )
                if self.path == '/dtc':
                    obd = snapshot.get('obd', {})
                    return self.send_json(
                        200,
                        {
                            'dtc': obd.get('dtc', {}),
                            'events': obd.get('dtcEvents', []),
                        },
                    )
                if self.path == '/obd/ports':
                    fresh = load_settings()
                    try:
                        selected = resolve(fresh, outer.obd.transport_override)
                        selected_payload = {'kind': selected.kind, 'port': selected.port}
                    except Exception as exc:
                        selected_payload = {'kind': None, 'port': None, 'error': str(exc)}
                    return self.send_json(
                        200,
                        {
                            'mode': outer.obd.transport_override or fresh.obd_transport,
                            'usb': usb_candidates(),
                            'bluetoothPort': fresh.obd_bluetooth_port,
                            'selected': selected_payload,
                        },
                    )
                if self.path == '/bluetooth/status':
                    fresh = load_settings()
                    return self.send_json(
                        200,
                        {
                            'controller': bluetooth.controller_status(),
                            'devices': bluetooth.devices(),
                            'configuredElmMac': fresh.obd_mac,
                            'configuredRfcommChannel': fresh.obd_rfcomm_channel,
                        },
                    )
                return self.send_json(404, {'error': 'not found'})

            def do_POST(self):
                body = self.body()
                try:
                    if self.path == '/signals/select':
                        policy = outer.obd.select_signal(
                            str(body.get('name', '')), bool(body.get('selected', True))
                        )
                        return self.send_json(200, {'ok': True, 'policy': policy})

                    if self.path == '/obd/reconnect':
                        outer.obd.reconnect()
                        return self.send_json(200, {'ok': True})

                    if self.path == '/obd/transport':
                        transport = str(body.get('transport', 'auto')).lower()
                        if transport not in {'auto', 'usb', 'bluetooth'}:
                            raise ValueError('transport must be auto, usb, or bluetooth')
                        values = {'OBD_TRANSPORT': transport}
                        port = str(body.get('usbPort', '')).strip()
                        if port:
                            values['OBD_USB_PORT'] = port
                        set_env_values(values)
                        outer.obd.set_transport(transport)
                        return self.send_json(200, {'ok': True, 'transport': transport})

                    if self.path == '/dtc/refresh':
                        return self.send_json(200, {'dtc': outer.obd.refresh_dtcs()})

                    if self.path == '/dtc/clear':
                        return self.send_json(
                            200,
                            {'result': outer.obd.clear_dtcs(str(body.get('confirm', '')))},
                        )

                    if self.path == '/bluetooth/scan':
                        seconds = int(body.get('seconds', outer.settings.bluetooth_scan_seconds))
                        return self.send_json(200, {'devices': bluetooth.scan(seconds)})

                    if self.path == '/bluetooth/pair':
                        mac = str(body.get('mac', ''))
                        pin = str(body.get('pin', '')).strip() or None
                        return self.send_json(200, {'device': bluetooth.pair(mac, pin)})

                    if self.path == '/bluetooth/disconnect':
                        return self.send_json(200, {'device': bluetooth.disconnect(str(body.get('mac', '')))})

                    if self.path == '/bluetooth/forget':
                        return self.send_json(200, bluetooth.forget(str(body.get('mac', ''))))

                    if self.path == '/bluetooth/use-elm':
                        mac = bluetooth.validate_mac(str(body.get('mac', '')))
                        channel = body.get('channel')
                        if channel in (None, '', 0, '0'):
                            channel = bluetooth.discover_channel(mac)
                        if channel is None:
                            raise RuntimeError('No ELM327/Serial Port RFCOMM channel was found on this Bluetooth device')
                        channel = int(channel)
                        set_env_values(
                            {
                                'OBD_ENABLED': 'true',
                                'OBD_TRANSPORT': 'bluetooth',
                                'OBD_MAC': mac,
                                'OBD_RFCOMM_CHANNEL': str(channel),
                            }
                        )
                        outer.obd.set_transport('bluetooth')
                        return self.send_json(
                            200,
                            {
                                'ok': True,
                                'mac': mac,
                                'channel': channel,
                                'message': 'Saved. The root RFCOMM link service will create /dev/rfcomm0 automatically.',
                            },
                        )
                except Exception as exc:
                    return self.send_json(409, {'error': str(exc)})

                return self.send_json(404, {'error': 'not found'})

        return Handler

    def run(self):
        self.server = ThreadingHTTPServer((self.host, self.port), self.handler())
        self.server.timeout = 0.5
        while not self.stop_event.is_set():
            self.server.handle_request()
        self.server.server_close()
