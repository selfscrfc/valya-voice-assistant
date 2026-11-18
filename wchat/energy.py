"""Local energy observations, not per-app watt estimates. Never logs conversation content."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import subprocess
import threading
import time


def battery_sample():
    values = plistlib.loads(subprocess.check_output(
        ['/usr/sbin/ioreg', '-r', '-c', 'AppleSmartBattery', '-a'], timeout=5))
    if not values:
        return {}
    b = values[0]
    current = b.get('InstantAmperage', b.get('Amperage'))
    if isinstance(current, int) and current >= 2**63:
        current -= 2**64
    voltage = b.get('Voltage')
    watts = -current * voltage / 1e6 if isinstance(current, (int, float)) and voltage else None
    on_battery = not b.get('ExternalConnected', True) and not b.get('IsCharging', False)
    return {'battery_percent': b.get('CurrentCapacity'), 'on_battery': on_battery,
            'battery_discharge_w': round(max(watts, 0), 3) if on_battery and watts is not None else None,
            'voltage_mv': voltage, 'current_ma': current,
            'charge_mah': b.get('AppleRawCurrentCapacity'), 'full_charge_mah': b.get('AppleRawMaxCapacity')}


def cpu_seconds(value):
    days, _, value = value.rpartition('-')
    parts = [float(p) for p in value.split(':')]
    return (int(days) * 86400 if days else 0) + sum(n * 60**i for i, n in enumerate(reversed(parts)))


def process_sample(root_pid):
    with subprocess.Popen(['/bin/ps', '-axo', 'pid=,ppid=,time=,rss='], stdout=subprocess.PIPE, text=True) as process:
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        measuring_pid = process.pid
    rows = {}
    for line in output.splitlines():
        pid, ppid, cpu, rss = line.split()
        rows[int(pid)] = (int(ppid), cpu_seconds(cpu), int(rss))
    selected = {root_pid} if root_pid in rows else set()
    while True:
        children = {pid for pid, (parent, _, _) in rows.items() if parent in selected}
        extra = children - selected
        if not extra:
            break
        selected |= extra
    # Exclude the measuring ps process; other app children (Codex and speech) are included.
    selected.discard(measuring_pid)
    return {pid: rows[pid][1] for pid in selected}, sum(rows[pid][2] for pid in selected) / 1024


class EnergyLog:
    def __init__(self, directory, label, interval=60, root_pid=None):
        if interval < 10:
            raise ValueError('Energy sampling interval must be at least 10 seconds')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.label, self.interval = label, interval
        self.root_pid = root_pid or os.getpid()
        self.run_id = f'{os.getpid()}-{time.time_ns()}'
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.previous_cpu, self.previous_time = None, None
        self.thread = None
        self.failed = False

    def record(self, event, **values):
        if self.failed:
            return
        now = datetime.now(timezone.utc)
        item = {'timestamp': now.isoformat(), 'event': event, 'mode': self.label,
                'run': self.run_id, **values}
        try:
            with self.lock:
                path = self.directory / (now.strftime('%Y-%m-%d') + '.jsonl')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a') as out:
                    out.write(json.dumps(item, ensure_ascii=False) + '\n')
        except OSError:
            # Diagnostics must never interrupt a command or fill the event stream with errors.
            self.failed = True

    def sample(self):
        now = time.monotonic()
        values = {}
        try:
            values.update(battery_sample())
        except (OSError, ValueError, subprocess.SubprocessError):
            values['battery_unavailable'] = True
        try:
            cpu, rss = process_sample(self.root_pid)
            values['app_rss_mb'] = round(rss, 1)
            values['app_processes'] = len(cpu)
            if self.previous_cpu is not None:
                seconds = sum(max(0, value - self.previous_cpu.get(pid, 0)) for pid, value in cpu.items())
                values['app_cpu_percent'] = round(100 * seconds / max(now - self.previous_time, .001), 2)
                values['sample_seconds'] = round(now - self.previous_time, 3)
            self.previous_cpu, self.previous_time = cpu, now
        except (OSError, ValueError, subprocess.SubprocessError):
            values['process_metrics_unavailable'] = True
        self.record('energy_sample', **values)

    def start(self):
        self.record('run_started', root_pid=self.root_pid, interval_seconds=self.interval)
        def sample_loop():
            while not self.stop_event.is_set():
                self.sample()
                self.stop_event.wait(self.interval)
        self.thread = threading.Thread(target=sample_loop, name='energy-log', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(6)
        self.record('run_finished')
