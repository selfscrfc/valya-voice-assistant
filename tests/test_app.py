import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from wchat.app import Assistant
from wchat.store import Store

FAKE = str(Path(__file__).with_name("fake_codex.py").resolve())


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.create("default", self.tmp.name)
        self.events = []
        self.app = Assistant(self.store, lambda kind, text, **kw: self.events.append((kind, text, kw)), FAKE)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_sound_suffix_plays_once_for_voice_and_text(self):
        self.app.speaker.speak = AsyncMock()
        with patch.object(self.app.codex, "call", wraps=self.app.codex.call) as call:
            for source in ("voice", "text"):
                result = await self.app.waiters[self.app.enqueue("Проверка, ЗВУК!", source)]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["prompt"], "Проверка, ЗВУК!")
                self.assertEqual(result["response"], "reply:Проверка")
            prompts = [c.args[1]["input"][0]["text"] for c in call.call_args_list if c.args[0] == "turn/start"]
            self.assertTrue(all(p.startswith("Проверка\n\n[Response format") for p in prompts))
        self.assertEqual(self.app.speaker.speak.await_count, 2)
        self.app.speaker.speak.assert_awaited_with("reply:Проверка")
        self.assertEqual(sum(k == "speech_finished" for k, _, _ in self.events), 2)

    async def test_plain_request_and_middle_word_stay_silent(self):
        self.app.speaker.speak = AsyncMock()
        for prompt in ("hello", "сделай звук тише", "ультразвук"):
            await self.app.waiters[self.app.enqueue(prompt)]
        self.app.speaker.speak.assert_not_awaited()
        with self.assertRaises(ValueError): self.app.enqueue("звук!")

    async def test_speech_failure_preserves_completed_task_and_queue(self):
        self.app.speaker.speak = AsyncMock(side_effect=RuntimeError("fixture audio failure"))
        result = await self.app.waiters[self.app.enqueue("hello звук")]
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.app.halted)
        self.assertTrue(any(k == "speech_error" for k, _, _ in self.events))
        result = await self.app.waiters[self.app.enqueue("next")]
        self.assertEqual(result["response"], "reply:next")

    async def test_failed_request_speaks_failure_not_success(self):
        self.app.speaker.speak = AsyncMock()
        result = await self.app.waiters[self.app.enqueue("FAIL звук")]
        self.assertEqual(result["status"], "failed")
        self.assertIn("Не удалось", self.app.speaker.speak.await_args.args[0])

    async def test_close_during_speech_preserves_completed_result(self):
        entered = asyncio.Event()
        async def speak(text):
            entered.set()
            await asyncio.Event().wait()
        self.app.speaker.speak = speak
        job = self.app.enqueue("hello звук")
        await asyncio.wait_for(entered.wait(), 3)
        await self.app.close()
        self.assertEqual(self.store.job(job)["status"], "completed")
        self.assertEqual(self.store.job(job)["response"], "reply:hello")

    async def test_serial_queue_routes_to_captured_session(self):
        first = self.app.enqueue("first")
        self.store.create("other", self.tmp.name)
        second = self.app.enqueue("second")
        await asyncio.wait_for(self.app.queue.join(), 5)
        self.assertEqual(self.store.job(first)["session"], "default")
        self.assertEqual(self.store.job(second)["session"], "other")
        self.assertNotEqual(self.store.session("default")["thread_id"], self.store.session("other")["thread_id"])
        self.assertEqual(self.store.job(first)["response"], "reply:first")
        ordered = [(k, d["job"]) for k, _, d in self.events if k in ("started", "finished")]
        self.assertEqual(ordered, [("started", first), ("finished", first), ("started", second), ("finished", second)])

    async def test_followups_use_same_thread(self):
        for prompt in ("first", "second"):
            await self.app.waiters[self.app.enqueue(prompt)]
        self.assertEqual({d["thread"] for k, _, d in self.events if k == "started"}, {"thread-1"})

    async def test_cancel_records_interrupted_and_keeps_previous_data(self):
        old = self.app.enqueue("old")
        await self.app.waiters[old]
        job = self.app.enqueue("WAIT")
        for _ in range(100):
            if self.app.current_turn:
                break
            await asyncio.sleep(.01)
        await self.app.cancel()
        result = await asyncio.wait_for(self.app.waiters[job], 3)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(self.store.job(old)["response"], "reply:old")

    async def test_cancel_during_session_start_does_not_send_prompt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.app.prepare

        async def delayed(name):
            entered.set()
            await release.wait()
            return await original(name)

        self.app.prepare = delayed
        job = self.app.enqueue("must not execute")
        await entered.wait()
        await self.app.cancel()
        release.set()
        result = await asyncio.wait_for(self.app.waiters[job], 3)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["turn_id"])

    async def test_failed_is_not_reported_as_completed(self):
        result = await self.app.waiters[self.app.enqueue("FAIL")]
        self.assertEqual(result["status"], "failed")
        self.assertIn("fixture failure", result["error"])

    async def test_crash_halts_queue_without_replaying(self):
        first = self.app.enqueue("CRASH")
        second = self.app.enqueue("must not run")
        await asyncio.wait_for(self.app.queue.join(), 5)
        self.assertEqual(self.store.job(first)["status"], "unknown")
        self.assertEqual(self.store.job(second)["status"], "needs_review")
        self.assertTrue(self.app.halted)

    async def test_explicit_permission_decision_only(self):
        self.app.approval_policy = "on-request"
        self.app.on_event({"id": "approve-1", "method": "item/commandExecution/requestApproval", "params": {"command": "example"}})
        self.assertIn("approve-1", self.app.requests)
        await self.app.command("/approve approve-1 no")
        self.assertNotIn("approve-1", self.app.requests)
        with self.assertRaises(ValueError):
            await self.app.command("/approve approve-1 yes")

    async def test_multiple_questions_wait_for_each_answer(self):
        self.app.codex.reply = AsyncMock()
        self.app.on_event({"id": "q1", "method": "item/tool/requestUserInput",
                           "params": {"questions": [{"id": "first"}, {"id": "second"}]}})
        await self.app.command("/answer q1 first one")
        self.app.codex.reply.assert_not_awaited()
        await self.app.command("/answer q1 second two words")
        self.app.codex.reply.assert_awaited_once_with("q1", {"answers": {
            "first": {"answers": ["one"]}, "second": {"answers": ["two words"]}}})

    async def test_mcp_permission_waits_for_explicit_response(self):
        self.app.codex.reply = AsyncMock()
        self.app.on_event({"id": "mcp-1", "method": "mcpServer/elicitation/request",
                           "params": {"mode": "form", "message": "Allow Clock?",
                                      "requestedSchema": {"type": "object", "properties": {}}}})
        await asyncio.sleep(0)
        self.assertIn("mcp-1", self.app.requests)
        self.app.codex.reply.assert_not_awaited()

    async def test_noninteractive_permissions_are_declined(self):
        self.app.approval_policy = "on-request"
        self.app.interactive = False
        self.app.codex.reply = AsyncMock()
        self.app.on_event({"id": "approval", "method": "item/fileChange/requestApproval", "params": {}})
        await asyncio.sleep(0)
        self.app.codex.reply.assert_awaited_once_with("approval", {"decision": "decline"}, None)
        self.assertEqual(self.app.requests, {})


