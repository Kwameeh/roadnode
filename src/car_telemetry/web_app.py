from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .common import read_json
from .config import settings
from .engine_client import EngineAPI

S = settings()
ENGINE = EngineAPI(S.api_host, S.api_port)
STATIC_DIR = Path(__file__).with_name('web_static')


class TelemetryHub:
    """Poll the in-memory engine once and fan the newest state out to all browsers."""

    def __init__(self, engine: EngineAPI, status_file: str, interval: float):
        self.engine = engine
        self.status_file = status_file
        self.interval = max(0.1, interval)
        self.latest: dict[str, Any] | None = None
        self.latest_payload: str | None = None
        self.revision = 0
        self.last_engine_update = 0.0
        self.last_engine_update_at: float | None = None
        self.engine_error: str | None = None
        self._condition = asyncio.Condition()
        self._task: asyncio.Task | None = None

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name='web-telemetry-hub')

    async def stop(self):
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _fetch(self) -> dict[str, Any]:
        try:
            data = await asyncio.to_thread(self.engine.get, '/state')
            self.last_engine_update = time.monotonic()
            self.last_engine_update_at = time.time()
            self.engine_error = None
            return {
                **data,
                '_web': {
                    'engineConnected': True,
                    'source': 'engine',
                    'lastEngineUpdateAt': self.last_engine_update_at,
                    'error': None,
                },
            }
        except Exception as exc:
            self.engine_error = str(exc)
            fallback = await asyncio.to_thread(read_json, self.status_file)
            return {
                **(fallback or {'agent': 'starting'}),
                '_web': {
                    'engineConnected': False,
                    'source': 'status-file' if fallback else 'starting',
                    'lastEngineUpdateAt': self.last_engine_update_at,
                    'error': self.engine_error,
                },
            }

    async def _run(self):
        while True:
            data = await self._fetch()
            payload = json.dumps(data, separators=(',', ':'), default=str)
            if payload != self.latest_payload:
                async with self._condition:
                    self.latest = data
                    self.latest_payload = payload
                    self.revision += 1
                    self._condition.notify_all()
            await asyncio.sleep(self.interval)

    async def next_payload(self, revision: int, heartbeat: float) -> tuple[str, int]:
        async with self._condition:
            if self.latest_payload is None:
                await self._condition.wait_for(lambda: self.latest_payload is not None)
            elif revision == self.revision:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: revision != self.revision),
                        timeout=max(1.0, heartbeat),
                    )
            return self.latest_payload or '{"agent":"starting"}', self.revision


HUB = TelemetryHub(ENGINE, S.status_file, S.web_state_refresh_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await HUB.start()
    try:
        yield
    finally:
        await HUB.stop()


app = FastAPI(title='Car Telemetry', docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


@app.get('/')
def index():
    return FileResponse(STATIC_DIR / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/api/state')
async def state():
    if HUB.latest is not None:
        return HUB.latest
    return await HUB._fetch()


@app.get('/api/web-config')
def web_config():
    return {
        'heartbeatSeconds': S.web_heartbeat_seconds,
        'fallbackPollSeconds': S.web_fallback_poll_seconds,
    }


@app.get('/api/signals')
def signals():
    try:
        return ENGINE.get('/signals')
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@app.get('/api/vehicle')
def vehicle():
    try:
        return ENGINE.get('/vehicle')
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@app.get('/api/dtc')
def dtc():
    try:
        return ENGINE.get('/dtc')
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@app.get('/api/obd/ports')
def obd_ports():
    try:
        return ENGINE.get('/obd/ports')
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@app.get('/api/bluetooth/status')
def bluetooth_status():
    try:
        return ENGINE.get('/bluetooth/status')
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


def proxy_post(path: str, payload: dict):
    try:
        return ENGINE.post(path, payload)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.post('/api/signals/select')
def signal_select(payload: dict = Body(default_factory=dict)):
    return proxy_post('/signals/select', payload)


@app.post('/api/obd/reconnect')
def obd_reconnect(payload: dict = Body(default_factory=dict)):
    return proxy_post('/obd/reconnect', payload)


@app.post('/api/obd/transport')
def obd_transport(payload: dict = Body(default_factory=dict)):
    return proxy_post('/obd/transport', payload)


@app.post('/api/dtc/refresh')
def dtc_refresh(payload: dict = Body(default_factory=dict)):
    return proxy_post('/dtc/refresh', payload)


@app.post('/api/dtc/clear')
def dtc_clear(payload: dict = Body(default_factory=dict)):
    return proxy_post('/dtc/clear', payload)


@app.post('/api/bluetooth/scan')
def bluetooth_scan(payload: dict = Body(default_factory=dict)):
    return proxy_post('/bluetooth/scan', payload)


@app.post('/api/bluetooth/pair')
def bluetooth_pair(payload: dict = Body(default_factory=dict)):
    return proxy_post('/bluetooth/pair', payload)


@app.post('/api/bluetooth/disconnect')
def bluetooth_disconnect(payload: dict = Body(default_factory=dict)):
    return proxy_post('/bluetooth/disconnect', payload)


@app.post('/api/bluetooth/forget')
def bluetooth_forget(payload: dict = Body(default_factory=dict)):
    return proxy_post('/bluetooth/forget', payload)


@app.post('/api/bluetooth/use-elm')
def bluetooth_use_elm(payload: dict = Body(default_factory=dict)):
    return proxy_post('/bluetooth/use-elm', payload)


@app.websocket('/ws/telemetry')
async def telemetry_socket(websocket: WebSocket):
    await websocket.accept()
    revision = 0
    try:
        while True:
            payload, revision = await HUB.next_payload(revision, S.web_heartbeat_seconds)
            await websocket.send_text(payload)
    except (WebSocketDisconnect, RuntimeError):
        return


def main():
    if not S.web_enabled:
        return
    uvicorn.run(app, host=S.web_host, port=S.web_port, log_level='info')
