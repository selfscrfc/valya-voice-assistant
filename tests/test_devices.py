import unittest
from unittest.mock import Mock, patch

from wchat.audio_devices import AudioDevice, automatic_candidates
from wchat.voice import capture_microphone, select_device, WakeGate

MAC = AudioDevice(1, 'MacBook Pro Microphone', 'Apple Inc.', True)
DJI = AudioDevice(2, 'Wireless Mic Rx', 'DJI Technology Co., Ltd.', False)
VIRTUAL = AudioDevice(3, 'BlackHole 64ch', 'Existential Audio', False)
PHONE = AudioDevice(4, 'Phone Microphone', 'Apple Inc.', False)


class Stop:
    stopped = False
    def __init__(self, on_wait=lambda: None):
        self.on_wait = on_wait
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, seconds):
        self.last_wait = seconds
        self.on_wait()
        return self.stopped


class Inventory:
    def __init__(self, devices): self.current = devices
    def devices(self): return self.current


class Audio:
    class PortAudioError(Exception): pass
    def __init__(self, inventory):
        self.inventory = inventory
        self.active = False
        self.refreshes = 0
    def _terminate(self):
        assert not self.active, 'must close stream before refreshing PortAudio'
    def _initialize(self): self.refreshes += 1
    def query_devices(self):
        return [{'name': d.name, 'max_input_channels': 1} for d in self.inventory.current]


class DeviceTests(unittest.TestCase):
    def test_dji_manufacturer_beats_builtin_and_excludes_virtual_and_phone(self):
        items = [PHONE, MAC, VIRTUAL, DJI]
        options = [(i, d.name) for i, d in enumerate(items)]
        self.assertEqual(automatic_candidates(options, items), [(3, DJI.name), (1, MAC.name)])
        self.assertEqual(automatic_candidates(options[:3], items[:3]), [(1, MAC.name)])
        self.assertEqual(automatic_candidates([(0, PHONE.name), (1, VIRTUAL.name)], [PHONE, VIRTUAL]), [])

    def test_explicit_missing_device_does_not_fall_back(self):
        with patch('wchat.voice.devices', return_value=[(0, MAC.name)]):
            with self.assertRaises(ValueError): select_device('DJI')
            self.assertEqual(select_device('MacBook'), 0)
            self.assertEqual(select_device('0'), 0)

    def test_switch_mac_dji_mac_resets_incomplete_command(self):
        inventory = Inventory([MAC])
        audio, stop, now, events, opened = Audio(inventory), Stop(), [0], [], []
        gate = WakeGate()
        reset = Mock(side_effect=gate.reset)
        def capture(sd, selected, name, feed, emit, stopping, changed):
            sd.active = True
            opened.append(name)
            try:
                if len(opened) == 1:
                    gate.feed('чат', False, 0)
                    inventory.current = [MAC, DJI]
                elif len(opened) == 2:
                    self.assertFalse(gate.armed)
                    self.assertEqual(gate.feed('удали файлы', True, 3), [])
                    inventory.current = [MAC]
                else:
                    self.assertFalse(gate.armed)
                    stop.set(); return
                now[0] += 59
                self.assertFalse(changed())
                now[0] += 1
                self.assertTrue(changed())
            finally: sd.active = False
        capture_microphone(audio, Mock(), reset, events.append, stop, inventory=inventory,
                           clock=lambda: now[0], capture=capture)
        self.assertEqual(opened, [MAC.name, DJI.name, MAC.name])
        self.assertEqual(reset.call_count, 3)
        self.assertEqual(audio.refreshes, 3)
        self.assertFalse(any(e.kind == 'prompt' for e in events))

    def test_unavailable_dji_falls_back_and_retries_after_cooldown(self):
        inventory, stop, now, opened = Inventory([MAC, DJI]), Stop(), [0], []
        audio = Audio(inventory)
        def capture(sd, selected, name, feed, emit, stopping, changed):
            opened.append(name)
            if len(opened) == 1: raise sd.PortAudioError('disconnected')
            if len(opened) == 2:
                now[0] = 3; self.assertFalse(changed())
                now[0] = 12; self.assertFalse(changed())
                now[0] = 60; self.assertTrue(changed())
            else: stop.set()
        capture_microphone(audio, Mock(), Mock(), Mock(), stop, inventory=inventory,
                           clock=lambda: now[0], capture=capture)
        self.assertEqual(opened, [DJI.name, MAC.name, DJI.name])

    def test_waits_for_a_microphone_instead_of_exiting(self):
        inventory, events = Inventory([]), []
        stop = Stop(lambda: setattr(inventory, 'current', [MAC]))
        opened = []
        def capture(sd, selected, name, feed, emit, stopping, changed):
            opened.append(name); stop.set()
        capture_microphone(Audio(inventory), Mock(), Mock(), events.append, stop,
                           inventory=inventory, capture=capture)
        self.assertEqual(opened, [MAC.name])
        self.assertEqual(events[0].kind, 'waiting')
        self.assertEqual(stop.last_wait, 60)
