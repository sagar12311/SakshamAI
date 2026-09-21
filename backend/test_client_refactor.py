
import asyncio
import sys
# Add current directory to path so we can import backend modules
sys.path.append('.')

from core.llm_client import get_llm_client

async def test_llm_client():
    print("🤖 Initializing LLMClient...")
    client = get_llm_client()
    
    print("💬 Testing Chat...")
    try:
        response = await client.chat(
            messages=[{"role": "user", "content": "Say hello from LLMClient!"}]
        )
        print(f"✅ Chat Response: {response['choices'][0]['message']['content']}")
    except Exception as e:
        print(f"❌ Chat Failed: {e}")
        
    print("\n🌊 Testing Stream...")
    try:
        print("Stream Output: ", end="", flush=True)
        async for chunk in client.stream(
            messages=[{"role": "user", "content": "Count to 3."}]
        ):
            print(chunk, end="", flush=True)
        print("\n✅ Stream Completed")
    except Exception as e:
        print(f"\n❌ Stream Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_llm_client())
