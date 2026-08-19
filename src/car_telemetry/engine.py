from __future__ import annotations

import signal
import threading

from .api_server import APIServer
from .common import write_json_atomic
from .config import settings
from .gps import worker as gps_worker
from .imu import worker as imu_worker
from .mqtt_client import worker as mqtt_worker
from .obd_service import OBDService
from .oled import worker as oled_worker
from .state import DeviceState
from .system_monitor import worker as system_worker


def run_engine(s=None):
    s = s or settings()
    stop = threading.Event()
    state = DeviceState(s.device_id, s.vehicle_id, s.prototype_stage)
    obd = OBDService(s, state)

    def shutdown(*_args):
        state.set('agent', 'shutting-down')
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    workers = [
        threading.Thread(target=gps_worker, args=(s, state, stop), daemon=True, name='gps'),
        threading.Thread(target=imu_worker, args=(s, state, stop), daemon=True, name='imu'),
        threading.Thread(target=oled_worker, args=(s, state, stop), daemon=True, name='oled'),
        threading.Thread(target=mqtt_worker, args=(s, state, stop), daemon=True, name='mqtt'),
        threading.Thread(target=obd.run, args=(stop,), daemon=True, name='obd'),
        threading.Thread(target=system_worker, args=(state, stop), daemon=True, name='system'),
        threading.Thread(
            target=APIServer(s, state, obd, stop).run,
            daemon=True,
            name='internal-api',
        ),
    ]

    for thread in workers:
        thread.start()

    state.set('agent', 'running')

    try:
        while not stop.is_set():
            write_json_atomic(s.status_file, state.snapshot())
            stop.wait(1.0)
    finally:
        stop.set()
        obd.reconnect()
        for thread in workers:
            thread.join(timeout=2)
        state.set('agent', 'stopped')
        write_json_atomic(s.status_file, state.snapshot())

    return 0


def main():
    raise SystemExit(run_engine())
