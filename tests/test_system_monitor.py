from car_telemetry import system_monitor
from car_telemetry.network import NetworkError


def test_wifi_ssid_reads_active_escaped_network(monkeypatch):
    output = '\n'.join(['no:Neighbour', r'yes:Cafe\: West', 'no:'])
    monkeypatch.setattr(system_monitor, 'nmcli', lambda *args, **kwargs: output)
    assert system_monitor._wifi_ssid() == 'Cafe: West'


def test_wifi_ssid_is_none_when_disconnected_or_nmcli_missing(monkeypatch):
    monkeypatch.setattr(system_monitor, 'nmcli', lambda *args, **kwargs: 'no:Neighbour')
    assert system_monitor._wifi_ssid() is None

    def missing(*args, **kwargs):
        raise NetworkError('no nmcli')

    monkeypatch.setattr(system_monitor, 'nmcli', missing)
    assert system_monitor._wifi_ssid() is None


def test_bluetooth_device_returns_connected_name(monkeypatch):
    devices = [
        {'name': 'Phone', 'connected': False},
        {'name': 'OBDII', 'connected': True},
    ]
    monkeypatch.setattr(system_monitor.bluetooth, 'devices', lambda: devices)
    assert system_monitor._bluetooth_device() == 'OBDII'

    monkeypatch.setattr(system_monitor.bluetooth, 'devices', lambda: devices[:1])
    assert system_monitor._bluetooth_device() is None
