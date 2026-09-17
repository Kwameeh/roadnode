from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageDraw

from . import oled_font as F
from .config import Settings
from .state import DeviceState, queue_depth

# One constant dashboard. The QR screen only replaces it at boot and when
# asked for (`telemetry oled-qr` or the web app), so nothing rotates.
PAGES = ('dashboard', 'qr')

STATUS_Y = 0
ROW_Y = tuple(9 + F.LINE * index for index in range(7))  # 9, 17, ... 57
SPEED_WIDTH = 36
RIGHT_X = 40
BOTTOM_LINE_SECONDS = 3
BURN_IN_SHIFT_SECONDS = 30
COMPASS = ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')


def signal_value(signal: Any, default: Any = None):
    value = signal.get('value') if isinstance(signal, dict) else signal
    if isinstance(value, dict):
        value = value.get('value')
    return default if value is None else value


def number(value: Any, digits: int = 0, default: str = '--') -> str:
    try:
        return f'{float(value):.{digits}f}'
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Known failure messages, in check order, reduced to something that fits one line.
_ERROR_PATTERNS = (
    (r'certificate verify failed|ssl|tls', 'TLS CERT FAILED'),
    (r'mqtt_username|credential', 'NO CREDENTIALS'),
    (r'not authori[sz]ed|bad user name|password|auth', 'AUTH REJECTED'),
    (r'name or service not known|getaddrinfo|temporary failure in name', 'DNS FAILED'),
    (r'network is unreachable|no route to host', 'NO ROUTE'),
    (r'connection refused', 'REFUSED'),
    (r'timed out|timeout', 'TIMEOUT'),
    (r'car_connected', 'NO ECU - IGNITION ON?'),
    (r'input/output error', 'BT I/O ERROR'),
    (r'rfcomm|no such file', 'NO RFCOMM0 LINK'),
    (r'no obd port|no usb|not found', 'ADAPTER NOT FOUND'),
    (r'permission denied', 'PERMISSION DENIED'),
    (r'could not open port|serial', 'PORT NOT OPEN'),
)


def short_error(message: Any, fallback: str) -> str:
    text = str(message or '').strip()
    if not text:
        return fallback
    lowered = text.lower()
    for pattern, label in _ERROR_PATTERNS:
        if re.search(pattern, lowered):
            return label
    return re.sub(r'\s+', ' ', text).upper()


def problems(snapshot: dict) -> list[str]:
    """Most important first. Each entry is one bottom-line message."""
    obd = snapshot.get('obd', {})
    gps = snapshot.get('gps', {})
    imu = snapshot.get('imu', {})
    publisher = snapshot.get('publisher', {})
    system = snapshot.get('system', {})
    found: list[str] = []

    if system and not system.get('ipAddress'):
        found.append('NO NETWORK - JOIN WIFI')
    if obd.get('enabled') is not False and not obd.get('connected'):
        if obd.get('connecting'):
            found.append(f"OBD CONNECTING {str(obd.get('transport') or '').upper()}".strip())
        else:
            found.append('OBD ' + short_error(obd.get('error'), 'WAITING'))
    if publisher.get('enabled') and not publisher.get('connected'):
        found.append('CLOUD ' + short_error(publisher.get('error'), 'CONNECTING'))
    if gps.get('enabled') and gps.get('serialOpen') is False:
        found.append('GPS ' + short_error(gps.get('error'), 'PORT CLOSED'))
    elif gps.get('enabled') and gps.get('serialOpen') and not gps.get('received'):
        found.append('GPS NO DATA - CHECK TX')
    if imu.get('enabled') and imu.get('calibrationState') not in (None, 'valid'):
        if imu.get('calibrating') or imu.get('calibrationState') == 'running':
            found.append(f"IMU CAL {number(imu.get('calibrationPercent'))}% KEEP STILL")
        else:
            found.append('IMU ' + short_error(imu.get('error'), str(imu.get('calibrationState')).upper()))
    return found


