from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from .state import DeviceState

MB = 1024 * 1024


@dataclass
class LoopStats:
    name: str
    interval: float
    samples: int = 0
    deadline_misses: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def record(self, elapsed: float, missed: bool) -> None:
        self.samples += 1
        if missed:
            self.deadline_misses += 1
        if len(self.latencies_ms) < 20_000:
            self.latencies_ms.append(elapsed * 1000.0)

    def summary(self) -> dict:
        values = self.latencies_ms or [0.0]
        ordered = sorted(values)
        p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        miss = self.deadline_misses * 100.0 / self.samples if self.samples else 0.0
        return {
            'samples': self.samples,
            'deadlineMisses': self.deadline_misses,
            'deadlineMissPercent': round(miss, 3),
            'meanWorkMs': round(statistics.fmean(values), 3),
            'p95WorkMs': round(p95, 3),
            'maxWorkMs': round(max(values), 3),
            'targetIntervalMs': round(self.interval * 1000.0, 3),
        }


def _proc_rss_mb() -> float:
    try:
        pages = int(Path('/proc/self/statm').read_text().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE') / MB
    except Exception:
        return 0.0


def _meminfo() -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        for raw in Path('/proc/meminfo').read_text().splitlines():
            key, rest = raw.split(':', 1)
            result[key] = float(rest.strip().split()[0]) / 1024.0
    except Exception:
        pass
    return result


def _cpu_snapshot() -> tuple[int, int] | None:
    try:
        fields = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle
    except Exception:
        return None


def _cpu_percent(before, after) -> float | None:
    if not before or not after:
        return None
    total = after[0] - before[0]
    idle = after[1] - before[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, (total - idle) * 100.0 / total))


def _temperature_c() -> float | None:
    for path in (Path('/sys/class/thermal/thermal_zone0/temp'), Path('/sys/devices/virtual/thermal/thermal_zone0/temp')):
        try:
            return float(path.read_text().strip()) / 1000.0
        except Exception:
            pass
    return None


def _throttled() -> str | None:
    try:
        process = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=2, check=False)
        return process.stdout.strip() or None
    except Exception:
        return None


def _load_project_libraries() -> dict[str, str]:
    modules = {
        'python-obd': 'obd',
        'pyserial': 'serial',
        'pynmea2': 'pynmea2',
        'paho-mqtt': 'paho.mqtt.client',
        'FastAPI': 'fastapi',
        'Uvicorn': 'uvicorn',
        'pexpect': 'pexpect',
        'Pillow': 'PIL.Image',
    }
    result = {}
    for label, module in modules.items():
        try:
            __import__(module)
            result[label] = 'loaded'
        except Exception as exc:
            result[label] = f'unavailable: {exc}'
    return result


def quantity(value: float, unit: str) -> dict:
    return {'value': round(value, 3), 'unit': unit}


