from dataclasses import replace

import pytest

from car_telemetry import oled
from car_telemetry import oled_font as F
from car_telemetry.config import settings
from car_telemetry.oled import (
    PAGES,
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
        ('python-OBD did not reach CAR_CONNECTED (status=ELM Connected)', 'NO ECU/IGN OFF'),
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


def test_dashboard_shows_a_minute_then_each_page_twenty_seconds_including_qr():
    snapshot = sample_snapshot()
    assert current_page(snapshot, elapsed=5, boot_qr_seconds=20, now=1000) == 'qr'

    def page_at(seconds_after_boot_qr, state=snapshot):
        return current_page(state, 20 + seconds_after_boot_qr, 20, 1000, page_seconds=20, dashboard_seconds=60)

    assert page_at(0) == 'dashboard'
    assert page_at(59.9) == 'dashboard'
    order = [page_at(60 + 20 * index) for index in range(7)]
    assert order == ['overview', 'obd', 'gps', 'imu', 'cloud', 'pi', 'qr']
    assert page_at(60 + 20 * 7) == 'dashboard'

    # Without a network address there is no link, so the QR slot is skipped.
    offline = sample_snapshot()
    offline['system']['ipAddress'] = None
    assert [page_at(60 + 20 * index, offline) for index in range(7)] == [
        'overview', 'obd', 'gps', 'imu', 'cloud', 'pi', 'dashboard'
    ]

    requested = {**snapshot, 'oled': {'qrUntil': 1060}}
    assert current_page(requested, elapsed=500, boot_qr_seconds=20, now=1000) == 'qr'
    assert current_page(requested, elapsed=500, boot_qr_seconds=20, now=1061) == 'obd'


def test_each_page_is_distinct_and_marks_its_position():
    snapshot = sample_snapshot()
    frames = {page: render_frame(snapshot, page) for page in PAGES}
    assert len({frame.tobytes() for frame in frames.values()}) == len(PAGES)
    rotating = [page for page in PAGES if page not in ('dashboard', 'qr')]
    icons = {frames[page].crop((0, 0, 85, 8)).tobytes() for page in rotating}
    dots = {frames[page].crop((85, 0, 128, 8)).tobytes() for page in rotating}
    # Same status icons on every page; only the page-position marker moves.
    assert len(icons) == 1
    assert len(dots) == len(rotating)
    # The dashboard keeps the Pi temperature in that corner instead.
    assert frames['dashboard'].crop((0, 0, 85, 8)).tobytes() in icons


def test_default_timing_is_one_minute_dashboard_and_twenty_second_pages(monkeypatch, tmp_path):
    for key in ('OLED_PAGE_SECONDS', 'OLED_DASHBOARD_SECONDS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('TELEMETRY_ENV', str(tmp_path / 'missing.env'))
    monkeypatch.chdir(tmp_path)
    configured = settings()
    assert configured.oled_dashboard_seconds == 60
    assert configured.oled_page_seconds == 20


def test_pi_page_bars_and_power_status():
    healthy = sample_snapshot()
    stressed = sample_snapshot()
    stressed['system'].update(cpuPercent=96, memoryUsedMb=400, temperatureC=82)
    assert render_frame(healthy, 'pi').tobytes() != render_frame(stressed, 'pi').tobytes()
    assert oled.power_status(0) == f'PWR {F.CHECK}'
    assert oled.power_status(0x50005) == '!LOW VOLTS'
    assert oled.power_status(0x50000) == 'LOW V SEEN'
    assert oled.power_status(0x4) == '!THROTTLED'
    assert oled.power_status(None) == 'PWR --'


def test_overview_page_reports_each_subsystem():
    scenarios = preview_scenarios()
    healthy = render_frame(*scenarios['overview'])
    problems_frame = render_frame(*scenarios['overview-problems'])
    assert healthy.tobytes() != problems_frame.tobytes()


@pytest.mark.parametrize('page', PAGES)
def test_every_page_fits_with_long_values_and_missing_data(page):
    snapshot = sample_snapshot()
    snapshot['system']['wifiSsid'] = 'An Extremely Long Wireless Network Name'
    snapshot['obd']['error'] = 'x' * 200
    snapshot['gps'].update(latitude=-33.868820, longitude=-151.209296)
    snapshot['imu']['linearAccelerationMps2'] = {'x': -19.5, 'y': 12.25, 'z': -10.0}
    for shift in (0, 1):
        assert render_frame(snapshot, page, shift=shift).getbbox()[2] <= 128
    empty = {'obd': {}, 'gps': {}, 'imu': {}, 'publisher': {}, 'system': {}, 'events': {}}
    assert render_frame(empty, page).getbbox() is not None


def test_pi_page_shows_memory_and_storage_usage():
    snapshot = sample_snapshot()
    base = render_frame(snapshot, 'pi')
    snapshot['system']['diskFreeGb'] = 1.2
    assert render_frame(snapshot, 'pi').tobytes() != base.tobytes()
    snapshot['system']['memoryUsedMb'] = 400
    assert oled._memory_text(snapshot['system']) == 'RAM 400/416M'
    assert oled._storage_text(snapshot['system']) == 'SD 13.4/15G'
    assert oled._storage_text({}) == 'SD --G FREE'


def test_gps_page_hides_stale_position_after_losing_fix():
    fixed = sample_snapshot()
    lost = sample_snapshot()
    lost['gps']['validFix'] = False
    assert render_frame(fixed, 'gps').tobytes() != render_frame(lost, 'gps').tobytes()
    moved = sample_snapshot()
    moved['gps'].update(validFix=False, latitude=40.0, longitude=10.0, altitudeMeters=999, hdop=9.9)
    assert render_frame(lost, 'gps').tobytes() == render_frame(moved, 'gps').tobytes()


def test_obd_page_shows_adapter_voltage_when_ecu_voltage_is_missing():
    with_ecu = sample_snapshot()
    adapter_only = sample_snapshot()
    del adapter_only['obd']['signals']['CONTROL_MODULE_VOLTAGE']
    assert render_frame(adapter_only, 'obd').tobytes() != render_frame(with_ecu, 'obd').tobytes()
    no_voltage = sample_snapshot()
    del no_voltage['obd']['signals']['CONTROL_MODULE_VOLTAGE']
    del no_voltage['obd']['vehicle']['ELM_VOLTAGE']
    assert render_frame(no_voltage, 'obd').tobytes() != render_frame(adapter_only, 'obd').tobytes()


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


@pytest.mark.parametrize(
    ('message', 'label'),
    [
        ('broker refused connection: Not authorized', 'AUTH REJECTED'),
        ('broker refused connection: Bad user name or password', 'AUTH REJECTED'),
        ('[Errno 2] No such file or directory', 'CA CERT MISSING'),
        ('broker connection not established', 'NO CONNACK'),
        ('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed', 'TLS CERT FAILED'),
    ],
)
def test_cloud_error_labels(message, label):
    assert oled.cloud_error(message, 'CONNECTING') == label
