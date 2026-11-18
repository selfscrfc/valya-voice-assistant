import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from wchat.app import Assistant
from wchat.store import Store
from wchat.ui import UIProtocol

ROOT = Path(__file__).resolve().parents[1]
FAKE = str(ROOT / 'tests/fake_codex.py')


class UITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.create('default', self.tmp.name)
        self.store.save_context('', self.tmp.name)
        self.events = []
        self.app = Assistant(self.store, lambda kind, text, **kw: self.events.append((kind, kw)), FAKE)
        await self.app.start()
        self.ui = UIProtocol(self.app)

    async def asyncTearDown(self):
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_literal_slash_multiline_and_session_routing(self):
        await self.ui.handle({'action': 'new', 'name': 'other'})
        await self.ui.handle({'action': 'send', 'text': '/quit\n**literal**', 'session': 'default'})
        await self.app.queue.join()
        self.ui.snapshot()
        snapshot = self.events[-1][1]
        self.assertEqual(snapshot['active'], 'other')
        self.assertEqual(snapshot['jobs'][0]['session'], 'default')
        self.assertEqual(snapshot['jobs'][0]['response'], 'reply:/quit\n**literal**')
        self.assertEqual(snapshot['jobs'][0]['status'], 'completed')

    async def test_shared_context_reaches_every_session_and_updates(self):
        await self.ui.handle({'action': 'save_context', 'context': 'Project files: /tmp/project-notes', 'cwd': self.tmp.name})
        await self.ui.handle({'action': 'new', 'name': 'other'})
        self.assertEqual(self.store.session('other')['cwd'], str(Path(self.tmp.name).resolve()))
        for name in ('default', 'other'):
            await self.ui.handle({'action': 'send', 'session': name, 'text': 'question'})
            await self.app.queue.join()
            job = self.store.jobs(name)[0]
            self.assertIn('/tmp/project-notes', job['response'])
            self.assertEqual(job['prompt'], 'question')
        await self.ui.handle({'action': 'save_context', 'context': '', 'cwd': self.tmp.name})
        await self.ui.handle({'action': 'send', 'session': 'default', 'text': 'no context'})
        await self.app.queue.join()
        self.assertEqual(self.store.jobs('default')[0]['response'], 'reply:no context')
        with self.assertRaises(ValueError):
            await self.ui.handle({'action': 'save_context', 'context': 'must not save', 'cwd': self.tmp.name + '/missing'})
        self.assertEqual(self.store.setting('common_context'), '')

    async def test_mcp_validation_and_explicit_accept_decline(self):
        self.app.codex.reply = AsyncMock()
        message = {'id': 'permission', 'method': 'mcpServer/elicitation/request', 'params': {
            'mode': 'form', 'message': 'Allow Clock?', 'requestedSchema': {'type': 'object',
            'properties': {'allow': {'type': 'boolean'}}, 'required': ['allow']}}}
        self.app.on_event(message)
        with self.assertRaises(ValueError):
            await self.ui.handle({'action': 'reply', 'request': 'permission', 'decision': 'accept', 'content': {}})
        self.app.codex.reply.assert_not_awaited()
        self.assertIn('permission', self.app.requests)
        await self.ui.handle({'action': 'reply', 'request': 'permission', 'decision': 'accept', 'content': {'allow': True}})
        self.app.codex.reply.assert_awaited_once_with('permission', {'action': 'accept', 'content': {'allow': True}})
        self.assertFalse(self.app.requests)
        self.app.codex.reply.reset_mock()
        self.app.on_event(message)
        await self.ui.handle({'action': 'reply', 'request': 'permission', 'decision': 'decline'})
        self.app.codex.reply.assert_awaited_once_with('permission', {'action': 'decline'})

    async def test_clock_access_empty_form_and_cancellation(self):
        params = json.loads((ROOT / 'tests/fixtures/clock-permission.json').read_text())
        self.app.codex.reply = AsyncMock()
        for decision in ('accept', 'decline', 'cancel'):
            self.app.on_event({'id': 'clock', 'method': 'mcpServer/elicitation/request', 'params': params})
            await self.ui.handle({'action': 'reply', 'request': 'clock', 'decision': decision, 'content': {}})
            expected = {'action': decision}
            if decision == 'accept':
                expected['content'] = {}
            self.app.codex.reply.assert_awaited_with('clock', expected)
            self.assertFalse(self.app.requests)

    async def test_noninteractive_mcp_declines(self):
        self.app.interactive = False
        self.app.codex.reply = AsyncMock()
        self.app.on_event({'id': 9, 'method': 'mcpServer/elicitation/request', 'params': {}})
        await asyncio.sleep(0)
        self.app.codex.reply.assert_awaited_once_with(9, {'action': 'decline'}, None)

    async def test_real_entrypoint_ui_history_and_quit(self):
        async def launch():
            return await asyncio.create_subprocess_exec(str(ROOT / 'run'), '--ui', '--text', '--codex', FAKE,
                '--data-dir', self.tmp.name + '/cli', '--cwd', self.tmp.name, cwd=ROOT, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def until(proc, kind):
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), 5)
                self.assertTrue(line, 'backend exited unexpectedly')
                item = json.loads(line)
                if item['kind'] == kind: return item
        async def write(proc, value):
            proc.stdin.write((json.dumps(value) + '\n').encode()); await proc.stdin.drain()
        proc = await launch()
        try:
            await until(proc, 'snapshot')
            await write(proc, {'action': 'save_context', 'context': 'PERSISTED_CONTEXT', 'cwd': self.tmp.name})
            await until(proc, 'snapshot')
            await write(proc, {'action': 'send', 'text': 'hello\nsecond line', 'session': 'default'})
            await until(proc, 'finished')
            await write(proc, {'action': 'quit'})
            self.assertEqual(await asyncio.wait_for(proc.wait(), 5), 0)
            proc = await launch()
            snapshot = await until(proc, 'snapshot')
            self.assertEqual(snapshot['context'], 'PERSISTED_CONTEXT')
            self.assertIn('PERSISTED_CONTEXT', snapshot['jobs'][0]['response'])
            self.assertTrue(snapshot['jobs'][0]['response'].endswith('hello\nsecond line'))
            await write(proc, {'action': 'quit'})
            self.assertEqual(await asyncio.wait_for(proc.wait(), 5), 0)
        finally:
            if proc.returncode is None:
                proc.terminate(); await proc.wait()
