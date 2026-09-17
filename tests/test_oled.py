from dataclasses import replace

import pytest

from car_telemetry import oled
from car_telemetry import oled_font as F
from car_telemetry.config import settings
from car_telemetry.oled import (
    OLEDDisplay,
    bottom_line,
    current_page,
    preview_scenarios,
    problems,
    render_frame,
    sample_snapshot,
    short_error,
    splash_frame,
    web_url,
)
from car_telemetry.state import DeviceState, queue_depth


def test_every_scenario_renders_a_distinct_full_size_frame():
    frames = {name: render_frame(snapshot, page) for name, (snapshot, page) in preview_scenarios().items()}
    assert all(frame.size == (128, 64) for frame in frames.values())
    assert all(frame.getbbox() is not None for frame in frames.values())
    assert len({frame.tobytes() for frame in frames.values()}) == len(frames)


def test_dashboard_stays_inside_the_display_with_long_names_and_coordinates():
    snapshot = sample_snapshot()
    snapshot['system']['wifiSsid'] = 'An Extremely Long Wireless Network Name'
    snapshot['system']['bluetoothDevice'] = 'A Very Long Bluetooth Adapter Name'
    snapshot['gps'].update(latitude=-33.868820, longitude=-151.209296, headingDegrees=359)
    for shift in (0, 1):
        frame = render_frame(snapshot, 'dashboard', shift=shift)
        assert frame.getbbox()[2] <= 128


def test_dashboard_handles_an_empty_snapshot():
    empty = {'obd': {}, 'gps': {}, 'publisher': {}, 'system': {}, 'events': {}}
    assert render_frame(empty, 'dashboard').getbbox() is not None
    # No network address means no QR link, so the dashboard is shown instead.
    assert render_frame(empty, 'qr').tobytes() == render_frame(empty, 'dashboard').tobytes()
    assert splash_frame(128, 64, 'PROTO-001').getbbox() is not None


def test_qr_encodes_the_local_web_app_url():
    snapshot = sample_snapshot()
    assert web_url(snapshot, 9090) == 'http://192.168.1.42:9090'
    matrix = oled.qr_matrix(web_url(snapshot, 8080))
    # 2px modules plus a quiet zone must fit the 64px-tall display.
    assert len(matrix) * 2 + 4 <= 64
    assert render_frame(snapshot, 'qr').tobytes() != render_frame(snapshot, 'dashboard').tobytes()


def test_qr_is_readable_by_a_decoder():
    cv2 = pytest.importorskip('cv2')
    numpy = pytest.importorskip('numpy')
    from PIL import Image

    image = render_frame(sample_snapshot(), 'qr').convert('L').resize((1024, 512), Image.NEAREST)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(numpy.array(image))
    assert decoded == 'http://192.168.1.42:8080'


def test_alert_banner_replaces_the_drive_block_but_keeps_status_and_web_address():
    snapshot = sample_snapshot()
    healthy = render_frame(snapshot, 'dashboard')
    snapshot['events']['possibleImpact'] = True
    alert = render_frame(snapshot, 'dashboard')
    assert alert.tobytes() != healthy.tobytes()
    # The status row and bottom line are unchanged.
    assert alert.crop((0, 0, 128, 8)).tobytes() == healthy.crop((0, 0, 128, 8)).tobytes()
    assert alert.crop((0, 57, 128, 64)).tobytes() == healthy.crop((0, 57, 128, 64)).tobytes()
    # An alert is never hidden behind the QR screen.
    assert render_frame(snapshot, 'qr').tobytes() == alert.tobytes()


def test_problems_are_ordered_and_explain_the_failure():
    snapshot = sample_snapshot()
    assert problems(snapshot) == []

    snapshot['obd'].update(connected=False, error='[Errno 5] Input/output error')
    snapshot['publisher'].update(connected=False, error='[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')
    snapshot['imu'].update(calibrationState='running', calibrating=True, calibrationPercent=64)
    assert problems(snapshot) == ['OBD BT I/O ERROR', 'CLOUD TLS CERT FAILED', 'IMU CAL 64% KEEP STILL']

    snapshot['system']['ipAddress'] = None
    assert problems(snapshot)[0] == 'NO NETWORK - JOIN WIFI'


def test_disabled_cloud_is_not_reported_as_a_problem():
    snapshot = sample_snapshot()
    snapshot['publisher'] = {'enabled': False, 'connected': False, 'error': 'MQTT_ENABLED is false'}
    assert problems(snapshot) == []


@pytest.mark.parametrize(
    ('message', 'label'),
    [
        ('python-OBD did not reach CAR_CONNECTED (status=ELM Connected)', 'NO ECU - IGNITION ON?'),
        ('[Errno -2] Name or service not known', 'DNS FAILED'),
        ('Not authorized', 'AUTH REJECTED'),
        ('MQTT_USERNAME and MQTT_PASSWORD must both be set in telemetry.env', 'NO CREDENTIALS'),
        ('something new', 'SOMETHING NEW'),
        ('', 'WAITING'),
    ],
)
def test_short_error(message, label):
    assert short_error(message, 'WAITING') == label


def test_bottom_line_alternates_between_problems_and_the_web_address():
    snapshot = sample_snapshot()
    assert bottom_line(snapshot, 8080, tick=0) == '192.168.1.42:8080'

    snapshot['publisher'].update(connected=False, error='timed out')
    lines = {bottom_line(snapshot, 8080, tick) for tick in range(0, 12)}
    assert lines == {'!CLOUD TIMEOUT', '192.168.1.42:8080'}


def test_queue_depth_reads_the_outbox_sections_not_the_old_mqtt_section():
    assert queue_depth({'frame': {'queueDepth': 42}, 'publisher': {'queueDepth': 7}}) == 42
    assert queue_depth({'publisher': {'queueDepth': 7}}) == 7
    assert queue_depth({'mqtt': {'bufferedMessages': 9}}) == 0


def test_qr_page_at_boot_and_on_request():
    assert current_page({}, elapsed=5, boot_qr_seconds=20, now=1000) == 'qr'
    assert current_page({}, elapsed=25, boot_qr_seconds=20, now=1000) == 'dashboard'
    requested = {'oled': {'qrUntil': 1060}}
    assert current_page(requested, elapsed=500, boot_qr_seconds=20, now=1000) == 'qr'
    assert current_page(requested, elapsed=500, boot_qr_seconds=20, now=1061) == 'dashboard'


def test_font_measures_and_fits_text():
    assert F.text_width('') == 0
    assert F.text_width('8') == 5
    assert F.text_width('88') == 11
    assert F.text_width('8', scale=2) == 10
    assert F.text_width('lower') == F.text_width('LOWER')
    fitted = F.fit('X' * 80, 60)
    assert F.text_width(fitted) <= 60


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
        'oled_access_seconds': 0,
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
    for section, values in sample_snapshot().items():
        if isinstance(values, dict):
            state.merge(section, values)
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
    assert oled_state['page'] == 'dashboard'
    assert oled_state['error'] is None
    assert oled_state['lastFrameAt']


def test_oled_worker_shows_qr_during_boot_window(monkeypatch):
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

    assert rendered == [('qr', 9090)] * 4


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


def test_save_previews_writes_enlarged_pngs(tmp_path):
    from PIL import Image

    paths = oled.save_previews(str(tmp_path), scale=2)
    assert len(paths) == len(preview_scenarios())
    assert Image.open(paths[0]).size == (256, 128)
