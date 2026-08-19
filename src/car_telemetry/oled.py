from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .state import DeviceState

PAGES = ('drive', 'location', 'health', 'connectivity')


def _font(size: int, bold: bool = False):
    names = ('DejaVuSans-Bold.ttf', 'DejaVuSans.ttf') if bold else ('DejaVuSans.ttf',)
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


SMALL_FONT = _font(9)
BODY_FONT = _font(11)
BODY_BOLD = _font(11, bold=True)
VALUE_FONT = _font(27, bold=True)
SPLASH_FONT = _font(20, bold=True)


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


def _center(draw: ImageDraw.ImageDraw, text: str, y: int, font, width: int, fill: int = 255):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) // 2, y), text, font=font, fill=fill)


def _header(draw: ImageDraw.ImageDraw, title: str, snapshot: dict, width: int, shift: int):
    obd = snapshot.get('obd', {})
    gps = snapshot.get('gps', {})
    mqtt = snapshot.get('mqtt', {})
    draw.text((shift, 0), title, font=SMALL_FONT, fill=255)
    flags = f"O{'+' if obd.get('connected') else '-'} G{'+' if gps.get('validFix') else '-'} M{'+' if mqtt.get('connected') else '-'}"
    box = draw.textbbox((0, 0), flags, font=SMALL_FONT)
    draw.text((width - (box[2] - box[0]) - shift, 0), flags, font=SMALL_FONT, fill=255)
    draw.line((shift, 11, width - 1 - shift, 11), fill=96)


def _alert(snapshot: dict) -> tuple[str, str] | None:
    events = snapshot.get('events', {})
    if events.get('possibleImpact'):
        return 'IMPACT', 'Check vehicle safely'
    coolant = signal_value(snapshot.get('obd', {}).get('signals', {}).get('COOLANT_TEMP'))
    try:
        if float(coolant) >= 110:
            return 'TEMP WARNING', f'Coolant {number(coolant)} C'
    except (TypeError, ValueError):
        pass
    return None


def render_frame(snapshot: dict, page: str, width: int = 128, height: int = 64, shift: int = 0) -> Image.Image:
    image = Image.new('1', (width, height))
    draw = ImageDraw.Draw(image)
    shift = max(0, min(1, shift))
    alert = _alert(snapshot)
    if alert:
        draw.rectangle((0, 0, width - 1, height - 1), outline=255)
        _center(draw, alert[0], 11, BODY_BOLD, width)
        _center(draw, alert[1], 32, BODY_FONT, width)
        _center(draw, 'STOP WHEN SAFE', 49, SMALL_FONT, width)
        return image

    obd = snapshot.get('obd', {})
    gps = snapshot.get('gps', {})
    mqtt = snapshot.get('mqtt', {})
    system = snapshot.get('system', {})
    signals = obd.get('signals', {})

    if page == 'drive':
        _header(draw, 'DRIVE', snapshot, width, shift)
        if not obd.get('connected'):
            _center(draw, 'WAITING', 19, SPLASH_FONT, width)
            _center(draw, f"OBD {str(obd.get('transport') or 'AUTO').upper()}", 46, SMALL_FONT, width)
            return image
        speed = number(signal_value(signals.get('SPEED')))
        _center(draw, speed, 12, VALUE_FONT, width)
        _center(draw, 'km/h', 39, SMALL_FONT, width)
        rpm = number(signal_value(signals.get('RPM')))
        coolant = number(signal_value(signals.get('COOLANT_TEMP')))
        _center(draw, f'RPM {rpm}   COOL {coolant}C', 51, SMALL_FONT, width)
        return image

    if page == 'location':
        _header(draw, 'LOCATION', snapshot, width, shift)
        if not gps.get('validFix'):
            _center(draw, 'NO GPS FIX', 22, BODY_BOLD, width)
            _center(draw, 'Searching for satellites', 43, SMALL_FONT, width)
            return image
        draw.text((shift, 16), f"FIX  SAT {gps.get('satellites', '--')}", font=BODY_BOLD, fill=255)
        draw.text((shift, 30), f"HDG {number(gps.get('headingDegrees'))} deg", font=BODY_FONT, fill=255)
        draw.text((shift, 44), f"{number(gps.get('latitude'), 4)}", font=SMALL_FONT, fill=255)
        draw.text((64, 44), f"{number(gps.get('longitude'), 4)}", font=SMALL_FONT, fill=255)
        return image

    if page == 'health':
        _header(draw, 'VEHICLE HEALTH', snapshot, width, shift)
        dtc = obd.get('dtc', {})
        voltage = number(signal_value(signals.get('CONTROL_MODULE_VOLTAGE')), 1)
        coolant = number(signal_value(signals.get('COOLANT_TEMP')))
        fuel = number(signal_value(signals.get('FUEL_LEVEL')))
        draw.text((shift, 16), f'VOLT  {voltage} V', font=BODY_FONT, fill=255)
        draw.text((shift, 30), f'COOL  {coolant} C', font=BODY_FONT, fill=255)
        draw.text((shift, 44), f"FUEL  {fuel}%   DTC {dtc.get('storedCount', 0)}", font=BODY_FONT, fill=255)
        return image

    _header(draw, 'CONNECTIVITY', snapshot, width, shift)
    draw.text((shift, 15), f"OBD   {'ONLINE' if obd.get('connected') else 'WAITING'}", font=BODY_FONT, fill=255)
    draw.text((shift, 28), f"GPS   {'FIX' if gps.get('validFix') else 'SEARCHING'}", font=BODY_FONT, fill=255)
    queued = mqtt.get('bufferedMessages', 0)
    draw.text((shift, 41), f"CLOUD {'ONLINE' if mqtt.get('connected') else 'OFFLINE'} Q{queued}", font=BODY_FONT, fill=255)
    ip = system.get('ipAddress') or f'{socket.gethostname()}.local'
    draw.text((shift, 54), str(ip)[:21], font=SMALL_FONT, fill=255)
    return image


