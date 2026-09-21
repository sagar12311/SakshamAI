import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.planner_agent import PlannerAgent
from core.cognition_bus import CognitionMessage, MessageType


class AmazonPlannerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_commerce_turn_is_routed_before_any_model_prompt(self):
        commerce = SimpleNamespace(plan_turn=AsyncMock(return_value={
            "understood_intent": "Search Amazon.in products",
            "plan": [{"action": "amazon_search", "target": "headphones", "parameters": {"max_price_inr": 500}}],
            "commerce": True,
        }))
        planner = PlannerAgent()
        planner.ask_llm = AsyncMock()

        with patch("core.amazon_commerce.get_amazon_commerce_service", return_value=commerce):
            thought = await planner.think(CognitionMessage(
                type=MessageType.USER_INPUT,
                payload={"text": "find headphones under ₹500 on Amazon", "conversation_id": "commerce"},
                source="user",
            ))

        self.assertEqual("amazon_search", thought["plan"][0]["action"])
        self.assertEqual("commerce", thought["_conversation_id"])
        planner.ask_llm.assert_not_awaited()
        commerce.plan_turn.assert_awaited_once()

    async def test_completed_commerce_action_uses_deterministic_renderer(self):
        planner = PlannerAgent()
        planner._bus = SimpleNamespace(publish=AsyncMock())
        planner._get_conversation_history = AsyncMock(return_value=[])
        planner.ask_llm = AsyncMock()

        await planner._handle_task_complete(CognitionMessage(
            type=MessageType.TASK_COMPLETE,
            payload={
                "intent": "Open the selected Amazon product",
                "success": True,
                "conversation_id": "commerce",
                "results": [{
                    "success": True,
                    "result": {
                        "action": "amazon_open_product",
                        "product": {"title": "HP Z3700 Wireless Mouse"},
                        "trust": "untrusted_web",
                    },
                }],
            },
            source="executor",
        ))

        planner.ask_llm.assert_not_awaited()
        published = planner._bus.publish.await_args.args[0]
        self.assertIn("Opened HP Z3700 Wireless Mouse", published.payload["text"])

    def test_numbered_cards_are_announced_when_the_local_overlay_is_available(self):
        text, spoken = PlannerAgent._commerce_response_from_results([
            {"success": True, "result": {
                "action": "amazon_search", "screen_labels_shown": True,
                "products": [{"title": "Budget lamp", "price_inr": 299}],
            }},
        ], True)
        self.assertIn("Visible Amazon.in options", text)
        self.assertIn("numbered", spoken)


if __name__ == "__main__":
    unittest.main()
