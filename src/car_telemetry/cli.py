from __future__ import annotations

import argparse
import json
import socket
import subprocess

from .bluetooth import bind, discover_channel
from .common import read_json
from .config import settings
from .engine_client import EngineAPI
from .obd_transport import resolve, usb_candidates


def all_python_obd_commands():
    import obd

    seen = set()
    rows = []
    for mode_number, mode in enumerate(obd.commands.modes):
        for command in mode:
            if command is None or command.name in seen:
                continue
            seen.add(command.name)
            raw = (
                command.command.decode('ascii', errors='ignore')
                if isinstance(command.command, (bytes, bytearray))
                else str(command.command)
            )
            rows.append(
                {
                    'mode': mode_number,
                    'name': command.name,
                    'command': raw,
                    'description': command.desc,
                }
            )
    for name in ('ELM_VERSION', 'ELM_VOLTAGE'):
        command = obd.commands[name]
        raw = (
            command.command.decode('ascii', errors='ignore')
            if isinstance(command.command, (bytes, bytearray))
            else str(command.command)
        )
        rows.append(
            {
                'mode': 'adapter',
                'name': command.name,
                'command': raw,
                'description': command.desc,
            }
        )
    return rows


def main():
    parser = argparse.ArgumentParser(prog='telemetry')
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('status')
    sub.add_parser('web-url')
    sub.add_parser('obd-ports')
    sub.add_parser('obd-catalog')
    sub.add_parser('vin')

    discover = sub.add_parser('obd-discover-bt')
    discover.add_argument('--mac', required=True)

    bind_parser = sub.add_parser('obd-bind-bt')
    bind_parser.add_argument('--mac', required=True)
    bind_parser.add_argument('--channel', type=int, required=True)

    sub.add_parser('obd-reconnect')

    transport = sub.add_parser('obd-transport')
    transport.add_argument('transport', choices=['auto', 'usb', 'bluetooth'])

    sub.add_parser('dtc-refresh')
    clear = sub.add_parser('dtc-clear')
    clear.add_argument('--confirm', action='store_true')

    bt_scan = sub.add_parser('bluetooth-scan')
    bt_scan.add_argument('--seconds', type=int, default=10)

    bt_pair = sub.add_parser('bluetooth-pair')
    bt_pair.add_argument('--mac', required=True)
    bt_pair.add_argument('--pin', default='')

    bt_use = sub.add_parser('bluetooth-use-elm')
    bt_use.add_argument('--mac', required=True)
    bt_use.add_argument('--channel', type=int)

    oled_test = sub.add_parser('oled-test')
    oled_test.add_argument('--driver', choices=['sh1106', 'ssd1306'])
    oled_test.add_argument('--seconds', type=float, default=3.0)

    bench = sub.add_parser('benchmark')
    bench.add_argument('--seconds', type=int, default=120)
    bench.add_argument('--stress', type=float, default=1.0)
    bench.add_argument('--reserve-mb', type=int, default=48)
    bench.add_argument('--web-clients', type=int, default=5)
    bench.add_argument('--output', default='benchmark-report.json')

    logs = sub.add_parser('logs')
    logs.add_argument('-f', '--follow', action='store_true')
    logs.add_argument('-n', '--lines', type=int, default=100)

    args = parser.parse_args()
    s = settings()
    api = EngineAPI(s.api_host, s.api_port)

    if args.cmd == 'status':
        print(json.dumps(read_json(s.status_file) or {}, indent=2))
        return 0

    if args.cmd == 'web-url':
        host = socket.gethostname()
        print(f'http://{host}.local:{s.web_port}')
        state = read_json(s.status_file) or {}
        ip = state.get('system', {}).get('ipAddress')
        if ip:
            print(f'http://{ip}:{s.web_port}')
        return 0

    if args.cmd == 'obd-ports':
        print('USB:')
        for port in usb_candidates():
            print(' ', port)
        print('Bluetooth:', s.obd_bluetooth_port)
        try:
            print('Selected:', resolve(s))
        except Exception as exc:
            print('Selected: none -', exc)
        return 0

    if args.cmd == 'obd-catalog':
        for row in all_python_obd_commands():
            print(f"{str(row['mode']):>7}  {row['command']:<6}  {row['name']:<32} {row['description']}")
        return 0

    if args.cmd == 'vin':
        state = read_json(s.status_file) or {}
        print(state.get('obd', {}).get('vehicle', {}).get('VIN') or 'VIN not available')
        return 0

    if args.cmd == 'obd-discover-bt':
        print(discover_channel(args.mac))
        return 0

    if args.cmd == 'obd-bind-bt':
        bind(args.mac, args.channel)
        return 0

    if args.cmd == 'obd-reconnect':
        print(api.post('/obd/reconnect'))
        return 0

    if args.cmd == 'obd-transport':
        print(api.post('/obd/transport', {'transport': args.transport}))
        return 0

    if args.cmd == 'dtc-refresh':
        print(json.dumps(api.post('/dtc/refresh'), indent=2))
        return 0

    if args.cmd == 'dtc-clear':
        if not args.confirm:
            print('Refusing. Re-run with --confirm and make sure the engine is OFF.')
            return 2
        print(api.post('/dtc/clear', {'confirm': 'CLEAR_DTC_CONFIRMED'}))
        return 0

    if args.cmd == 'bluetooth-scan':
        print(json.dumps(api.post('/bluetooth/scan', {'seconds': args.seconds}), indent=2))
        return 0

    if args.cmd == 'bluetooth-pair':
        print(json.dumps(api.post('/bluetooth/pair', {'mac': args.mac, 'pin': args.pin}), indent=2))
        return 0

    if args.cmd == 'bluetooth-use-elm':
        payload = {'mac': args.mac}
        if args.channel is not None:
            payload['channel'] = args.channel
        print(json.dumps(api.post('/bluetooth/use-elm', payload), indent=2))
        return 0

    if args.cmd == 'oled-test':
        from .oled import test_display

        test_display(s, args.driver, max(0.2, args.seconds))
        return 0

    if args.cmd == 'benchmark':
        from .benchmark import run_benchmark

        report = run_benchmark(
            args.seconds,
            args.stress,
            args.reserve_mb,
            args.web_clients,
            args.output or None,
        )
        return 0 if report['result'] in {'PASS', 'WARN'} else 1

    if args.cmd == 'logs':
        command = ['journalctl', '-u', 'car-telemetry.service', '-n', str(args.lines), '--no-pager']
        if args.follow:
            command.append('-f')
        return subprocess.call(command)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
