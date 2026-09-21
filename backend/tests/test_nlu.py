import asyncio
import unittest

from core.dialogue_manager import DialogueManager
from core.nlu import Affect, NLUEngine, SpeechAct


class NLUEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.nlu = NLUEngine()

    def test_detects_english_continuation_and_reference(self) -> None:
        result = self.nlu.analyze("And also compare it with a banana")

        self.assertIn("and also", result.continuation_markers)
        self.assertIn("it", result.references)
        self.assertIn("compare", {intent.action for intent in result.intents})

    def test_detects_hinglish_continuation(self) -> None:
        result = self.nlu.analyze("Uske baad isko email bhi kar dena")

        self.assertIn("uske baad", result.continuation_markers)
        self.assertIn("isko", result.references)
        self.assertIn("send", {intent.action for intent in result.intents})

    def test_detects_multiple_intents(self) -> None:
        result = self.nlu.analyze(
            "Tell me the nutritional values of an apple and compare it with a banana"
        )
        actions = {intent.action for intent in result.intents}

        self.assertEqual(SpeechAct.QUESTION, result.speech_act)
        self.assertIn("explain", actions)
        self.assertIn("compare", actions)

    def test_related_follow_up_scores_above_threshold(self) -> None:
        previous = self.nlu.analyze("Tell me the nutritional values of an apple")
        current = self.nlu.analyze("And compare it with a banana")
        relationship = self.nlu.relationship(previous, current, gap_seconds=1.0)

        self.assertTrue(relationship.related)
        self.assertIn("continuation_marker", relationship.reasons)
        self.assertIn("reference_to_prior_context", relationship.reasons)

    def test_unrelated_request_stays_separate(self) -> None:
        previous = self.nlu.analyze("Tell me the nutritional values of an apple")
        current = self.nlu.analyze("Open Safari")
        relationship = self.nlu.relationship(previous, current, gap_seconds=1.0)

        self.assertFalse(relationship.related)

    def test_extracts_application_entity_and_frustrated_affect(self) -> None:
        result = self.nlu.analyze("This is frustrating, please open Safari")

        self.assertEqual(Affect.FRUSTRATED, result.affect)
        self.assertEqual(["Safari"], result.entities["application"])

    def test_detects_common_frustrated_misspelling(self) -> None:
        result = self.nlu.analyze("I am frustruated")

        self.assertEqual(Affect.FRUSTRATED, result.affect)

    def test_recovers_music_command_when_play_is_missing(self) -> None:
        result = self.nlu.analyze("Can you please some soothing songs for me?")
        actions = {intent.action for intent in result.intents}

        self.assertEqual(SpeechAct.COMMAND, result.speech_act)
        self.assertIn("media_control", actions)
        self.assertNotIn("answer", actions)

    def test_song_recommendation_remains_informational(self) -> None:
        result = self.nlu.analyze("Can you recommend some soothing songs for me?")
        actions = {intent.action for intent in result.intents}

        self.assertEqual(SpeechAct.QUESTION, result.speech_act)
        self.assertNotIn("media_control", actions)
        self.assertIn("answer", actions)

    def test_plain_greeting_is_not_treated_as_an_action(self) -> None:
        result = self.nlu.analyze("hi")

        self.assertEqual(SpeechAct.GREETING, result.speech_act)
        self.assertEqual((), result.intents)

    def test_identity_memory_question_is_informational_not_a_remember_command(self) -> None:
        result = self.nlu.analyze("Hey Saksham, this is Sagar. Do you remember me?")
        actions = {intent.action for intent in result.intents}

        self.assertEqual(SpeechAct.QUESTION, result.speech_act)
        self.assertNotIn("remember", actions)


class DialogueManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_related_fragments_commit_as_one_turn(self) -> None:
        committed = []

        async def capture(turn) -> None:
            committed.append(turn)

        manager = DialogueManager(
            settle_seconds=0.01,
            incomplete_settle_seconds=0.02,
            on_commit=capture,
        )

        await manager.note_audio_received("default", 1)
        await manager.note_audio_received("default", 2)
        await manager.submit_fragment(
            "default",
            "Tell me the nutritional values of an apple",
            1,
        )
        await manager.mark_audio_processed("default", 1)
        await asyncio.sleep(0.02)
        self.assertEqual([], committed)

        update = await manager.submit_fragment(
            "default",
            "And compare it with a banana",
            2,
        )
        await manager.mark_audio_processed("default", 2)
        await asyncio.sleep(0.04)

        self.assertEqual(2, update.fragment_count)
        self.assertEqual(1, len(committed))
        self.assertEqual(
            "Tell me the nutritional values of an apple, and compare it with a banana",
            committed[0].text,
        )
        await manager.shutdown()

    async def test_unrelated_fragments_commit_as_separate_turns(self) -> None:
        committed = []

        async def capture(turn) -> None:
            committed.append(turn.text)

        manager = DialogueManager(settle_seconds=0.01, on_commit=capture)
        await manager.note_audio_received("default", 1)
        await manager.note_audio_received("default", 2)
        await manager.submit_fragment("default", "Explain apples", 1)
        await manager.submit_fragment("default", "Open Safari", 2)
        await manager.mark_audio_processed("default", 2)
        await asyncio.sleep(0.04)

        self.assertEqual(["Explain apples", "Open Safari"], committed)
        await manager.shutdown()

    async def test_speech_onset_pauses_commit_before_audio_is_ready(self) -> None:
        committed = []

        async def capture(turn) -> None:
            committed.append(turn.text)

        manager = DialogueManager(settle_seconds=0.02, on_commit=capture)
        await manager.note_audio_received("default", 1)
        await manager.submit_fragment("default", "Explain apples", 1)
        await manager.mark_audio_processed("default", 1)

        await asyncio.sleep(0.01)
        await manager.note_speech_started("default")
        await asyncio.sleep(0.03)
        self.assertEqual([], committed)

        await manager.note_audio_received("default", 2)
        await manager.submit_fragment("default", "And compare them with bananas", 2)
        await manager.mark_audio_processed("default", 2)
        await asyncio.sleep(0.04)

        self.assertEqual(
            ["Explain apples, and compare them with bananas"],
            committed,
        )
        await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
