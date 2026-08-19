import asyncio
import json

from fastapi.testclient import TestClient

from car_telemetry import web_app
from car_telemetry.web_app import TelemetryHub, app


class FakeEngine:
    def __init__(self):
        self.value = 0
        self.calls = 0

    def get(self, path):
        assert path == '/state'
        self.calls += 1
        return {'agent': 'running', 'value': self.value}


def test_hub_polls_once_and_delivers_changed_state(tmp_path):
    async def exercise():
        engine = FakeEngine()
        hub = TelemetryHub(engine, str(tmp_path / 'missing.json'), 0.1)
        await hub.start()
        try:
            first, revision = await asyncio.wait_for(hub.next_payload(0, 1), timeout=1)
            assert json.loads(first)['value'] == 0
            engine.value = 1
            second, next_revision = await asyncio.wait_for(
                hub.next_payload(revision, 1), timeout=1
            )
            assert json.loads(second)['value'] == 1
            assert next_revision > revision
            assert engine.calls >= 2
        finally:
            await hub.stop()

    asyncio.run(exercise())


def test_hub_falls_back_to_status_file(tmp_path):
    class BrokenEngine:
        def get(self, _path):
            raise RuntimeError('engine unavailable')

    status = tmp_path / 'status.json'
    status.write_text('{"agent":"fallback","value":42}', encoding='utf-8')

    async def exercise():
        hub = TelemetryHub(BrokenEngine(), str(status), 0.1)
        payload = await hub._fetch()
        assert payload['agent'] == 'fallback'
        assert payload['value'] == 42
        assert payload['_web'] == {
            'engineConnected': False,
            'source': 'status-file',
            'lastEngineUpdateAt': None,
            'error': 'engine unavailable',
        }
        assert hub.engine_error == 'engine unavailable'

    asyncio.run(exercise())


def test_websocket_delivers_state_without_browser_polling():
    with TestClient(app) as client:
        with client.websocket_connect('/ws/telemetry') as websocket:
            payload = websocket.receive_json()

    assert isinstance(payload, dict)
    assert payload.get('agent')


def test_websocket_pushes_changed_state_on_same_connection(monkeypatch, tmp_path):
    engine = FakeEngine()
    hub = TelemetryHub(engine, str(tmp_path / 'missing.json'), 0.1)
    monkeypatch.setattr(web_app, 'HUB', hub)

    with TestClient(app) as client:
        with client.websocket_connect('/ws/telemetry') as websocket:
            first = websocket.receive_json()
            engine.value = 9
            for _ in range(5):
                second = websocket.receive_json()
                if second['value'] == 9:
                    break

    assert first['value'] == 0
    assert second['value'] == 9
    assert second['_web']['engineConnected'] is True
