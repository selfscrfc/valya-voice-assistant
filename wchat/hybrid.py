"""Vosk wake detection with buffered, on-demand Whisper transcription."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time

DEFAULT_MODEL = Path.home() / 'Library/Application Support/wchat/models/whisper-large-v3-turbo'


class WhisperWorker:
    def __init__(self, model=DEFAULT_MODEL, metric=lambda *a, **kw: None):
        self.model = str(model)
        self.metric = metric
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='whisper')

    def warm(self, stop):
        def load():
            started = time.monotonic()
            import numpy as np
            from mlx_whisper import transcribe
            if not Path(self.model).is_dir():
                raise RuntimeError('Whisper model missing; run .venv/bin/python scripts/download_whisper.py')
            # The pinned mlx-whisper caches its model. One silent pass also warms Metal kernels.
            transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=self.model,
                       language='ru', temperature=0, condition_on_previous_text=False, verbose=None)
            self.metric('whisper_warm', seconds=round(time.monotonic() - started, 3))
        future = self.executor.submit(load)
        while not stop.is_set():
            if future.done():
                future.result()
                return
            stop.wait(.1)
        raise InterruptedError('Whisper startup cancelled')

    def submit(self, audio):
        def transcribe_audio():
            import numpy as np
            from mlx_whisper import transcribe
            started = time.monotonic()
            self.metric('transcription_started', audio_seconds=round(len(audio) / 32000, 3))
            try:
                result = transcribe(np.frombuffer(audio, dtype='<i2').astype(np.float32) / 32768,
                                    path_or_hf_repo=self.model, language='ru', temperature=0,
                                    condition_on_previous_text=False, verbose=None)
                self.metric('transcription_finished', seconds=round(time.monotonic() - started, 3),
                            audio_seconds=round(len(audio) / 32000, 3))
                return result['text']
            except Exception:
                self.metric('transcription_failed', seconds=round(time.monotonic() - started, 3))
                raise
        return self.executor.submit(transcribe_audio)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


class HybridRecognizer:
    def __init__(self, recognizer, worker, emit, wake_word='чат', playback=None, metric=lambda *a, **kw: None):
        from .voice import WakeGate
        self.recognizer, self.worker, self.emit = recognizer, worker, emit
        self.gate = WakeGate(wake_word)
        self.wake = re.compile(r'^\s*["«“]*' + re.escape(wake_word) + r'\b[\s,.:;!?»”"-]*', re.I)
        self.playback = playback
        self.metric = metric
        self.ring = deque()
        self.ring_size = 0
        self.clip = bytearray()
        self.future = None
        self.generation = 0
        self.pending_generation = 0
        self.was_muted = False

    def reset(self):
        self.generation += 1
        self.gate.reset()
        self.recognizer.Reset()
        self.ring.clear()
        self.ring_size = 0
        self.clip.clear()

    def poll(self):
        from .voice import VoiceEvent
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        valid = self.pending_generation == self.generation
        try:
            text = future.result().strip()
            if not valid:
                return
            match = self.wake.match(text)
            # Verify the wake word in the preserved audio; never execute pre-roll conversation.
            prompt = text[match.end():].strip() if match else ''
            if prompt:
                self.emit(VoiceEvent('prompt', prompt))
            else:
                self.emit(VoiceEvent('discard', 'Wake word or command was not confirmed by Whisper'))
                self.metric('transcription_discarded')
        except Exception as exc:
            if valid:
                self.emit(VoiceEvent('transcription_error', str(exc)))
                self.emit(VoiceEvent('discard', 'Transcription failed; repeat your command'))
        finally:
            self.reset()

    def feed(self, data, now):
        from .voice import VoiceEvent
        muted = self.playback is not None and self.playback.is_set()
        if muted:
            if not self.was_muted:
                interrupted = self.gate.armed or self.future is not None
                self.reset()
                if interrupted:
                    self.emit(VoiceEvent('discard', 'Capture paused during speech playback'))
            self.was_muted = True
            self.poll()
            return
        self.was_muted = False
        if self.future is not None:
            self.poll()
            return
        self.ring.append(data)
        self.ring_size += len(data)
        while self.ring_size > 64000:  # Two seconds of PCM16 mono at 16 kHz.
            self.ring_size -= len(self.ring.popleft())
        armed = self.gate.armed
        if armed:
            self.clip.extend(data)
        final = self.recognizer.AcceptWaveform(data)
        result = json.loads(self.recognizer.Result() if final else self.recognizer.PartialResult())
        events = self.gate.feed(result.get('text' if final else 'partial', ''), final, now)
        events += self.gate.tick(now)
        for event in events:
            if event.kind == 'wake':
                self.clip = bytearray(b''.join(self.ring))
                self.metric('wake_detected')
                self.emit(event)
            elif event.kind == 'prompt':
                # Vosk supplies only the endpoint. Whisper supplies the command text.
                self.future = self.worker.submit(bytes(self.clip))
                self.pending_generation = self.generation
                self.clip.clear()
                self.emit(VoiceEvent('transcribing', 'Transcribing with Whisper…'))
                self.recognizer.Reset()
                self.ring.clear()
                self.ring_size = 0
            elif event.kind == 'discard':
                self.reset()
                self.emit(event)
        if final and not self.gate.armed and self.future is None:
            self.ring.clear()
            self.ring_size = 0
