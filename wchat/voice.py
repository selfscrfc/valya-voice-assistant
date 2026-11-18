from dataclasses import dataclass
import json
import queue
import re
import time
import wave


@dataclass
class VoiceEvent:
    kind: str
    text: str = ""


class WakeGate:
    """Only final transcripts dispatch tasks; partials just indicate activation."""

    def __init__(self, wake_word="чат", timeout=12, max_seconds=60):
        if not re.fullmatch(r"\w+", wake_word):
            raise ValueError("Слово активации должно быть одним непустым словом")
        self.pattern = re.compile(r"^\s*" + re.escape(wake_word) + r"\b[\s,.:;!?-]*", re.I)
        self.timeout = timeout
        self.max_seconds = max_seconds
        self.reset()

    def reset(self):
        self.armed = False
        self.awaiting_next = False
        self.started = self.last_change = 0
        self.last_partial = ""

    def tick(self, now):
        if self.armed and (now - self.last_change > self.timeout or now - self.started > self.max_seconds):
            self.reset()
            return [VoiceEvent("discard", "Время ожидания поручения истекло")]
        return []

    def feed(self, text, final, now):
        events = self.tick(now)
        if events:
            return events
        text = text.strip()
        if not text:
            return events
        match = self.pattern.match(text)
        if not self.armed:
            if not match:
                return events
            self.armed = True
            self.started = self.last_change = now
            events.append(VoiceEvent("wake"))
        if text != self.last_partial:
            self.last_change = now
            self.last_partial = text
        if not final:
            return events
        # A partial 'чат' can be revised by the recognizer: do not execute it.
        if not match and not self.awaiting_next:
            self.reset()
            return events + [VoiceEvent("discard", "Слово активации не подтвердилось")]
        prompt = text[match.end():].strip() if match else text
        if not prompt:
            self.awaiting_next = True
            self.last_partial = ""
            return events
        events.append(VoiceEvent("prompt", prompt))
        self.reset()
        return events


def devices():
    import sounddevice as sd
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def select_device(requested):
    options = devices()
    if requested is not None and requested.isdecimal():
        matches = [i for i, _ in options if i == int(requested)]
    else:
        term = requested if requested is not None else "DJI"
        matches = [i for i, name in options if term.casefold() in name.casefold()]
    if len(matches) != 1:
        raise ValueError("Выберите один микрофон: --devices, затем --device ID или уникальное имя. "
                         "Автоматическая подмена DJI другим микрофоном отключена.")
    return matches[0]


def listen(model_path, emit, stop, device=None, wav_path=None, wake_word="чат"):
    """Blocking audio worker. Audio stays local; the caller receives only commands."""
    import vosk
    vosk.SetLogLevel(-1)
    model = vosk.Model(str(model_path))
    gate = WakeGate(wake_word)
    rate = 16000
    recognizer = vosk.KaldiRecognizer(model, rate)

    def feed(data, now):
        final = recognizer.AcceptWaveform(data)
        result = json.loads(recognizer.Result() if final else recognizer.PartialResult())
        for event in gate.feed(result.get("text" if final else "partial", ""), final, now):
            emit(event)
            if event.kind == "discard":
                recognizer.Reset()
        for event in gate.tick(now):
            emit(event)
            recognizer.Reset()

    if wav_path:
        with wave.open(str(wav_path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, rate):
                raise ValueError("Нужен WAV: PCM16, mono, 16000 Hz")
            emit(VoiceEvent("ready", "Локальная проверка WAV"))
            samples = 0
            while not stop.is_set() and (data := audio.readframes(1600)):
                samples += len(data) // 2
                feed(data, samples / rate)
            # A file ending while the speaker is talking is not a finished command.
            if gate.armed:
                emit(VoiceEvent("discard", "Запись закончилась до завершения поручения"))
        return

    import sounddevice as sd
    selected = select_device(device)
    info = sd.query_devices(selected)
    original_name = info["name"]
    blocks = queue.Queue(maxsize=40)
    overflow = False

    def callback(indata, frames, timing, status):
        nonlocal overflow
        if status:
            overflow = True
        try:
            blocks.put_nowait(bytes(indata))
        except queue.Full:
            overflow = True

    with sd.RawInputStream(samplerate=rate, blocksize=1600, device=selected,
                           dtype="int16", channels=1, callback=callback) as stream:
        emit(VoiceEvent("ready", original_name))
        while not stop.is_set():
            if overflow:
                raise RuntimeError("Потеря аудиоданных: прослушивание остановлено, повторите поручение")
            try:
                data = blocks.get(timeout=1)
            except queue.Empty:
                raise RuntimeError("Микрофон перестал передавать звук; прослушивание остановлено") from None
            if not stream.active:
                raise RuntimeError("Микрофон отключён")
            feed(data, time.monotonic())
