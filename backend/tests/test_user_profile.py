import unittest

from core.user_profile import UserProfile


class FakeStateStore:
    def __init__(self) -> None:
        self.values = {}
        self.memories = []

    async def get_runtime_value(self, key: str):
        return self.values.get(key)

    async def set_runtime_value(self, key: str, value: str) -> None:
        self.values[key] = value

    async def create_memory(self, **memory):
        self.memories.append(memory)
        return {"id": str(len(self.memories)), **memory}


class FakeSemanticMemory:
    def __init__(self) -> None:
        self.facts = []

    async def store_fact(self, fact) -> None:
        self.facts.append(fact)


class UserProfileTests(unittest.IsolatedAsyncioTestCase):
    def test_identifies_only_pronunciation_related_instructions(self) -> None:
        self.assertTrue(UserProfile.is_pronunciation_instruction("Pronounce Sagar as Suh-gur"))
        self.assertTrue(UserProfile.is_pronunciation_instruction("My name is Sagar, pronounced Suh-gur"))
        self.assertTrue(UserProfile.is_pronunciation_instruction("Say Sagar as Suh-gur"))
        self.assertTrue(UserProfile.is_pronunciation_instruction("My name is Sagar"))
        self.assertFalse(UserProfile.is_pronunciation_instruction("Cancel the pending task"))

    async def test_explicit_pronunciation_correction_is_persisted_and_spoken(self) -> None:
        store = FakeStateStore()
        semantic_memory = FakeSemanticMemory()
        profile = UserProfile(store=store, semantic_memory=semantic_memory)

        update = await profile.learn_from_user_text("Pronounce Sagar as Sa-gar.")

        self.assertEqual("Sagar", update.written)
        self.assertEqual("Sa-gar", update.spoken)
        self.assertEqual("Hello, Sa-gar.", await profile.prepare_speech("Hello, Sagar."))
        self.assertEqual(1, len(store.memories))
        self.assertEqual("user_preference", store.memories[0]["memory_type"])
        self.assertEqual(1, len(semantic_memory.facts))
        self.assertEqual("user_preference", semantic_memory.facts[0].category)

    async def test_contextual_correction_uses_saved_name(self) -> None:
        store = FakeStateStore()
        profile = UserProfile(store=store)
        await profile.learn_from_user_text("My name is Sagar.")

        update = await profile.learn_from_user_text("Pronounce my name as Sa-gar.")

        self.assertEqual("Sagar", update.written)
        self.assertEqual("Sa-gar", update.spoken)
        self.assertEqual("Sa-gar is here.", await profile.prepare_speech("Sagar is here."))

    async def test_profile_is_reloaded_for_a_new_service_instance(self) -> None:
        store = FakeStateStore()
        profile = UserProfile(store=store)
        await profile.set_pronunciation("Sagar", "Sa-gar")

        restarted_profile = UserProfile(store=store)

        self.assertEqual(
            "Sa-gar is ready.",
            await restarted_profile.prepare_speech("Sagar is ready."),
        )

    async def test_greeting_introduction_persists_preferred_name(self) -> None:
        store = FakeStateStore()
        semantic_memory = FakeSemanticMemory()
        profile = UserProfile(store=store, semantic_memory=semantic_memory)

        await profile.learn_from_user_text(
            "Hey Saksham, this is Sagar. Do you remember me?"
        )

        self.assertEqual("Sagar", (await profile.get_context())["preferred_name"])
        self.assertEqual("preferred_name", store.memories[0]["metadata"]["kind"])
        self.assertEqual("user_preference", semantic_memory.facts[0].category)

    async def test_emotional_statement_is_not_mistaken_for_a_name(self) -> None:
        store = FakeStateStore()
        profile = UserProfile(store=store)

        await profile.learn_from_user_text("I am frustrated")

        self.assertEqual("", (await profile.get_context())["preferred_name"])


if __name__ == "__main__":
    unittest.main()
