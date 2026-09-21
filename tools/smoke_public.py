"""Run inside the disposable smoke gateway; never against production."""
import asyncio
import os

import asyncpg
import httpx
from fastapi import HTTPException
from app.main import QuotaStore


async def expect_limit(awaitable):
    try:
        await awaitable
    except HTTPException as error:
        assert error.status_code == 429
    else:
        raise AssertionError("Expected the quota to reject this request")


async def verify():
    assert os.environ["DATABASE_URL"] == "postgresql://smoke:smoke-only-password@postgres:5432/smoke"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080") as client:
        assert (await client.get("/ready")).status_code == 200
        assert (await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})).status_code == 401
        assert (await client.post("/v1/meetings/transcribe", files={"audio": ("test.wav", b"test")})).status_code == 401
        for path in ("/api/system", "/v1/admin/verify", "/v1/embed", "/v1/diarize"):
            assert (await client.post(path)).status_code == 404
        response = await client.options("/v1/meetings/transcribe", headers={
            "Origin": "https://saksham.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-saksham-consent-confirmed",
        })
        assert response.status_code == 200
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    try:
        quota = QuotaStore(pool, 100, 150, 1, 2)
        # New rows must obey limits, just as existing rows do.
        await expect_limit(quota.reserve_tokens("fresh-too-large", 101))
        await expect_limit(quota.reserve_meeting("fresh-too-long", 61))
        # Transactions must roll back the user reservation if global admission fails.
        await quota.reserve_tokens("first", 80)
        await expect_limit(quota.reserve_tokens("second", 80))
        assert await pool.fetchval("SELECT COUNT(*) FROM saksham_daily_quota WHERE user_id='second'") == 0
        await quota.settle_tokens("first", 80, 20)
        await quota.reserve_tokens("second", 80)
        # Concurrent requests cannot exceed the remaining global allowance.
        results = await asyncio.gather(
            quota.reserve_tokens("racing", 40), quota.reserve_tokens("racing", 40),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        await quota.reserve_meeting("meeting", 60)
        await expect_limit(quota.reserve_meeting("meeting", 1))
        await quota.refund_meeting("meeting", 60)
        await quota.reserve_meeting("meeting", 60)
    finally:
        await pool.close()
    print("Smoke passed: container readiness, authentication, route isolation, CORS, and real Postgres quotas.")


asyncio.run(verify())