class SyntheticWorkload:
    def __init__(self, stress: float, reserve_mb: int, web_clients: int, status_path: Path):
        self.stress = max(0.25, stress)
        self.reserve_mb = max(0, reserve_mb)
        self.web_clients = max(0, web_clients)
        self.status_path = status_path
        self.stop = threading.Event()
        self.state = DeviceState('BENCH-001', 'SIMULATED-VEHICLE', 1)
        self.stats: dict[str, LoopStats] = {}
        self.events = deque(maxlen=500)
        self.reserve: bytearray | None = None
        self.signal_names = [
            'RPM', 'SPEED', 'COOLANT_TEMP', 'ENGINE_LOAD', 'THROTTLE_POS',
            'CONTROL_MODULE_VOLTAGE', 'FUEL_LEVEL', 'INTAKE_TEMP', 'MAF',
            'OIL_TEMP', 'FUEL_RATE', 'TIMING_ADVANCE', 'BAROMETRIC_PRESSURE',
            'SHORT_FUEL_TRIM_1', 'LONG_FUEL_TRIM_1',
        ]
        supported = [
            {'name': name, 'description': name.replace('_', ' ').title(), 'mode': 1}
            for name in self.signal_names
        ]
        self.state.merge('obd', {
            'enabled': True,
            'connected': True,
            'transport': 'simulated-usb',
            'protocolName': 'ISO 15765-4 CAN (simulated)',
            'vehicleProfileKey': 'BENCHMARKVIN1234567',
            'vehicle': {'VIN': 'BENCHMARKVIN1234567', 'protocolName': 'ISO 15765-4 CAN (simulated)'},
            'supportedSignals': supported,
            'supportedCount': len(supported),
            'coreSignals': self.signal_names[:9],
            'selectedSignals': self.signal_names,
            'signals': {},
            'dtc': {'stored': [], 'currentCycle': [], 'storedCount': 0, 'currentCycleCount': 0},
            'dtcEvents': [],
        })
        self.state.merge('gps', {'enabled': True, 'received': True, 'validFix': True})
        self.state.merge('imu', {'enabled': True, 'calibrated': True})
        self.state.merge('mqtt', {'enabled': True, 'connected': True})
        self.state.merge('system', {'hostname': 'benchmark-pi', 'ipAddress': '192.168.1.50'})

    def allocate_reserve(self):
        if self.reserve_mb:
            self.reserve = bytearray(self.reserve_mb * MB)
            for index in range(0, len(self.reserve), MB):
                self.reserve[index] = (index // MB) % 251

    def _loop(self, name: str, hz: float, work: Callable[[int], None]):
        interval = 1.0 / max(0.1, hz * self.stress)
        stats = LoopStats(name, interval)
        self.stats[name] = stats
        deadline = time.perf_counter()
        counter = 0
        while not self.stop.is_set():
            deadline += interval
            started = time.perf_counter()
            try:
                work(counter)
            except Exception as exc:
                self.events.append({'worker': name, 'error': str(exc), 'time': time.time()})
            elapsed = time.perf_counter() - started
            remaining = deadline - time.perf_counter()
            stats.record(elapsed, remaining < 0)
            counter += 1
            if remaining > 0:
                self.stop.wait(remaining)
            elif -remaining > interval * 4:
                deadline = time.perf_counter()

    def imu_work(self, n: int):
        t = n / 20.0
        ax, ay, az = math.sin(t) * 0.8, math.sin(t * 0.43) * 0.5, math.cos(t * 0.17) * 0.12
        resultant = math.sqrt((9.80665 + az) ** 2 + ax ** 2 + ay ** 2) / 9.80665
        self.state.merge('imu', {
            'linearAccelerationMps2': {'x': ax, 'y': ay, 'z': az},
            'gyroRadPerSec': {'x': ay / 8, 'y': ax / 8, 'z': math.sin(t) / 10},
            'resultantG': resultant,
            'temperatureC': 31.5,
        })

    def gps_work(self, n: int):
        self.state.merge('gps', {
            'latitude': 5.6037 + math.sin(n / 50) * 0.001,
            'longitude': -0.1870 + math.cos(n / 50) * 0.001,
            'speedKph': 45 + n % 25,
            'headingDegrees': (n * 4) % 360,
            'satellites': 8 + n % 4,
            'hdop': 1.1,
            'lastDataUnix': time.time(),
        })

    def obd_work(self, n: int):
        name = self.signal_names[n % len(self.signal_names)]
        phase = n / 13.0
        values = {
            'RPM': quantity(900 + abs(math.sin(phase)) * 3400, 'rpm'),
            'SPEED': quantity(20 + abs(math.sin(phase / 3)) * 100, 'km/h'),
            'COOLANT_TEMP': quantity(82 + abs(math.sin(phase / 20)) * 12, 'degC'),
            'ENGINE_LOAD': quantity(20 + abs(math.sin(phase)) * 65, 'percent'),
            'THROTTLE_POS': quantity(8 + abs(math.sin(phase * 1.4)) * 75, 'percent'),
            'CONTROL_MODULE_VOLTAGE': quantity(13.7 + math.sin(phase / 8) * 0.4, 'V'),
            'FUEL_LEVEL': quantity(62.0, 'percent'),
            'INTAKE_TEMP': quantity(34.0, 'degC'),
            'MAF': quantity(3 + abs(math.sin(phase)) * 42, 'g/s'),
            'OIL_TEMP': quantity(90.0, 'degC'),
            'FUEL_RATE': quantity(5.4, 'L/h'),
            'TIMING_ADVANCE': quantity(11.0, 'degree'),
            'BAROMETRIC_PRESSURE': quantity(101.0, 'kPa'),
            'SHORT_FUEL_TRIM_1': quantity(1.4, 'percent'),
            'LONG_FUEL_TRIM_1': quantity(-0.9, 'percent'),
        }
        self.state.merge_nested('obd', 'signals', {name: {'value': values[name], 'updatedAt': time.time()}})

    def dtc_work(self, n: int):
        has_code = n % 2 == 1
        stored = [{'code': 'P0420', 'description': 'Catalyst System Efficiency Below Threshold'}] if has_code else []
        events = self.state.snapshot().get('obd', {}).get('dtcEvents', [])[-50:]
        events.append({'seq': n + 1, 'timestamp': time.time(), 'event': 'DTC_ADDED' if has_code else 'DTC_REMOVED', 'scope': 'stored', 'code': 'P0420'})
        self.state.merge('obd', {'dtc': {'stored': stored, 'currentCycle': [], 'storedCount': len(stored), 'currentCycleCount': 0, 'lastScanAt': time.time()}, 'dtcEvents': events})

    def mqtt_work(self, _n: int):
        snap = self.state.snapshot()
        raw = json.dumps({'messageType': 'TELEMETRY', 'gps': snap['gps'], 'imu': snap['imu'], 'obd': snap['obd'], 'events': snap['events']}, separators=(',', ':'), default=str).encode()
        framed = b'MQTT' + len(raw).to_bytes(4, 'big') + raw
        self.state.merge('mqtt', {'lastPublishOk': True, 'lastPayloadBytes': len(framed), 'lastPublishAt': time.time()})

    def status_work(self, _n: int):
        data = json.dumps(self.state.snapshot(), separators=(',', ':'), default=str)
        temp = self.status_path.with_suffix('.tmp')
        temp.write_text(data, encoding='utf-8')
        temp.replace(self.status_path)

    def oled_work(self, _n: int):
        image = Image.new('1', (128, 64), 0)
        draw = ImageDraw.Draw(image)
        draw.text((0, 0), 'CAR TELEMETRY', fill=1)
        image.tobytes()

    def web_work(self, _n: int):
        snapshot = self.state.snapshot()
        base = json.dumps(snapshot, separators=(',', ':'), default=str).encode()
        # Approximate a state endpoint plus one WebSocket frame per connected browser.
        for client in range(self.web_clients):
            envelope = b'WS' + client.to_bytes(2, 'big', signed=False) + len(base).to_bytes(4, 'big') + base
            if not envelope:
                raise RuntimeError('empty web frame')

    def event_log_work(self, n: int):
        if n % 5 == 0:
            self.events.append({'timestamp': time.time(), 'event': random.choice(['normal', 'normal', 'harshBrakeTest'])})
        json.dumps(list(self.events), separators=(',', ':'))

    def start(self):
        workers = [
            ('imu', 20.0, self.imu_work),
            ('gps', 1.0, self.gps_work),
            ('obd', 12.0, self.obd_work),
            ('dtc-scan', 1 / 30.0, self.dtc_work),
            ('mqtt', 0.5, self.mqtt_work),
            ('status-file', 1.0, self.status_work),
            ('oled', 2.0, self.oled_work),
            ('web-stream', 2.0, self.web_work),
            ('event-log', 2.0, self.event_log_work),
        ]
        threads = []
        for name, hz, func in workers:
            thread = threading.Thread(target=self._loop, args=(name, hz, func), daemon=True, name=f'bench-{name}')
            thread.start()
            threads.append(thread)
        return threads


def _grade(report: dict) -> tuple[str, list[str]]:
    metrics = report['metrics']
    workers = report['workers']
    reasons: list[str] = []
    fail = warn = False

    available = metrics.get('minSystemAvailableMb')
    cpu = metrics.get('averageSystemCpuPercent')
    growth = metrics.get('processRssGrowthMb', 0.0)
    misses = max((item['deadlineMissPercent'] for item in workers.values()), default=0.0)
    web_p95 = workers.get('web-stream', {}).get('p95WorkMs', 0.0)
    throttled = str(metrics.get('throttledEnd') or '')

    if available is not None:
        if available < 50:
            fail = True; reasons.append(f'available RAM fell below 50 MB ({available:.1f} MB)')
        elif available < 80:
            warn = True; reasons.append(f'available RAM fell below preferred 80 MB ({available:.1f} MB)')
    if cpu is not None:
        if cpu >= 95:
            fail = True; reasons.append(f'average system CPU reached {cpu:.1f}%')
        elif cpu >= 80:
            warn = True; reasons.append(f'average system CPU is high ({cpu:.1f}%)')
    if misses >= 15:
        fail = True; reasons.append(f'worker deadline misses reached {misses:.1f}%')
    elif misses >= 5:
        warn = True; reasons.append(f'worker deadline misses reached {misses:.1f}%')
    if web_p95 >= 400:
        fail = True; reasons.append(f'web stream p95 work took {web_p95:.1f} ms')
    elif web_p95 >= 200:
        warn = True; reasons.append(f'web stream p95 work took {web_p95:.1f} ms')
    if growth >= 100:
        fail = True; reasons.append(f'benchmark RSS grew by {growth:.1f} MB')
    elif growth >= 50:
        warn = True; reasons.append(f'benchmark RSS grew by {growth:.1f} MB')
    if throttled and throttled != 'throttled=0x0':
        warn = True; reasons.append(f'Pi reported power/thermal flags: {throttled}')

    if fail:
        return 'FAIL', reasons
    if warn:
        return 'WARN', reasons
    return 'PASS', ['CPU, RAM, worker scheduling and web-stream workload stayed inside benchmark targets']


def run_benchmark(seconds: int = 120, stress: float = 1.0, reserve_mb: int = 48, web_clients: int = 5, output: str | None = None) -> dict:
    seconds = max(10, int(seconds))
    stress = max(0.25, float(stress))
    reserve_mb = max(0, int(reserve_mb))
    web_clients = max(0, int(web_clients))
    libraries = _load_project_libraries()

    with tempfile.TemporaryDirectory(prefix='car-telemetry-benchmark-') as temp_dir:
        workload = SyntheticWorkload(stress, reserve_mb, web_clients, Path(temp_dir) / 'status.json')
        mem_before = _meminfo(); rss_before = _proc_rss_mb(); temp_before = _temperature_c(); throttle_before = _throttled()
        process_cpu_before = time.process_time(); wall_before = time.perf_counter()
        workload.allocate_reserve(); threads = workload.start()
        rss_peak = _proc_rss_mb(); available_samples=[]; cpu_samples=[]; temp_samples=[]; previous_cpu=_cpu_snapshot()

        print('Car Telemetry Headless Web Full-Load Benchmark')
        print('=' * 68)
        print(f'Duration: {seconds}s | Stress: {stress:.2f}x | RAM reserve: {reserve_mb} MB | Web clients: {web_clients}')
        print('Simulating GPS + 20 Hz IMU + python-OBD callbacks + DTC/VIN state + MQTT +')
        print('           status writes + OLED + local web/API/WebSocket clients')
        print('Press Ctrl+C to stop early.\n')

        started=time.monotonic(); next_report=started; interrupted=False
        try:
            while time.monotonic()-started < seconds:
                time.sleep(1); rss_peak=max(rss_peak,_proc_rss_mb()); mem=_meminfo()
                if 'MemAvailable' in mem: available_samples.append(mem['MemAvailable'])
                current=_cpu_snapshot(); cpu_value=_cpu_percent(previous_cpu,current); previous_cpu=current
                if cpu_value is not None: cpu_samples.append(cpu_value)
                temp=_temperature_c()
                if temp is not None: temp_samples.append(temp)
                now=time.monotonic()
                if now>=next_report:
                    elapsed=min(seconds,int(now-started)); avail=mem.get('MemAvailable'); avail_text=f'{avail:.0f} MB' if avail is not None else 'n/a'; cpu_text=f'{cpu_value:.0f}%' if cpu_value is not None else 'n/a'
                    temp_text=f' | temp {temp:.1f}C' if temp is not None else ''
                    print(f'[{elapsed:>4}/{seconds}s] RSS {_proc_rss_mb():6.1f} MB | available {avail_text:>8} | CPU {cpu_text:>4}{temp_text}')
                    next_report=now+10
        except KeyboardInterrupt:
            interrupted=True; print('\nBenchmark interrupted; producing partial results.')
        finally:
            workload.stop.set()
            for thread in threads: thread.join(timeout=2)

        wall_elapsed=max(0.001,time.perf_counter()-wall_before); process_cpu_elapsed=time.process_time()-process_cpu_before
        rss_end=_proc_rss_mb(); mem_end=_meminfo(); temp_end=_temperature_c(); throttle_end=_throttled()
        report={
            'benchmark':'car-telemetry-headless-web-full-load', 'interrupted':interrupted,
            'durationSeconds':round(wall_elapsed,3),'stressMultiplier':stress,'ramReserveMb':reserve_mb,'webClients':web_clients,'libraries':libraries,
            'metrics':{
                'systemMemTotalMb':round(mem_end.get('MemTotal',mem_before.get('MemTotal',0.0)),1) if (mem_end or mem_before) else None,
                'systemAvailableStartMb':round(mem_before.get('MemAvailable',0.0),1) if 'MemAvailable' in mem_before else None,
                'minSystemAvailableMb':round(min(available_samples),1) if available_samples else None,
                'systemAvailableEndMb':round(mem_end.get('MemAvailable',0.0),1) if 'MemAvailable' in mem_end else None,
                'processRssStartMb':round(rss_before,1),'processRssPeakMb':round(rss_peak,1),'processRssEndMb':round(rss_end,1),'processRssGrowthMb':round(max(0.0,rss_end-rss_before),1),
                'processCpuOneCoreEquivalentPercent':round(process_cpu_elapsed*100.0/wall_elapsed,1),
                'averageSystemCpuPercent':round(statistics.fmean(cpu_samples),1) if cpu_samples else None,'peakSystemCpuPercent':round(max(cpu_samples),1) if cpu_samples else None,
                'temperatureStartC':round(temp_before,1) if temp_before is not None else None,'temperaturePeakC':round(max(temp_samples),1) if temp_samples else temp_end,'temperatureEndC':round(temp_end,1) if temp_end is not None else None,
                'throttledStart':throttle_before,'throttledEnd':throttle_end,'loadAverage':list(os.getloadavg()) if hasattr(os,'getloadavg') else None,'cpuCount':os.cpu_count(),
            },
            'workers':{name:stats.summary() for name,stats in workload.stats.items()},
        }
        grade,reasons=_grade(report); report['result']=grade; report['reasons']=reasons
        if output:
            path=Path(output).expanduser(); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(report,indent=2),encoding='utf-8'); report['outputFile']=str(path)

        print('\n'+'='*68); print(f'RESULT: {grade}')
        for reason in reasons: print(' -',reason)
        metrics=report['metrics']; print(f"Peak benchmark RSS: {metrics['processRssPeakMb']} MB")
        if metrics['minSystemAvailableMb'] is not None: print(f"Minimum system available RAM: {metrics['minSystemAvailableMb']} MB")
        if metrics['averageSystemCpuPercent'] is not None: print(f"Average system CPU: {metrics['averageSystemCpuPercent']}%")
        print(f"Web stream p95 work: {report['workers'].get('web-stream',{}).get('p95WorkMs',0)} ms")
        print(f"Worst worker deadline-miss rate: {max((w['deadlineMissPercent'] for w in report['workers'].values()),default=0):.2f}%")
        if output: print(f'JSON report: {Path(output).expanduser()}')
        print('='*68)
        return report


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description='Simulate the complete headless telemetry + LAN web workload.')
    parser.add_argument('--seconds',type=int,default=120)
    parser.add_argument('--stress',type=float,default=1.0)
    parser.add_argument('--reserve-mb',type=int,default=48)
    parser.add_argument('--web-clients',type=int,default=5)
    parser.add_argument('--output',default='benchmark-report.json')
    args=parser.parse_args(argv)
    report=run_benchmark(args.seconds,args.stress,args.reserve_mb,args.web_clients,args.output or None)
    return 0 if report['result'] in {'PASS','WARN'} else 1


if __name__=='__main__':
    raise SystemExit(main())
