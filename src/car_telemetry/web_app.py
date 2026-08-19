from __future__ import annotations

import asyncio
import json
from pathlib import Path

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

app = FastAPI(title='Car Telemetry', docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


@app.get('/')
def index():
    return FileResponse(STATIC_DIR / 'index.html')


@app.get('/api/state')
def state():
    return read_json(S.status_file) or {'agent': 'starting'}


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
    last_payload = None
    try:
        while True:
            data = read_json(S.status_file) or {'agent': 'starting'}
            payload = json.dumps(data, separators=(',', ':'), default=str)
            if payload != last_payload:
                await websocket.send_text(payload)
                last_payload = payload
            await asyncio.sleep(max(0.2, S.web_state_refresh_seconds))
    except WebSocketDisconnect:
        return


def main():
    if not S.web_enabled:
        return
    uvicorn.run(app, host=S.web_host, port=S.web_port, log_level='info')
