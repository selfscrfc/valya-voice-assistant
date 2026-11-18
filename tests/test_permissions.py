import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from wchat.app import Assistant
from wchat.store import Store

ROOT = Path(__file__).resolve().parents[1]


class PermissionModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.create('default', self.tmp.name)
        self.events = []
        self.app = Assistant(self.store, lambda *a, **kw: self.events.append((a, kw)), str(ROOT / 'tests/fake_codex.py'))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_never_applies_to_new_and_resumed_sessions(self):
        with patch.object(self.app.codex, 'call', wraps=self.app.codex.call) as call:
            await self.app.prepare('default')
            self.app.loaded.clear()
            await self.app.prepare('default')
        requests = [c for c in call.call_args_list if c.args[0] in ('thread/start', 'thread/resume')]
        self.assertEqual([c.args[0] for c in requests], ['thread/start', 'thread/resume'])
        self.assertTrue(all(c.args[1]['approvalPolicy'] == 'never' for c in requests))
        self.assertTrue(all(c.args[1]['sandbox'] == 'workspace-write' for c in requests))

    async def test_clock_permission_is_accepted_without_a_dialog(self):
        self.app.codex.reply = AsyncMock()
        params = json.loads((ROOT / 'tests/fixtures/clock-permission.json').read_text())
        self.app.on_event({'id': 'clock', 'method': 'mcpServer/elicitation/request', 'params': params})
        await asyncio.sleep(0)
        self.app.codex.reply.assert_awaited_once_with('clock', {'action': 'accept', 'content': {}}, None)
        self.assertFalse(self.app.requests)

    async def test_command_and_file_permissions_are_accepted(self):
        self.app.codex.reply = AsyncMock()
        self.app.interactive = False
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
            self.app.on_event({'id': method, 'method': method, 'params': {}})
            await asyncio.sleep(0)
            self.app.codex.reply.assert_awaited_with(method, {'decision': 'accept'}, None)
        self.assertFalse(self.app.requests)

    async def test_questions_and_forms_are_not_fabricated(self):
        self.app.codex.reply = AsyncMock()
        requests = [
            ('question', 'item/tool/requestUserInput', {'questions': [{'id': 'name', 'question': 'Which file?'}]}),
            ('form', 'mcpServer/elicitation/request', {'mode': 'form', '_meta': {'codex_approval_kind': 'mcp_tool_call'},
                'requestedSchema': {'type': 'object', 'properties': {'name': {'type': 'string'}}}}),
            ('generic', 'mcpServer/elicitation/request', {'mode': 'form', '_meta': None, 'requestedSchema': {}}),
            ('auth', 'mcpServer/elicitation/request', {'mode': 'url', 'url': 'https://example.com/login'}),
        ]
        for rid, method, params in requests:
            self.app.on_event({'id': rid, 'method': method, 'params': params})
        await asyncio.sleep(0)
        self.app.codex.reply.assert_not_awaited()
        self.assertEqual(set(self.app.requests), {'question', 'form', 'generic', 'auth'})