class CLITests(unittest.TestCase):
    def test_text_mode_explicitly_reports_that_microphone_is_off(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "wchat", "--text", "--codex", FAKE,
                                     "--data-dir", directory, "--cwd", directory, "--json"],
                                    input="/quit\n", capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            notices = [e for e in events if e["kind"] == "voice_disabled"]
            self.assertEqual(len(notices), 1)
            self.assertIn("--text", notices[0]["text"])
            self.assertFalse(any(e["kind"] == "voice_ready" for e in events))

    def test_real_cli_entry_streams_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "wchat", "--once", "entry",
                                     "--codex", FAKE, "--data-dir", directory, "--cwd", directory, "--json"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertTrue(any(e["kind"] == "delta" and e["text"] == "reply:entry" for e in events))
            with_store = Store(directory)
            try:
                self.assertEqual(with_store.jobs()[0]["response"], "reply:entry")
            finally:
                with_store.close()

    def test_default_folder_is_documents_jack(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "Documents/jack"
            expected.mkdir(parents=True)
            result = subprocess.run([sys.executable, "-m", "wchat", "--once", "entry",
                                     "--codex", FAKE, "--data-dir", str(Path(directory) / "state")],
                                    env={**os.environ, "HOME": directory},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            store = Store(Path(directory) / "state")
            try:
                self.assertEqual(store.session("default")["cwd"], str(expected.resolve()))
            finally:
                store.close()

    def test_api_key_auth_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "wchat", "--once", "entry",
                                     "--codex", FAKE, "--data-dir", directory, "--cwd", directory],
                                    env={**os.environ, "WCHAT_TEST_AUTH": "apiKey"},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("ChatGPT", result.stderr)


if __name__ == "__main__":
    unittest.main()
