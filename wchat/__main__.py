import argparse
import asyncio
import json
from pathlib import Path
import re
import signal
import sys
import threading

from .app import Assistant
from .store import Store
from .ui import UIProtocol
from .voice import VoiceEvent, devices, listen


ROOT = Path.home() / "Library/Application Support/wchat"


def parser():
    p = argparse.ArgumentParser(description="Voice assistant: listens for “чат” and runs tasks through Codex")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--text", action="store_true", help="Text chat only, without a microphone")
    modes.add_argument("--once", help="Send one text request, then exit")
    modes.add_argument("--wav", type=Path, help="Read PCM16 mono 16 kHz WAV instead of microphone audio")
    p.add_argument("--devices", action="store_true", help="List available microphones and exit")
    p.add_argument("--device", help="auto (default): DJI, otherwise built-in Mac; or ID/unique name")
    p.add_argument("--voice-model", type=Path, default=ROOT / "models/vosk-model-small-ru-0.22")
    p.add_argument("--wake-word", default="чат")
    p.add_argument("--transcribe-only", action="store_true", help="Test wake detection locally, without Codex")
    p.add_argument("--data-dir", type=Path, default=ROOT)
    p.add_argument("--cwd", type=Path, help="Folder for new conversations; default ~/Documents/jack")
    p.add_argument("--session", help="Create or select a named conversation")
    p.add_argument("--model", help="Codex model; defaults to the installed CLI setting")
    p.add_argument("--codex", default="codex", help=argparse.SUPPRESS)
    p.add_argument("--turn-timeout", type=float, default=600)
    p.add_argument("--ui", action="store_true", help="JSON protocol for the desktop client")
    p.add_argument("--json", action="store_true", help="Output JSONL events")
    return p


