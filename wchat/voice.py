from dataclasses import dataclass
import json
import queue
import re
import time
import wave


DEVICE_CHECK_INTERVAL = 60


@dataclass
class VoiceEvent:
    kind: str
    text: str = ""


class WakeGate:
    """Only final transcripts dispatch tasks; partials just indicate activation."""

    def __init__(self, wake_word="чат", timeout=12, max_seconds=60):
        if not re.fullmatch(r"\w+", wake_word):
            raise ValueError("Wake word must be a single nonempty word")
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
            return [VoiceEvent("discard", "Timed out waiting for a command")]
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
            return events + [VoiceEvent("discard", "Wake word was not confirmed")]
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


def select_device(requested, options=None):
    options = devices() if options is None else options
    if requested is None or requested.casefold() == "auto":
        from .audio_devices import MacAudio, automatic_candidates
        candidates = automatic_candidates(options, MacAudio().devices())
        if not candidates:
            raise ValueError("No DJI or built-in Mac microphone found")
        return candidates[0][0]
    if requested.isdecimal():
        matches = [i for i, _ in options if i == int(requested)]
    else:
        matches = [i for i, name in options if requested.casefold() in name.casefold()]
    if len(matches) != 1:
        raise ValueError("Select one microphone: --devices, then --device ID or a unique name. "
                         "Use --device auto for automatic selection.")
    return matches[0]


class AudioInputError(RuntimeError):
    pass


def capture_stream(sd, selected, name, feed, emit, stop, changed):
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

    with sd.RawInputStream(samplerate=16000, blocksize=1600, device=selected,
                           dtype="int16", channels=1, callback=callback) as stream:
        emit(VoiceEvent("ready", name))
        last_audio = time.monotonic()
        while not stop.is_set():
            if changed():
                return
            if overflow:
                raise AudioInputError("Audio data lost; repeat your command")
            if not stream.active:
                raise AudioInputError("Microphone disconnected")
            try:
                data = blocks.get(timeout=.2)
            except queue.Empty:
                if time.monotonic() - last_audio > 1:
                    raise AudioInputError("Microphone stopped sending audio") from None
                continue
            last_audio = time.monotonic()
            feed(data, last_audio)


def capture_microphone(sd, feed, reset, emit, stop, requested=None, inventory=None, clock=time.monotonic,
                       capture=capture_stream):
    """Own exactly one stream. Rescan PortAudio only after that stream has closed."""
    if requested is not None and requested.casefold() != "auto":
        selected = select_device(requested)
        capture(sd, selected, sd.query_devices(selected)["name"], feed, emit, stop, lambda: False)
        return
    from .audio_devices import MacAudio, automatic_candidates
    inventory = inventory or MacAudio()
    failed_until = {}
    refresh = True
    last_notice = None
    while not stop.is_set():
        try:
            hardware = inventory.devices()
            if refresh:
                # PortAudio caches the CoreAudio device list. Never reinitialize an open stream.
                sd._terminate()
                sd._initialize()
                refresh = False
            options = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
            candidates = automatic_candidates(options, hardware)
            available = [(i, n) for i, n in candidates if failed_until.get(n, 0) <= clock()]
            if not available:
                notice = "Waiting for a DJI or built-in Mac microphone"
                if notice != last_notice:
                    emit(VoiceEvent("waiting", notice))
                    last_notice = notice
                stop.wait(DEVICE_CHECK_INTERVAL)
                refresh = True
                continue
            selected, name = available[0]
            last_notice = None
            next_check = clock() + DEVICE_CHECK_INTERVAL

            def changed():
                nonlocal next_check
                now = clock()
                if now < next_check:
                    return False
                next_check = now + DEVICE_CHECK_INTERVAL
                if inventory.devices() != hardware:
                    return True
                # Retry a failed preferred receiver after its cooldown, keeping Mac active meanwhile.
                return any(failed_until.get(n, 0) <= now for _, n in candidates[:candidates.index((selected, name))])

            try:
                capture(sd, selected, name, feed, emit, stop, changed)
            except (sd.PortAudioError, AudioInputError, OSError) as exc:
                failed_until[name] = clock() + 10
                emit(VoiceEvent("waiting", f"{name}: {exc}. Selecting an available microphone"))
            finally:
                # Never combine the start of a command from one input with another input's audio.
                reset()
                emit(VoiceEvent("discard", "Recording interrupted; repeat your command when the microphone is ready"))
            refresh = True
        except (sd.PortAudioError, OSError) as exc:
            notice = "Waiting for a microphone: " + str(exc)
            if notice != last_notice:
                emit(VoiceEvent("waiting", notice))
                last_notice = notice
            stop.wait(DEVICE_CHECK_INTERVAL)
            refresh = True


def listen(model_path, emit, stop, device=None, wav_path=None, wake_word="чат", playback=None):
    """Blocking audio worker. Audio stays local; the caller receives only commands."""
    import vosk
    vosk.SetLogLevel(-1)
    model = vosk.Model(str(model_path))
    gate = WakeGate(wake_word)
    rate = 16000
    recognizer = vosk.KaldiRecognizer(model, rate)

    def feed(data, now):
        if playback is not None and playback.is_set():
            gate.reset()
            recognizer.Reset()
            return
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
                raise ValueError("WAV required: PCM16, mono, 16000 Hz")
            emit(VoiceEvent("ready", "Local WAV test"))
            samples = 0
            while not stop.is_set() and (data := audio.readframes(1600)):
                samples += len(data) // 2
                feed(data, samples / rate)
            # A file ending while the speaker is talking is not a finished command.
            if gate.armed:
                emit(VoiceEvent("discard", "Recording ended before the command was complete"))
        return

    import sounddevice as sd
    def reset():
        gate.reset()
        recognizer.Reset()

    capture_microphone(sd, feed, reset, emit, stop, requested=device)
