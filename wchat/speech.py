"""Per-request speech output using the local macOS voices."""
import asyncio
import re
import threading


TRIGGER = re.compile(r'(?:^|(?<=\s)|(?<=[,;:]))звук\b[^\w]*$', re.I)
SUMMARY_HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?Spoken summary(?:\*\*)?\s*:?\s*$', re.M | re.I)
SPEECH_INSTRUCTION = (
    '\n\n[Response format for this request only: Keep the full answer in chat. '
    'At the end of your final answer, add a section headed exactly "Spoken summary". '
    'Write 1-3 short sentences in the language of the user request summarizing the actual result, '
    'important failure, or clarification needed. Use plain prose without code, links or lists. '
    'The app will speak this section locally; do not run speech tools yourself.]'
)


def split_speech_request(prompt):
    match = TRIGGER.search(prompt)
    if match is None:
        return prompt, False
    command = prompt[:match.start()].rstrip(' \t\r\n,;:.!?')
    if not command:
        raise ValueError('Add a request before the speech keyword “звук”.')
    return command, True


def spoken_summary(text):
    headings = list(SUMMARY_HEADING.finditer(text))
    if headings:
        text = text[headings[-1].end():]
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\[\[[\s\S]*?\]\]', '', text)
    text = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}|[-*+] |\d+[.)] )\s*', '', text)
    text = re.sub(r'[*_`]', '', text)
    text = ' '.join(text.split())
    sentences = re.split(r'(?<=[.!?])\s+', text)
    summary = ' '.join(sentences[:3])
    if len(summary) > 600:
        summary = summary[:597].rsplit(' ', 1)[0] + '…'
    return summary


class SpeechPlayer:
    def __init__(self, tail_seconds=.4):
        self.muted = threading.Event()
        self.process = None
        self.cancel_requested = False
        self.tail_seconds = tail_seconds

    async def speak(self, text):
        self.cancel_requested = False
        self.muted.set()
        try:
            voice = 'Milena' if re.search('[А-Яа-яЁё]', text) else 'Samantha'
            self.process = await asyncio.create_subprocess_exec(
                '/usr/bin/say', '-v', voice, '-r', '180',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            if self.cancel_requested:
                await self.stop()
            try:
                _, error = await asyncio.wait_for(self.process.communicate(text.encode()), 60)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
                raise RuntimeError('Speech playback timed out') from None
            if self.process.returncode and not self.cancel_requested:
                raise RuntimeError(error.decode(errors='replace').strip() or 'Speech playback failed')
        finally:
            if self.process and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            self.process = None
            try:
                # Discard speaker echo and already buffered audio before listening again.
                await asyncio.sleep(self.tail_seconds)
            finally:
                self.muted.clear()

    async def stop(self):
        self.cancel_requested = True
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            await self.process.wait()