def _alert(snapshot: dict) -> tuple[str, str] | None:
    events = snapshot.get('events', {})
    if events.get('possibleImpact'):
        return 'IMPACT', 'CHECK VEHICLE SAFELY'
    coolant = _float(signal_value(snapshot.get('obd', {}).get('signals', {}).get('COOLANT_TEMP')))
    if coolant is not None and coolant >= 110:
        return f'HOT {coolant:.0f}{F.DEGREE}', 'STOP WHEN SAFE'
    return None


def _right(draw: ImageDraw.ImageDraw, text: str, y: int, width: int, fill: int = 255):
    F.draw_text(draw, (width - 1 - F.text_width(text), y), text, fill=fill)


def _status_row(draw: ImageDraw.ImageDraw, snapshot: dict, width: int, x: int, blink_on: bool):
    obd = snapshot.get('obd', {})
    gps = snapshot.get('gps', {})
    imu = snapshot.get('imu', {})
    publisher = snapshot.get('publisher', {})
    system = snapshot.get('system', {})

    def item(icon: str, state: str, suffix: str = ''):
        """state: ok (steady), bad (blinks), off (a dash: disabled or not used)."""
        nonlocal x
        if state == 'off':
            x = F.draw_text(draw, (x, STATUS_Y), '-')
        elif state == 'ok' or blink_on:
            x = F.draw_text(draw, (x, STATUS_Y), icon)
        else:
            x += F.text_width(icon) + 1
        if suffix:
            x = F.draw_text(draw, (x, STATUS_Y), suffix)
        x += 2

    item(F.CAR, 'off' if obd.get('enabled') is False else 'ok' if obd.get('connected') else 'bad')
    sats = gps.get('satellites')
    item(
        F.PIN,
        'off' if gps.get('enabled') is False else 'ok' if gps.get('validFix') else 'bad',
        str(sats) if sats is not None else '',
    )
    item(F.CLOUD, 'off' if not publisher.get('enabled') else 'ok' if publisher.get('connected') else 'bad')
    item(F.WIFI, 'ok' if system.get('ipAddress') else 'bad')
    item(F.BT, 'ok' if system.get('bluetoothDevice') else 'off')
    item(
        F.AXES,
        'off' if imu.get('enabled') is False else 'ok' if imu.get('calibrationState') == 'valid' else 'bad',
    )

    temperature = _float(system.get('temperatureC'))
    if temperature is not None:
        _right(draw, f'{temperature:.0f}{F.DEGREE}C', STATUS_Y, width)


def _speed(snapshot: dict) -> tuple[str, str]:
    obd = snapshot.get('obd', {})
    gps = snapshot.get('gps', {})
    if obd.get('connected'):
        speed = _float(signal_value(obd.get('signals', {}).get('SPEED')))
        if speed is not None:
            return f'{speed:.0f}', 'KM/H'
    if gps.get('validFix') and _float(gps.get('speedKph')) is not None:
        return f"{float(gps['speedKph']):.0f}", 'GPS'
    return '--', 'KM/H'


def _location_line(gps: dict, width: int) -> str:
    if gps.get('enabled') is False:
        return f'{F.PIN}GPS OFF'
    if not gps.get('validFix'):
        sats = gps.get('satellites')
        detail = f'{sats} SATS' if sats is not None else 'NO DATA' if not gps.get('received') else ''
        return f'{F.PIN}NO FIX {detail}'.rstrip()
    heading = _float(gps.get('headingDegrees'))
    suffix = ''
    if heading is not None:
        suffix = f' {heading:.0f}{F.DEGREE}{COMPASS[int((heading % 360) / 45 + 0.5) % 8]}'
    for digits in (4, 3, 2):
        line = f"{F.PIN}{number(gps.get('latitude'), digits)},{number(gps.get('longitude'), digits)}"
        if F.text_width(line + suffix) <= width - 2:
            return line + suffix
    return line


