import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from car_telemetry import network, web_app


def test_scan_handles_escaped_ssids_duplicates_and_unsupported_security(monkeypatch):
    output = '\n'.join([
        r':Cafe\: West\\Guest:45:WPA2:wlan0',
        r'*:Cafe\: West\\Guest:30:WPA2:wlan0',
        ':Open:90:--:wlan0', ':Office:80:WPA2 802.1X:wlan0', ':Legacy:10:WEP:wlan0',
        ':Open:50:--:wlan0', ':Open:50:--:wlan1', ':Hidden:60:WPA3:wlan0', '::40:WPA2:wlan0',
    ])
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return 'enabled' if args == ('radio', 'wifi') else output

    monkeypatch.setattr(network, 'nmcli', run)
    result = network.NetworkManager().scan()['networks']
    assert len(result) == 6
    assert result[0]['ssid'] == 'Cafe: West\\Guest'
    assert result[0]['connected']
    assert next(item for item in result if item['ssid'] == 'Open')['signal'] == 90
    assert not next(item for item in result if item['ssid'] == 'Office')['supported']
    assert not next(item for item in result if item['ssid'] == 'Legacy')['supported']
    assert calls[-1][-2:] == ('--rescan', 'yes')


def test_nmcli_keeps_password_off_command_line_and_redacts_errors(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=4, stdout='secret-password', stderr='secret-password')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(network.NetworkError) as error:
        network.nmcli('device', 'wifi', 'connect', 'Cafe', password='secret-password')
    assert 'secret-password' not in captured['command']
    assert captured['input'] == 'secret-password\n'
    assert '--ask' in captured['command']
    assert 'secret-password' not in str(error.value)
    assert captured.get('shell', False) is False


@pytest.mark.parametrize('exception', [FileNotFoundError(), subprocess.TimeoutExpired('nmcli', 15)])
def test_command_failure_is_actionable(monkeypatch, exception):
    def fail(*args, **kwargs):
        raise exception
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(network.NetworkError):
        network.nmcli('device', 'status')


def test_wifi_connected_does_not_imply_internet_and_status_is_cached(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[-2:] == ('device', 'status'):
            return 'wlan0:wifi:connected:Saved profile'
        if args == ('radio', 'wifi'):
            return 'enabled'
        if 'show' in args:
            return '192.168.1.8/24\nfe80\\:\\:1/64'
        return '*:Actual SSID:70:WPA2:wlan0'

    monkeypatch.setattr(network, 'nmcli', run)
    monkeypatch.setattr(network, 'internet_status', lambda: 'offline')
    manager = network.NetworkManager()
    status = manager.status()
    assert status['wifi'][0]['ssid'] == 'Actual SSID'
    assert status['internet'] == 'offline'
    assert status['interfaces'][0]['addresses'] == ['192.168.1.8/24', 'fe80::1/64']
    count = len(calls)
    manager.reserve_connect('Next', 'wlan0')
    assert manager.status()['operation']['state'] == 'connecting'
    assert len(calls) == count


def test_missing_networkmanager_still_checks_internet(monkeypatch):
    def fail(*args, **kwargs):
        raise network.NetworkError('nmcli unavailable')
    monkeypatch.setattr(network, 'nmcli', fail)
    monkeypatch.setattr(network, 'internet_status', lambda: 'online')
    result = network.NetworkManager().status()
    assert not result['available']
    assert result['internet'] == 'online'
    assert result['error'] == 'nmcli unavailable'


@pytest.mark.parametrize('code,expected', [(204, 'online'), (200, 'limited'), (302, 'limited')])
def test_internet_probe_requires_https_204(monkeypatch, code, expected):
    class Connection:
        closed = False
        def __init__(self, host, timeout):
            assert host == 'connectivitycheck.gstatic.com'
            assert timeout == 4
        def request(self, method, path):
            assert (method, path) == ('GET', '/generate_204')
        def getresponse(self):
            return SimpleNamespace(status=code)
        def close(self):
            Connection.closed = True
    monkeypatch.setattr(network.http.client, 'HTTPSConnection', Connection)
    assert network.internet_status() == expected
    assert Connection.closed


def test_connect_failure_releases_lock_for_retry(monkeypatch):
    def fail(*args, **kwargs):
        raise network.NetworkError('Check password')
    monkeypatch.setattr(network, 'nmcli', fail)
    manager = network.NetworkManager()
    manager.reserve_connect('Cafe', 'wlan0')
    with pytest.raises(network.NetworkError, match='already in progress'):
        manager.reserve_connect('Other', 'wlan0')
    with pytest.raises(network.NetworkError, match='already in progress'):
        manager.scan()
    manager.connect('Cafe', 'wlan0', 'password')
    assert manager.operation()['state'] == 'failed'
    assert manager.reserve_connect('Other', 'wlan0')['state'] == 'connecting'


@pytest.mark.parametrize('payload', [
    {'ssid': '', 'interface': 'wlan0', 'password': 'secret'},
    {'ssid': 'é' * 17, 'interface': 'wlan0', 'password': 'secret'},
    {'ssid': 'Cafe', 'interface': 'wlan0;reboot', 'password': 'secret'},
    {'ssid': 'Cafe', 'interface': 'wlan0', 'password': 'secret\nnext-input'},
    {'ssid': 'Cafe', 'interface': 'wlan0', 'password': {'secret': True}},
    {'ssid': 'Cafe', 'password': 'secret'},
])
def test_connect_rejects_bad_input_without_echoing_password(payload):
    response = TestClient(web_app.app).post('/api/network/connect', json=payload)
    assert response.status_code == 422
    assert 'secret' not in response.text


@pytest.mark.parametrize('path', ['/api/network/scan', '/api/network/connect'])
def test_network_mutations_reject_cross_origin_and_form_requests(path):
    client = TestClient(web_app.app)
    assert client.post(path, json={}, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.post(path, data={'ssid': 'Cafe'}).status_code == 415


def test_connect_sends_accepted_response_before_starting_network_change(monkeypatch):
    manager = network.NetworkManager()
    monkeypatch.setattr(web_app, 'NETWORK', manager)
    calls = []
    monkeypatch.setattr(network, 'nmcli', lambda *args, **kwargs: calls.append(args) or '')
    events = []

    async def exercise():
        payload = json.dumps({'ssid': 'Cafe', 'interface': 'wlan0', 'password': 'secret'}).encode()
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/api/network/connect',
                 'raw_path': b'/api/network/connect', 'query_string': b'',
                 'headers': [(b'content-type', b'application/json'), (b'host', b'testserver')],
                 'server': ('testserver', 80), 'client': ('127.0.0.1', 1234)}
        async def receive():
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        async def send(message):
            events.append(message)
            assert not calls, 'Changing Wi-Fi must wait until the response has been sent'
        await web_app.app(scope, receive, send)

    import asyncio
    asyncio.run(exercise())
    assert events[0]['status'] == 202
    assert b'secret' not in events[1]['body']
    assert calls[-1] == ('device', 'wifi', 'connect', 'Cafe', 'ifname', 'wlan0')
    assert manager.operation()['state'] == 'connected'
