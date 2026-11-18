import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from wchat.speech import SpeechPlayer, spoken_summary, split_speech_request


class SpeechTests(unittest.TestCase):
    def test_suffix_boundaries_and_punctuation(self):
        for text in ('Тест звук', 'Тест, ЗВУК!!!', 'Тест,звук.', 'Тест звук…  '):
            self.assertEqual(split_speech_request(text), ('Тест', True))
        for text in ('звук тише', 'ультразвук', 'тест звуки', 'тест звук потом'):
            self.assertEqual(split_speech_request(text), (text, False))
        with self.assertRaises(ValueError): split_speech_request('ЗВУК!')

    def test_only_short_summary_is_spoken(self):
        text = 'Full explanation.\n```python\nprint(1)\n```\n\n## Spoken summary\nГотово. **Два** изменения. [Подробности](https://example.com). Fourth sentence.'
        self.assertEqual(spoken_summary(text), 'Готово. Два изменения. Подробности.')
        self.assertLessEqual(len(spoken_summary('x ' * 1000)), 600)
        self.assertNotIn('slnc', spoken_summary('Hi [[slnc 99999]] there.'))


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_terminates_player_and_releases_microphone(self):
        player = SpeechPlayer(tail_seconds=0)
        process = AsyncMock()
        process.returncode = None
        started, ended = asyncio.Event(), asyncio.Event()
        async def communicate(data):
            self.assertEqual(data, 'Проверка'.encode())
            started.set()
            await ended.wait()
            return b'', b''
        def terminate():
            process.returncode = -15
            ended.set()
        process.communicate = communicate
        process.terminate = terminate
        with patch('wchat.speech.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch:
            task = asyncio.create_task(player.speak('Проверка'))
            await started.wait()
            self.assertTrue(player.muted.is_set())
            await player.stop()
            await task
            self.assertIn('Milena', launch.call_args.args)
            self.assertFalse(player.muted.is_set())
            self.assertIsNone(player.process)