def _uptime(seconds: Any) -> str:
    value = _float(seconds)
    if value is None:
        return ''
    hours, minutes = int(value // 3600), int(value % 3600 // 60)
    return f'{hours // 24}D{hours % 24}H' if hours >= 24 else f'{hours}H{minutes:02d}M'


def _imu_text(imu: dict) -> str:
    if imu.get('enabled') is False:
        return f'{F.AXES}OFF'
    state = imu.get('calibrationState')
    if state == 'valid':
        g = _float(imu.get('resultantG'))
        return f'{F.AXES}{F.CHECK}' + (f' {g:.2f}G' if g is not None else '')
    if imu.get('calibrating') or state == 'running':
        return f"{F.AXES}CAL {number(imu.get('calibrationPercent'))}%"
    return f'{F.AXES}{F.CROSS}' + (f' {str(state).upper()}' if state else '')


def render_dashboard(
    snapshot: dict,
    width: int = 128,
    height: int = 64,
    shift: int = 0,
    web_port: int = 8080,
    tick: int = 0,
) -> Image.Image:
    image = Image.new('1', (width, height))
    draw = ImageDraw.Draw(image)
    # Every glyph leaves its right-hand column blank, so a 1px shift never clips.
    shift = max(0, min(1, shift))
    usable = width - shift
    obd = snapshot.get('obd', {})
    gps = snapshot.get('gps', {})
    system = snapshot.get('system', {})
    signals = obd.get('signals', {})
    frame = snapshot.get('frame', {})

    _status_row(draw, snapshot, width, shift, blink_on=tick % 2 == 0)

    alert = _alert(snapshot)
    if alert:
        draw.rectangle((0, ROW_Y[0] - 1, width - 1, ROW_Y[3] - 2), fill=255)
        title = F.fit(alert[0], width - 4, scale=2)
        F.draw_text(draw, ((width - F.text_width(title, 2)) // 2, ROW_Y[0]), title, fill=0, scale=2)
        F.draw_text(draw, ((width - F.text_width(alert[1])) // 2, ROW_Y[2] - 1), alert[1], fill=0)
    else:
        speed, unit = _speed(snapshot)
        F.draw_text(draw, (shift + SPEED_WIDTH - F.text_width(speed, 2), ROW_Y[0]), speed, scale=2)
        F.draw_text(draw, (shift, ROW_Y[2]), unit)

        rpm = number(signal_value(signals.get('RPM')))
        coolant = number(signal_value(signals.get('COOLANT_TEMP')))
        volts = number(signal_value(signals.get('CONTROL_MODULE_VOLTAGE')), 1)
        fuel = number(signal_value(signals.get('FUEL_LEVEL')))
        F.draw_text(draw, (shift + RIGHT_X, ROW_Y[0]), f'{F.RPM}{rpm} {F.THERMO}{coolant}{F.DEGREE}')
        F.draw_text(draw, (shift + RIGHT_X, ROW_Y[1]), f'{F.BOLT}{volts}V {F.FUEL}{fuel}%')

        dtc = obd.get('dtc', {}).get('storedCount')
        mode = str(frame.get('mode') or '').upper()
        F.draw_text(
            draw,
            (shift + RIGHT_X, ROW_Y[2]),
            f"{F.WARN}{dtc if dtc is not None else '-'} {F.UPLOAD}{queue_depth(snapshot)}",
        )
        if mode:
            _right(draw, mode, ROW_Y[2], usable)

    F.draw_text(draw, (shift, ROW_Y[3]), F.fit(_location_line(gps, usable), usable))

    ssid = system.get('wifiSsid') or ('LAN' if system.get('ipAddress') else '--')
    F.draw_text(draw, (shift, ROW_Y[4]), F.fit(f'{F.WIFI}{ssid}', 62))
    bt_name = system.get('bluetoothDevice') or str(obd.get('transport') or '--')
    bt_text = F.fit(f'{F.BT}{bt_name}', usable - 66)
    _right(draw, bt_text, ROW_Y[4], usable)

    F.draw_text(draw, (shift, ROW_Y[5]), F.fit(_imu_text(snapshot.get('imu', {})), 62))
    cpu = _float(system.get('cpuPercent'))
    right = ' '.join(part for part in (f'{cpu:.0f}%' if cpu is not None else '', _uptime(system.get('uptimeSeconds'))) if part)
    if right:
        _right(draw, right, ROW_Y[5], usable)

    F.draw_text(draw, (shift, ROW_Y[6]), F.fit(bottom_line(snapshot, web_port, tick), usable))
    return image


def web_address(snapshot: dict, web_port: int) -> str | None:
    ip = snapshot.get('system', {}).get('ipAddress')
    return f'{ip}:{web_port}' if ip else None


def bottom_line(snapshot: dict, web_port: int, tick: int = 0) -> str:
    """The web address when healthy; otherwise problems take turns with it."""
    messages = [f'!{message}' for message in problems(snapshot)]
    address = web_address(snapshot, web_port)
    if address:
        messages.append(address)
    if not messages:
        return f"{snapshot.get('system', {}).get('hostname') or socket.gethostname()}.LOCAL:{web_port}"
    return messages[(tick // BOTTOM_LINE_SECONDS) % len(messages)]


def web_url(snapshot: dict, web_port: int) -> str | None:
    address = web_address(snapshot, web_port)
    return f'http://{address}' if address else None


def qr_matrix(data: str) -> list[list[bool]]:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M

    for correction in (ERROR_CORRECT_M, ERROR_CORRECT_L):
        code = qrcode.QRCode(error_correction=correction, border=0, box_size=1)
        code.add_data(data)
        code.make(fit=True)
        matrix = code.get_matrix()
        # Keep modules 2px wide on a 64px-tall display: phones cannot read 1px.
        if len(matrix) * 2 + 4 <= 64:
            return matrix
    return matrix


def render_qr(snapshot: dict, width: int = 128, height: int = 64, web_port: int = 8080) -> Image.Image | None:
    url = web_url(snapshot, web_port)
    if url is None:
        return None
    image = Image.new('1', (width, height))
    draw = ImageDraw.Draw(image)

    matrix = qr_matrix(url)
    size = len(matrix)
    scale = max(1, min((height - 4) // size, 2 if size * 2 + 4 <= height else 1))
    side = size * scale
    # A lit square is the QR quiet zone; modules are dark pixels on it.
    draw.rectangle((0, 0, height - 1, height - 1), fill=255)
    left = top = (height - side) // 2
    for row_index, row in enumerate(matrix):
        for col_index, dark in enumerate(row):
            if dark:
                x = left + col_index * scale
                y = top + row_index * scale
                draw.rectangle((x, y, x + scale - 1, y + scale - 1), fill=0)

    x = height + 3
    column = width - x
    system = snapshot.get('system', {})
    address = web_address(snapshot, web_port) or ''
    lines = ['SCAN FOR', 'WEB APP', '']
    # Wrap the address at the dots so the IP stays readable.
    current = ''
    for part in re.split(r'(?<=[.:])', address):
        if current and F.text_width(current + part) > column:
            lines.append(current)
            current = part
        else:
            current += part
    if current:
        lines.append(current)
    lines.append('')
    lines.append(f"{F.WIFI}{system.get('wifiSsid') or 'LAN'}")
    for index, line in enumerate(lines[:8]):
        F.draw_text(draw, (x, index * F.LINE), F.fit(line, column))
    return image


def render_frame(
    snapshot: dict,
    page: str = 'dashboard',
    width: int = 128,
    height: int = 64,
    shift: int = 0,
    web_port: int = 8080,
    tick: int = 0,
) -> Image.Image:
    if page == 'qr' and not _alert(snapshot):
        image = render_qr(snapshot, width, height, web_port)
        if image is not None:
            return image
    return render_dashboard(snapshot, width, height, shift, web_port, tick)


def splash_frame(width: int, height: int, device_id: str) -> Image.Image:
    image = Image.new('1', (width, height))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, height - 1), outline=255)
    title = 'ROADNODE'
    F.draw_text(draw, ((width - F.text_width(title, 2)) // 2, 14), title, scale=2)
    subtitle = 'VEHICLE TELEMETRY'
    F.draw_text(draw, ((width - F.text_width(subtitle)) // 2, 36), subtitle)
    ident = F.fit(device_id, width - 8)
    F.draw_text(draw, ((width - F.text_width(ident)) // 2, 48), ident)
    return image


def current_page(snapshot: dict, elapsed: float, boot_qr_seconds: float, now: float) -> str:
    """QR while the boot window is open or a request is active, else the dashboard."""
    requested_until = _float(snapshot.get('oled', {}).get('qrUntil'))
    if elapsed < boot_qr_seconds or (requested_until is not None and now < requested_until):
        return 'qr'
    return 'dashboard'


class OLEDDisplay:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.device = None
        self.lock = threading.RLock()

    def open(self):
        if self.device is not None:
            return self.device
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106, ssd1306

        drivers = {'sh1106': sh1106, 'ssd1306': ssd1306}
        try:
            driver = drivers[self.settings.oled_driver]
        except KeyError as exc:
            raise ValueError('OLED_DRIVER must be sh1106 or ssd1306') from exc
        serial = i2c(port=self.settings.oled_i2c_bus, address=self.settings.oled_address)
        self.device = driver(
            serial,
            width=self.settings.oled_width,
            height=self.settings.oled_height,
            rotate=self.settings.oled_rotation,
        )
        self.device.contrast(self.settings.oled_contrast)
        self.device.clear()
        return self.device

    def show(self, image: Image.Image):
        if not self.settings.oled_enabled:
            return
        with self.lock:
            self.open().display(image)

    def clear(self):
        with self.lock:
            if self.device is not None:
                self.device.clear()


def worker(settings: Settings, state: DeviceState, stop: threading.Event):
    state.merge('oled', {'enabled': settings.oled_enabled, 'driver': settings.oled_driver})
    if not settings.oled_enabled:
        return
    oled = OLEDDisplay(settings)
    started = time.monotonic()
    last_page = None
    splash_shown = False
    try:
        while not stop.is_set():
            try:
                if not splash_shown:
                    oled.show(splash_frame(settings.oled_width, settings.oled_height, settings.device_id))
                    state.merge('oled', {'connected': True, 'page': 'startup', 'error': None})
                    splash_shown = True
                    stop.wait(2.0)
                    started = time.monotonic()
                    continue

                elapsed = max(0.0, time.monotonic() - started)
                snapshot = state.snapshot()
                page = current_page(snapshot, elapsed, settings.oled_access_seconds, time.time())
                frame = render_frame(
                    snapshot,
                    page,
                    settings.oled_width,
                    settings.oled_height,
                    shift=int(elapsed / BURN_IN_SHIFT_SECONDS) % 2,
                    web_port=settings.web_port,
                    tick=int(elapsed),
                )
                oled.show(frame)
                shown = 'alert' if _alert(snapshot) else page
                if page == 'qr' and web_url(snapshot, settings.web_port) is None:
                    shown = 'dashboard'
                state.merge(
                    'oled',
                    {
                        'connected': True,
                        'driver': settings.oled_driver,
                        'page': shown,
                        'lastFrameAt': datetime.now(timezone.utc).isoformat(),
                        'error': None,
                    },
                )
                last_page = page
                stop.wait(0.5)
            except Exception as exc:
                state.merge(
                    'oled',
                    {
                        'connected': False,
                        'driver': settings.oled_driver,
                        'page': last_page,
                        'error': str(exc),
                    },
                )
                oled.device = None
                stop.wait(5.0)
    finally:
        try:
            oled.clear()
        except Exception:
            pass


def sample_snapshot(device_id: str = 'PROTO-001', web_port: int = 8080) -> dict:
    """A healthy vehicle, used by `telemetry oled-test` and the preview images."""
    return {
        'deviceId': device_id,
        'obd': {
            'enabled': True,
            'connected': True,
            'transport': 'bluetooth',
            'signals': {
                'SPEED': {'value': 72},
                'RPM': {'value': 2450},
                'COOLANT_TEMP': {'value': 91},
                'CONTROL_MODULE_VOLTAGE': {'value': 13.9},
                'FUEL_LEVEL': {'value': 64},
            },
            'dtc': {'storedCount': 0},
        },
        'gps': {
            'enabled': True,
            'serialOpen': True,
            'received': True,
            'validFix': True,
            'satellites': 9,
            'headingDegrees': 241,
            'latitude': 5.6037,
            'longitude': -0.1870,
            'speedKph': 71,
        },
        'imu': {'enabled': True, 'calibrationState': 'valid', 'resultantG': 0.04},
        'publisher': {'enabled': True, 'connected': True, 'published': 1200},
        'frame': {'mode': 'active', 'queueDepth': 0},
        'system': {
            'ipAddress': '192.168.1.42',
            'hostname': socket.gethostname(),
            'wifiSsid': 'RoadNode-WiFi',
            'bluetoothDevice': 'OBDII',
            'temperatureC': 47.2,
            'cpuPercent': 18,
            'uptimeSeconds': 8040,
        },
        'events': {},
    }


def preview_scenarios(device_id: str = 'PROTO-001') -> dict[str, tuple[dict, str]]:
    healthy = sample_snapshot(device_id)

    def variant(**sections):
        snapshot = sample_snapshot(device_id)
        for section, values in sections.items():
            snapshot[section] = {**snapshot.get(section, {}), **values}
        return snapshot

    return {
        'healthy': (healthy, 'dashboard'),
        'qr': (healthy, 'qr'),
        'obd-down': (
            variant(obd={'connected': False, 'error': '[Errno 5] Input/output error: /dev/rfcomm0', 'signals': {}}),
            'dashboard',
        ),
        'no-gps-fix': (variant(gps={'validFix': False, 'satellites': 3}), 'dashboard'),
        'cloud-offline': (
            variant(publisher={'connected': False, 'error': '[SSL: CERTIFICATE_VERIFY_FAILED]'}, frame={'mode': 'active', 'queueDepth': 42}),
            'dashboard',
        ),
        'imu-calibrating': (
            variant(imu={'calibrationState': 'running', 'calibrating': True, 'calibrationPercent': 64}),
            'dashboard',
        ),
        'impact-alert': (variant(events={'possibleImpact': True}), 'dashboard'),
    }


def save_previews(out_dir: str, scale: int = 4, device_id: str = 'PROTO-001', web_port: int = 8080) -> list[str]:
    """Write each scenario as an enlarged PNG so a layout can be reviewed without hardware."""
    from pathlib import Path

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (snapshot, page) in preview_scenarios(device_id).items():
        image = render_frame(snapshot, page, web_port=web_port)
        path = target / f'oled-{name}.png'
        image.resize((image.width * scale, image.height * scale), Image.NEAREST).save(path)
        written.append(str(path))
    return written


def test_display(settings: Settings, driver: str | None = None, seconds: float = 3.0) -> None:
    selected = replace(settings, oled_enabled=True, oled_driver=driver or settings.oled_driver)
    oled = OLEDDisplay(selected)
    try:
        oled.show(splash_frame(selected.oled_width, selected.oled_height, selected.device_id))
        time.sleep(min(2.0, max(0.2, seconds)))
        for snapshot, page in preview_scenarios(selected.device_id).values():
            oled.show(
                render_frame(snapshot, page, selected.oled_width, selected.oled_height, 0, selected.web_port)
            )
            time.sleep(max(0.2, seconds))
    finally:
        oled.clear()
