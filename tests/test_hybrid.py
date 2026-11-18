from concurrent.futures import Future
import json
import threading
import unittest

from wchat.hybrid import HybridRecognizer


class Recognizer:
    text, final = '', False
    resets = 0
    def AcceptWaveform(self, data): return self.final
    def Result(self): return json.dumps({'text': self.text})
    def PartialResult(self): return json.dumps({'partial': self.text})
    def Reset(self): self.resets += 1; self.text = ''; self.final = False


class Worker:
    def __init__(self): self.calls = []
    def submit(self, audio):
        future = Future()
        self.calls.append((audio, future))
        return future


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.recognizer, self.worker, self.events = Recognizer(), Worker(), []
        self.muted = threading.Event()
        self.engine = HybridRecognizer(self.recognizer, self.worker, self.events.append, playback=self.muted)

    def feed(self, text, final, now, data=b'\0' * 3200):
        self.recognizer.text, self.recognizer.final = text, final
        self.engine.feed(data, now)

    def test_whisper_only_runs_after_wake_and_endpoint_with_preroll(self):
        first, second, third = b'a' * 3200, b'b' * 3200, b'c' * 3200
        self.feed('', False, .1, first)
        self.feed('чат', False, .2, second)
        self.assertEqual(self.worker.calls, [])
        self.feed('чат неверный текст', True, 1, third)
        self.assertEqual(self.worker.calls[0][0], first + second + third)
        self.assertFalse(any(e.kind == 'prompt' for e in self.events))
        self.worker.calls[0][1].set_result('Чат, правильный текст, звук.')
        self.engine.poll()
        self.assertEqual([e.text for e in self.events if e.kind == 'prompt'], ['правильный текст, звук.'])

    def test_idle_and_ordinary_speech_never_run_whisper(self):
        for i in range(40): self.feed('обычная речь', False, i / 10)
        self.assertLessEqual(self.engine.ring_size, 64000)
        self.feed('обычная речь', True, 5)
        self.assertEqual(self.engine.ring_size, 0)
        self.assertEqual(self.worker.calls, [])

    def test_pause_after_wake_keeps_audio(self):
        self.feed('чат', True, 1)
        self.assertTrue(self.engine.gate.awaiting_next)
        self.feed('другая расшифровка', True, 3)
        self.worker.calls[0][1].set_result('Чат. Установи таймер на пять минут.')
        self.engine.poll()
        self.assertEqual([e.text for e in self.events if e.kind == 'prompt'], ['Установи таймер на пять минут.'])

    def test_missing_wake_in_whisper_never_executes_preroll(self):
        self.feed('чат вопрос', True, 1)
        self.worker.calls[0][1].set_result('Ненамеренный текст без активации.')
        self.engine.poll()
        self.assertFalse(any(e.kind == 'prompt' for e in self.events))

    def test_device_switch_discards_late_transcription(self):
        self.feed('чат вопрос', True, 1)
        self.engine.reset()
        self.worker.calls[0][1].set_result('Чат, старое поручение.')
        self.engine.poll()
        self.assertFalse(any(e.kind == 'prompt' for e in self.events))

    def test_playback_discards_inflight_and_suppresses_wake(self):
        self.feed('чат вопрос', True, 1)
        self.muted.set()
        self.feed('чат новое поручение', True, 2)
        self.assertTrue(any(e.kind == 'discard' for e in self.events))
        self.worker.calls[0][1].set_result('Чат, старое поручение.')
        self.engine.poll()
        self.assertFalse(any(e.kind == 'prompt' for e in self.events))
        self.assertEqual(len(self.worker.calls), 1)
        self.muted.clear()
        self.feed('чат следующий вопрос', True, 3)
        self.assertEqual(len(self.worker.calls), 2)

    def test_timeout_and_decoder_failure_recover(self):
        self.feed('чат', False, 0)
        self.feed('', False, 13)
        self.assertFalse(self.engine.gate.armed)
        self.assertEqual(self.worker.calls, [])
        self.feed('чат вопрос', True, 14)
        self.worker.calls[0][1].set_exception(RuntimeError('fixture failure'))
        self.engine.poll()
        self.assertTrue(any(e.kind == 'transcription_error' for e in self.events))
        self.feed('чат повтор', True, 15)
        self.assertEqual(len(self.worker.calls), 2)
