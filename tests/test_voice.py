import unittest
from wchat.voice import WakeGate


class WakeTests(unittest.TestCase):
    def test_empty_wake_word_cannot_enable_all_speech(self):
        for text in ("", " ", "чат помощник"):
            with self.assertRaises(ValueError):
                WakeGate(text)

    def test_same_utterance_and_no_partial_execution(self):
        gate = WakeGate()
        self.assertEqual([e.kind for e in gate.feed("чат сколько", False, 1)], ["wake"])
        events = gate.feed("чат сколько будет два плюс два", True, 2)
        self.assertEqual([(e.kind, e.text) for e in events], [("prompt", "сколько будет два плюс два")])
        self.assertFalse(gate.armed)

    def test_pause_after_wake(self):
        gate = WakeGate()
        self.assertEqual(gate.feed("чат", True, 1)[0].kind, "wake")
        self.assertEqual(gate.feed("открой проект", True, 3)[0].text, "открой проект")

    def test_ordinary_speech_substring_and_mid_sentence_do_not_activate(self):
        for text in ["открой проект", "чатик открой", "мы обсуждали чат", "часть работы"]:
            self.assertEqual(WakeGate().feed(text, True, 1), [])

    def test_revised_partial_is_discarded(self):
        gate = WakeGate()
        gate.feed("чат", False, 1)
        self.assertEqual(gate.feed("часть работы", True, 2)[0].kind, "discard")
        self.assertEqual(gate.feed("удали файл", True, 3), [])

    def test_timeout_never_dispatches_a_later_unrelated_phrase(self):
        gate = WakeGate()
        gate.feed("чат", True, 1)
        self.assertEqual(gate.feed("удали файл", True, 20)[0].kind, "discard")
        self.assertEqual(gate.feed("ещё речь", True, 21), [])

    def test_stale_partial_does_not_rearm_on_timeout(self):
        gate = WakeGate()
        gate.feed("чат", False, 1)
        self.assertEqual([e.kind for e in gate.feed("чат", False, 20)], ["discard"])
        self.assertFalse(gate.armed)

    def test_case_and_punctuation(self):
        events = WakeGate().feed("ЧАТ, проверь файл!", True, 1)
        self.assertEqual(events[-1].text, "проверь файл!")

    def test_each_command_needs_its_own_wake(self):
        gate = WakeGate()
        gate.feed("чат первая задача", True, 1)
        self.assertEqual(gate.feed("вторая задача", True, 2), [])
        self.assertEqual(gate.feed("чат вторая задача", True, 3)[-1].text, "вторая задача")
