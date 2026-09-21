import unittest

from core.voice_processor import is_barge_in_stop


class VoiceControlTests(unittest.TestCase):
    def test_recognizes_direct_stop_requests(self) -> None:
        for phrase in (
            "stop",
            "Stop reading now.",
            "hold on",
            "Saksham stop please",
            "wait a moment",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_barge_in_stop(phrase))

    def test_rejects_non_interruptions_and_long_echoes(self) -> None:
        for phrase in (
            "please continue",
            "tell me about stop motion animation",
            "the bus will stop near the station after five minutes",
            "",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_barge_in_stop(phrase))


if __name__ == "__main__":
    unittest.main()