def splash_frame(width: int, height: int, device_id: str) -> Image.Image:
    image = Image.new('1', (width, height))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, height - 1), outline=255)
    _center(draw, 'ROADNODE', 12, SPLASH_FONT, width)
    _center(draw, 'VEHICLE TELEMETRY', 36, SMALL_FONT, width)
    _center(draw, device_id[:20], 49, SMALL_FONT, width)
    return image


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
                page = PAGES[int(elapsed / max(1.0, settings.oled_page_seconds)) % len(PAGES)]
                snapshot = state.snapshot()
                frame = render_frame(
                    snapshot,
                    page,
                    settings.oled_width,
                    settings.oled_height,
                    shift=int(elapsed / max(1.0, settings.oled_page_seconds)) % 2,
                )
                oled.show(frame)
                state.merge(
                    'oled',
                    {
                        'connected': True,
                        'driver': settings.oled_driver,
                        'page': 'alert' if _alert(snapshot) else page,
                        'lastFrameAt': datetime.now(timezone.utc).isoformat(),
                        'error': None,
                    },
                )
                last_page = page
                stop.wait(min(0.5, max(0.2, settings.oled_page_seconds)))
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


def test_display(settings: Settings, driver: str | None = None, seconds: float = 3.0) -> None:
    selected = replace(settings, oled_enabled=True, oled_driver=driver or settings.oled_driver)
    oled = OLEDDisplay(selected)
    sample = {
        'deviceId': selected.device_id,
        'obd': {
            'connected': True,
            'transport': 'usb',
            'signals': {
                'SPEED': {'value': 72},
                'RPM': {'value': 2450},
                'COOLANT_TEMP': {'value': 91},
                'CONTROL_MODULE_VOLTAGE': {'value': 13.9},
                'FUEL_LEVEL': {'value': 64},
            },
            'dtc': {'storedCount': 0},
        },
        'gps': {'validFix': True, 'satellites': 9, 'headingDegrees': 241, 'latitude': 5.6037, 'longitude': -0.1870},
        'mqtt': {'connected': True, 'bufferedMessages': 0},
        'system': {'ipAddress': '192.168.1.42'},
        'events': {},
    }
    try:
        oled.show(splash_frame(selected.oled_width, selected.oled_height, selected.device_id))
        time.sleep(min(2.0, max(0.2, seconds)))
        for index, page in enumerate(PAGES):
            oled.show(render_frame(sample, page, selected.oled_width, selected.oled_height, index % 2))
            time.sleep(max(0.2, seconds))
    finally:
        oled.clear()
