from __future__ import annotations

import json
from urllib import error, request


class EngineAPI:
    def __init__(self, host: str, port: int):
        self.base = f"http://{host}:{port}"

    def get(self, path: str):
        try:
            with request.urlopen(self.base + path, timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except error.HTTPError as exc:
            payload = exc.read().decode('utf-8', errors='replace')
            try:
                detail = json.loads(payload)
            except Exception:
                detail = {'error': payload or str(exc)}
            raise RuntimeError(detail.get('error') or str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"Telemetry engine API unavailable: {exc}") from exc

    def post(self, path: str, payload: dict | None = None):
        raw = json.dumps(payload or {}).encode('utf-8')
        req = request.Request(
            self.base + path,
            data=raw,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=45) as response:
                return json.loads(response.read().decode('utf-8'))
        except error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            try:
                detail = json.loads(body)
            except Exception:
                detail = {'error': body or str(exc)}
            raise RuntimeError(detail.get('error') or str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"Telemetry engine API unavailable: {exc}") from exc
