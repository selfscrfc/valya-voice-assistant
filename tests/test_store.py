from pathlib import Path
import tempfile
import unittest
from wchat.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.create("default", self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_existing_data_and_thread_survive_restart_without_replay(self):
        self.store.thread("default", "saved-thread")
        done = self.store.enqueue("default", "old", "text")
        self.store.update(done, status="completed", response="preserved")
        running = self.store.enqueue("default", "doing", "voice")
        self.store.update(running, status="running")
        queued = self.store.enqueue("default", "later", "voice")
        self.store.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.store.session("default")["thread_id"], "saved-thread")
        self.assertEqual(self.store.job(done)["response"], "preserved")
        self.assertEqual(self.store.job(running)["status"], "unknown")
        self.assertEqual(self.store.job(queued)["status"], "needs_review")

    def test_session_switch_does_not_retarget_existing_job(self):
        job = self.store.enqueue("default", "first", "voice")
        self.store.create("other", self.tmp.name)
        self.assertEqual(self.store.job(job)["session"], "default")
        self.assertEqual(self.store.active, "other")
        self.assertIsNone(self.store.session("other")["thread_id"])

    def test_second_process_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "другой"):
            Store(self.tmp.name)

    def test_invalid_session_cannot_replace_existing(self):
        for name in ["default", "../bad", "", "two words"]:
            with self.assertRaises(ValueError):
                self.store.create(name, self.tmp.name)
        self.assertEqual(len(self.store.sessions()), 1)

    def test_invalid_workspace(self):
        with self.assertRaises(ValueError):
            self.store.create("missing", Path(self.tmp.name) / "missing")
