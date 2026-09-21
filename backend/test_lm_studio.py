
import asyncio
from openai import AsyncOpenAI
from config import get_settings

settings = get_settings()

async def test_connection():
    print(f"Testing connection to: {settings.llm_base_url}")
    print(f"API Key: {settings.llm_api_key.get_secret_value()}")
    print(f"Model: {settings.llm_model}")
    
    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value()
    )
    
    try:
        print("1. Listing models...")
        models = await client.models.list()
        print(f"✅ Found {len(models.data)} models.")
        for m in models.data:
            print(f" - {m.id}")
            
        print(f"\n2. Testing completion with model '{settings.llm_model}'...")
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "Hello, are you there?"}],
            max_tokens=50
        )
        print(f"✅ Response received:\n{response.choices[0].message.content}")
        
    except Exception as e:
        print(f"❌ Connection Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
