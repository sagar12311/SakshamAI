
import asyncio
import sys
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_memory():
    print("Testing VectorStore...")
    try:
        from memory.vector_store import VectorStore
        vs = VectorStore()
        await vs.initialize()
        print("Initialized.")
        
        print("Storing...")
        await vs.store("This is a test memory", {"type": "test"}, "test_collection")
        print("Stored.")
        
        print("Searching...")
        results = await vs.search("test", "test_collection")
        print(f"Results: {results}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_memory())
