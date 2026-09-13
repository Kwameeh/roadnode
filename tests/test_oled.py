from dataclasses import replace

from car_telemetry import oled
from car_telemetry.config import settings
from car_telemetry.oled import OLEDDisplay, PAGES, render_frame, splash_frame
from car_telemetry.state import DeviceState


def sample_state():
    return {
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
        'gps': {
            'validFix': True,
            'satellites': 9,
            'headingDegrees': 241,
            'latitude': 5.6037,
            'longitude': -0.1870,
        },
        'mqtt': {'connected': True, 'bufferedMessages': 0},
        'system': {
            'ipAddress': '192.168.1.42',
            'hostname': 'roadnode',
            'wifiSsid': 'Home WiFi',
            'bluetoothDevice': 'OBDII',
        },
        'events': {},
    }


def test_all_oled_pages_render_distinct_non_empty_frames():
    frames = [render_frame(sample_state(), page) for page in PAGES]
    assert all(frame.size == (128, 64) for frame in frames)
    assert all(frame.getbbox() is not None for frame in frames)
    assert len({frame.tobytes() for frame in frames}) == len(PAGES)


def test_oled_alert_overrides_carousel_page():
    state = sample_state()
    state['events']['possibleImpact'] = True
    drive = render_frame(state, 'drive')
    location = render_frame(state, 'location')
    assert drive.tobytes() == location.tobytes()


def test_oled_handles_missing_vehicle_and_gps_data():
    state = {'obd': {}, 'gps': {}, 'mqtt': {}, 'system': {}, 'events': {}}
    assert render_frame(state, 'drive').getbbox() is not None
    assert render_frame(state, 'location').getbbox() is not None
    assert render_frame(state, 'access').getbbox() is not None
    assert splash_frame(128, 64, 'PROTO-001').getbbox() is not None


def test_access_page_reflects_port_and_links_and_stays_on_screen():
    state = sample_state()
    base = render_frame(state, 'access', web_port=8080)
    assert render_frame(state, 'access', web_port=9090).tobytes() != base.tobytes()
    state['system']['wifiSsid'] = None
    assert render_frame(state, 'access', web_port=8080).tobytes() != base.tobytes()

    state = sample_state()
    state['system']['hostname'] = 'a-very-long-roadnode-hostname-for-testing'
    state['system']['wifiSsid'] = 'An Extremely Long Wireless Network Name'
    frame = render_frame(state, 'access')
    assert frame.getbbox()[2] <= 128


def test_fit_truncates_to_width():
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new('1', (128, 64)))
    assert oled._fit(draw, 'short', oled.SMALL_FONT, 128) == 'short'
    fitted = oled._fit(draw, 'x' * 80, oled.SMALL_FONT, 128)
    assert fitted.endswith('..')
    assert draw.textlength(fitted, font=oled.SMALL_FONT) <= 128


class ControlledStop:
    def __init__(self, waits_until_stop):
        self.waits_until_stop = waits_until_stop
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) >= self.waits_until_stop:
            self.stopped = True
        return self.stopped


def oled_settings(**overrides):
    values = {
        'device_id': 'PROTO-001',
        'oled_enabled': True,
        'oled_driver': 'sh1106',
        'oled_width': 128,
        'oled_height': 64,
        'oled_page_seconds': 3,
    }
    values.update(overrides)
    return replace(settings(), **values)


def test_oled_worker_retries_transient_failure_updates_state_and_clears(monkeypatch):
    displays = []

    class FlakyDisplay:
        def __init__(self, _settings):
            self.device = object()
            self.show_attempts = 0
            self.frames = []
            self.cleared = False
            displays.append(self)

        def show(self, frame):
            self.show_attempts += 1
            if self.show_attempts == 1:
                raise OSError('temporary i2c failure')
            self.device = object()
            self.frames.append(frame)

        def clear(self):
            self.cleared = True

    monkeypatch.setattr(oled, 'OLEDDisplay', FlakyDisplay)
    state = DeviceState('PROTO-001', 'VEH-001', 1)
    state.merge('obd', sample_state()['obd'])
    state.merge('gps', sample_state()['gps'])
    state.merge('mqtt', sample_state()['mqtt'])
    stop = ControlledStop(waits_until_stop=3)

    oled.worker(oled_settings(), state, stop)

    display = displays[0]
    assert display.show_attempts == 3
    assert len(display.frames) == 2
    assert display.cleared is True
    assert stop.waits[0] == 5.0
    assert stop.waits[1] == 2.0
    oled_state = state.snapshot()['oled']
    assert oled_state['connected'] is True
    assert oled_state['page'] in PAGES
    assert oled_state['error'] is None
    assert oled_state['lastFrameAt']


def test_oled_worker_holds_access_page_after_splash(monkeypatch):
    rendered = []

    class RecordingDisplay:
        def __init__(self, _settings):
            self.device = object()

        def show(self, frame):
            pass

        def clear(self):
            pass

    def record(snapshot, page, *args, **kwargs):
        rendered.append((page, kwargs.get('web_port')))
        return splash_frame(128, 64, 'PROTO-001')

    monkeypatch.setattr(oled, 'OLEDDisplay', RecordingDisplay)
    monkeypatch.setattr(oled, 'render_frame', record)
    state = DeviceState('PROTO-001', 'VEH-001', 1)

    oled.worker(
        oled_settings(oled_access_seconds=60, web_port=9090), state, ControlledStop(waits_until_stop=5)
    )

    assert rendered == [('access', 9090)] * 4


def test_oled_worker_disabled_does_not_open_display(monkeypatch):
    def fail_display(_settings):
        raise AssertionError('display should not be constructed')

    monkeypatch.setattr(oled, 'OLEDDisplay', fail_display)
    state = DeviceState('PROTO-001', 'VEH-001', 1)

    oled.worker(
        oled_settings(oled_enabled=False), state, ControlledStop(waits_until_stop=1)
    )

    assert state.snapshot()['oled'] == {'enabled': False, 'driver': 'sh1106'}


def test_oled_display_show_is_noop_when_disabled(monkeypatch):
    display = OLEDDisplay(oled_settings(oled_enabled=False))
    monkeypatch.setattr(
        display, 'open', lambda: (_ for _ in ()).throw(AssertionError('must not open'))
    )

    assert display.show(splash_frame(128, 64, 'PROTO-001')) is None
    assert display.device is None
