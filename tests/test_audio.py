from pathlib import Path
import tempfile
import threading
import unittest
import wave
from concurrent.futures import Future
from unittest.mock import patch

from wchat.voice import listen

MODEL = Path.home() / "Library/Application Support/wchat/models/vosk-model-small-ru-0.22"
FIXTURES = Path(__file__).with_name("fixtures")


@unittest.skipUnless(MODEL.is_dir(), "Run make setup to install the Russian model")
class AudioTests(unittest.TestCase):
    def recognize(self, path):
        events = []
        listen(MODEL, events.append, threading.Event(), wav_path=path)
        return events

    def test_playback_cannot_trigger_a_command(self):
        muted = threading.Event()
        muted.set()
        events = []
        listen(MODEL, events.append, threading.Event(), wav_path=FIXTURES / "command.wav", playback=muted)
        self.assertFalse(any(e.kind in ("wake", "prompt") for e in events))
        muted.clear()
        events.clear()
        listen(MODEL, events.append, threading.Event(), wav_path=FIXTURES / "command.wav", playback=muted)
        self.assertTrue(any(e.kind == "prompt" for e in events))

    def test_hybrid_wav_waits_for_pending_transcription(self):
        class Worker:
            def __init__(self, *args): pass
            def warm(self, stop): pass
            def submit(self, audio):
                result = Future()
                threading.Timer(.5, lambda: result.set_result("Чат, правильное поручение.")).start()
                return result
            def close(self): pass
        events = []
        with patch('wchat.hybrid.WhisperWorker', Worker):
            listen(MODEL, events.append, threading.Event(), wav_path=FIXTURES / "command.wav", asr="hybrid")
        self.assertEqual([e.text for e in events if e.kind == "prompt"], ["правильное поручение."])

    def test_continuous_phrase(self):
        events = self.recognize(FIXTURES / "command.wav")
        self.assertEqual([e.text for e in events if e.kind == "prompt"], ["сколько будет два плюс два"])

    def test_wake_then_pause_then_prompt(self):
        events = self.recognize(FIXTURES / "pause.wav")
        self.assertEqual([e.text for e in events if e.kind == "prompt"], ["сколько будет три плюс пять"])

    def test_ordinary_speech_does_not_execute(self):
        events = self.recognize(FIXTURES / "ordinary.wav")
        self.assertFalse(any(e.kind == "prompt" for e in events))

    def test_truncated_audio_never_dispatches_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "truncated.wav"
            with wave.open(str(FIXTURES / "command.wav"), "rb") as source:
                with wave.open(str(target), "wb") as out:
                    out.setparams(source.getparams())
                    out.writeframes(source.readframes(16000))
            self.assertFalse(any(e.kind == "prompt" for e in self.recognize(target)))
