import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from wchat.energy import EnergyLog, battery_sample, cpu_seconds


class EnergyTests(unittest.TestCase):
    def test_battery_signed_current_and_ac_are_not_app_watts(self):
        battery = {'InstantAmperage': 2**64-1000, 'Voltage': 12000, 'CurrentCapacity': 60,
                   'ExternalConnected': False, 'IsCharging': False}
        with patch('wchat.energy.subprocess.check_output', return_value=plistlib.dumps([battery])):
            self.assertEqual(battery_sample()['battery_discharge_w'], 12)
        battery['ExternalConnected'] = True
        with patch('wchat.energy.subprocess.check_output', return_value=plistlib.dumps([battery])):
            self.assertIsNone(battery_sample()['battery_discharge_w'])

    def test_cpu_deltas_and_private_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EnergyLog(directory, 'hybrid', interval=60)
            with patch('wchat.energy.battery_sample', return_value={'on_battery': False}), \
                 patch('wchat.energy.process_sample', side_effect=[({1: 10}, 1024), ({1: 16}, 1025)]), \
                 patch('wchat.energy.time.monotonic', side_effect=[0, 60]):
                log.sample(); log.sample()
            path = next(Path(directory).glob('*.jsonl'))
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[-1]['app_cpu_percent'], 10)
            self.assertEqual(records[-1]['app_rss_mb'], 1025)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse({'prompt', 'text', 'audio'} & records[-1].keys())

    def test_ps_time_formats(self):
        self.assertEqual(cpu_seconds('01:02.50'), 62.5)
        self.assertEqual(cpu_seconds('1-01:02:03'), 90123)
