import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from car_telemetry.api_server import APIServer
from car_telemetry.config import settings
from car_telemetry.state import DeviceState


@pytest.fixture
def api():
    state = DeviceState('PROTO-001', 'VEH-001', 1)
    stop = threading.Event()
    server = APIServer(replace(settings(), api_port=0, oled_enabled=True, web_port=8080), state, None, stop)
    ready = threading.Event()
    original = server.handler

    def run():
        from http.server import ThreadingHTTPServer

        server.server = ThreadingHTTPServer(('127.0.0.1', 0), original())
        server.server.timeout = 0.1
        ready.set()
        while not stop.is_set():
            server.server.handle_request()
        server.server.server_close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(2)

    def post(path, body):
        request = urllib.request.Request(
            f'http://127.0.0.1:{server.server.server_address[1]}{path}',
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    yield state, post
    stop.set()
    thread.join(timeout=2)


def test_qr_request_is_refused_without_a_network_address(api):
    state, post = api
    status, body = post('/oled/qr', {'seconds': 60})
    assert status == 409
    assert 'no network address' in body['error']
    assert 'qrUntil' not in state.snapshot().get('oled', {})


def test_qr_request_shows_the_web_app_link_for_a_bounded_time(api):
    state, post = api
    state.merge('system', {'ipAddress': '192.168.1.42'})
    before = time.time()
    status, body = post('/oled/qr', {'seconds': 9999})
    assert status == 200
    assert body['url'] == 'http://192.168.1.42:8080'
    assert body['seconds'] == 600
    assert before + 599 <= state.snapshot()['oled']['qrUntil'] <= time.time() + 600
