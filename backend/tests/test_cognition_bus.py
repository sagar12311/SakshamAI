import asyncio
import unittest

from core.cognition_bus import CognitionBus, CognitionMessage, MessageType


class _InspectingQueue:
    def __init__(self, bus: CognitionBus) -> None:
        self.bus = bus
        self.response_channel_ready = asyncio.Event()

    async def put(self, item) -> None:
        _, _, message = item
        if message.correlation_id in self.bus._pending_responses:
            self.response_channel_ready.set()


class CognitionBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_channel_exists_before_message_is_queued(self) -> None:
        bus = CognitionBus()
        queue = _InspectingQueue(bus)
        bus._message_queue = queue
        message = CognitionMessage(
            type=MessageType.MEMORY_RECALL,
            payload={"query": "Sagar"},
            source="planner",
            target="memory",
            requires_response=True,
        )

        publish_task = asyncio.create_task(bus.publish(message))
        await asyncio.wait_for(queue.response_channel_ready.wait(), timeout=1.0)
        await bus.respond(message.correlation_id, {"memories": []})

        self.assertEqual({"memories": []}, await publish_task)
        self.assertNotIn(message.correlation_id, bus._pending_responses)


if __name__ == "__main__":
    unittest.main()
