import unittest

from core.integration_lab import IntegrationLab


class FakeStateStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get_runtime_value(self, key: str):
        return self.values.get(key)

    async def set_runtime_value(self, key: str, value: str) -> None:
        self.values[key] = value


class IntegrationLabTests(unittest.IsolatedAsyncioTestCase):
    async def test_apple_music_proposal_is_chat_only_and_includes_research(self) -> None:
        lab = IntegrationLab(FakeStateStore())

        proposal = await lab.propose_from_chat("Research and propose an Apple Music integration")

        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal["capability"], "apple_music_catalog")
        self.assertTrue(proposal["safety"]["chat_only"])
        self.assertFalse(proposal["safety"]["auto_apply"])
        self.assertEqual(len(proposal["research"]["sources"]), 3)

    async def test_feedback_is_persisted_and_changes_review_status(self) -> None:
        store = FakeStateStore()
        lab = IntegrationLab(store)
        proposal = await lab.create_proposal("Research and propose a calendar connector")

        updated = await lab.add_feedback(proposal["id"], approved=False, feedback="Need calendar write permissions")

        assert updated is not None
        self.assertEqual(updated["status"], "needs_revision")
        self.assertEqual(updated["feedback"][0]["comment"], "Need calendar write permissions")

        reloaded = IntegrationLab(store)
        proposals = await reloaded.list_proposals()
        self.assertEqual(proposals[0]["id"], proposal["id"])

    async def test_normal_chat_does_not_create_a_proposal(self) -> None:
        lab = IntegrationLab(FakeStateStore())

        proposal = await lab.propose_from_chat("Play some ambient music")

        self.assertIsNone(proposal)


if __name__ == "__main__":
    unittest.main()