async def main(args):
    if args.ui:
        args.json = True
    def emit(kind, text, **details):
        if args.json:
            print(json.dumps({"kind": kind, "text": text, **details}, ensure_ascii=False), flush=True)
        else:
            clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
            if kind == "delta":
                print(clean, end="", flush=True)
            else:
                extra = " " + json.dumps(details, ensure_ascii=False) if details else ""
                print(f"\n[{kind}] {clean}{extra}", flush=True)

    if args.devices:
        for index, name in devices():
            emit("device", name, id=index)
        return 0
    voice_enabled = not args.text and args.once is None
    if args.text:
        emit("voice_disabled", "Microphone off: --text enables text chat only. "
             "For voice, restart without --text, for example ./run --device 'MacBook Pro Microphone'.")
    if args.transcribe_only and not voice_enabled:
        raise ValueError("--transcribe-only requires a microphone or --wav")
    if voice_enabled and not args.voice_model.is_dir():
        raise ValueError(f"Model not found: {args.voice_model}; run make setup")
    store = Store(args.data_dir)
    assistant = Assistant(store, emit, executable=args.codex, model=args.model, turn_timeout=args.turn_timeout,
                          interactive=args.once is None and args.wav is None)
    ui = UIProtocol(assistant)
    stop_audio = threading.Event()
    voice_thread = None
    reader_transport = None
    stdin_task = None
    exit_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    voice_events = asyncio.Queue()
    captured_session = None
    voice_error = False
    submitted = []
    try:
        if not store.sessions():
            store.create(args.session or "default", args.cwd or store.default_cwd)
        elif args.session:
            if args.session not in {s["name"] for s in store.sessions()}:
                store.create(args.session, args.cwd or store.default_cwd)
            else:
                store.select(args.session)
        emit("session", store.active, cwd=store.session(store.active)["cwd"])
        for job in store.jobs(limit=100):
            if job["status"] in ("unknown", "needs_review"):
                emit("review", "Review the previous task before retrying", job=job["id"], status=job["status"])
        if not args.transcribe_only:
            await assistant.start()
        if args.ui:
            ui.snapshot()
        if args.once is not None:
            job = assistant.enqueue(args.once)
            result = await assistant.waiters[job]
            return 0 if result["status"] == "completed" else 1

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, exit_requested.set)

        async def input_loop():
            nonlocal reader_transport
            reader = asyncio.StreamReader(limit=1024 * 1024)
            protocol = asyncio.StreamReaderProtocol(reader)
            try:
                reader_transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            except (OSError, ValueError):
                assistant.interactive = False
                if not voice_enabled:
                    emit("error", "Terminal input is unavailable")
                    exit_requested.set()
                return
            while line := await reader.readline():
                text = line.decode().strip()
                if not text:
                    continue
                if text == "/quit":
                    exit_requested.set()
                    return
                try:
                    if args.ui:
                        action = json.loads(text)
                        if isinstance(action, dict) and action.get("action") == "quit":
                            exit_requested.set()
                            return
                        await ui.handle(action)
                    elif args.transcribe_only:
                        emit("status", "Local test: /quit to exit")
                    elif text.startswith("/"):
                        await assistant.command(text)
                    else:
                        assistant.enqueue(text)
                except (ValueError, RuntimeError, TypeError, KeyError) as exc:
                    emit("error", str(exc))
            # In voice mode the closed terminal input must not disable listening.
            assistant.interactive = False
            if args.ui:
                exit_requested.set()
            elif not voice_enabled:
                await assistant.queue.join()
                exit_requested.set()

        if not args.wav:
            emit("help", "Exit with /quit. /sessions, /new NAME [FOLDER], /use NAME, /history, /cancel, /queue, /clear")
            stdin_task = asyncio.create_task(input_loop())
        if voice_enabled:
            def audio_worker():
                try:
                    listen(args.voice_model, lambda event: loop.call_soon_threadsafe(voice_events.put_nowait, event),
                           stop_audio, device=args.device, wav_path=args.wav, wake_word=args.wake_word,
                           playback=assistant.speaker.muted)
                except Exception as exc:
                    loop.call_soon_threadsafe(voice_events.put_nowait, VoiceEvent("error", str(exc)))
                finally:
                    loop.call_soon_threadsafe(voice_events.put_nowait, VoiceEvent("end"))

            voice_thread = threading.Thread(target=audio_worker, daemon=True)
            voice_thread.start()
            while not exit_requested.is_set():
                try:
                    event = await asyncio.wait_for(voice_events.get(), .2)
                except asyncio.TimeoutError:
                    continue
                if assistant.speaker.muted.is_set() and event.kind in ("wake", "prompt", "discard"):
                    captured_session = None
                    continue
                emit("voice_" + event.kind, event.text)
                if event.kind == "wake":
                    captured_session = store.active
                elif event.kind == "discard":
                    captured_session = None
                elif event.kind == "prompt":
                    if not args.transcribe_only:
                        if event.text.casefold().strip(" .,!?;") == "стоп":
                            await assistant.cancel()
                        else:
                            try:
                                submitted.append(assistant.enqueue(event.text, "voice", captured_session))
                            except (ValueError, RuntimeError, TypeError, KeyError) as exc:
                                emit("error", str(exc))
                    captured_session = None
                elif event.kind == "error":
                    voice_error = True
                elif event.kind == "end":
                    if args.ui and not args.wav:
                        # A disconnected microphone must not take the written chat down.
                        await exit_requested.wait()
                    else:
                        await assistant.queue.join()
                        exit_requested.set()
        else:
            await exit_requested.wait()
        return 1 if voice_error or any(store.job(j)["status"] != "completed" for j in submitted) else 0
    finally:
        stop_audio.set()
        if stdin_task:
            stdin_task.cancel()
            await asyncio.gather(stdin_task, return_exceptions=True)
        if reader_transport:
            reader_transport.close()
        if voice_thread:
            await asyncio.to_thread(voice_thread.join, 3)
        await assistant.close()
        store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(parser().parse_args())))
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        print(f"wchat: {exc}", file=sys.stderr)
        raise SystemExit(1)
