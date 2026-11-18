#!/usr/bin/env python3
"""Record comparison windows or summarize the app's private JSONL metrics."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import statistics
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wchat.energy import EnergyLog

DEFAULT = Path.home() / 'Library/Application Support/wchat/metrics'


def report(directory, hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    groups = defaultdict(list)
    for path in sorted(directory.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            try:
                item = json.loads(line)
                if datetime.fromisoformat(item['timestamp']) >= cutoff:
                    groups[item['mode']].append(item)
            except (ValueError, KeyError):
                continue  # A concurrent final line may still be incomplete.
    result = {}
    for mode, events in groups.items():
        samples = [e for e in events if e['event'] == 'energy_sample']
        watts = [e['battery_discharge_w'] for e in samples if e.get('battery_discharge_w') is not None]
        cpu = [e['app_cpu_percent'] for e in samples if 'app_cpu_percent' in e]
        rss = [e['app_rss_mb'] for e in samples if 'app_rss_mb' in e]
        transcriptions = [e['seconds'] for e in events if e['event'] == 'transcription_finished']
        result[mode] = {
            'samples': len(samples), 'battery_samples': len(watts),
            'device_discharge_w_mean': round(statistics.mean(watts), 3) if watts else None,
            'app_cpu_percent_mean': round(statistics.mean(cpu), 2) if cpu else None,
            'app_rss_mb_max': max(rss) if rss else None,
            'transcriptions': len(transcriptions),
            'transcription_seconds_total': round(sum(transcriptions), 3),
            'transcription_seconds_median': round(statistics.median(transcriptions), 3) if transcriptions else None,
            'transcription_seconds_max': max(transcriptions) if transcriptions else None,
        }
    return {'hours': hours, 'note': 'Battery watts cover the whole Mac, not this app. CPU 100% is one core. RSS may double-count shared pages.',
            'modes': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--label', default='baseline')
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--duration', type=float, default=600)
    parser.add_argument('--root-pid', type=int)
    args = parser.parse_args()
    if args.report:
        print(json.dumps(report(args.directory, args.hours), indent=2))
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log = EnergyLog(args.directory, args.label, args.interval, args.root_pid)
    log.start()
    print(f'Logging {args.label} for {args.duration:g}s to {args.directory}', flush=True)
    try:
        stop.wait(args.duration)
    finally:
        log.close()


if __name__ == '__main__':
    main()
