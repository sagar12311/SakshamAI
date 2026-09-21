import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Request
from starlette.responses import Response

from core.origin_policy import allowed_origins, is_trusted_browser_origin
import main


class OriginPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173/",
            "*",
        ]

    def test_accepts_only_explicit_local_frontend_origins(self) -> None:
        self.assertTrue(is_trusted_browser_origin("http://localhost:5173", self.origins))
        self.assertTrue(is_trusted_browser_origin("http://127.0.0.1:5173", self.origins))

    def test_rejects_wildcard_and_untrusted_origins(self) -> None:
        self.assertNotIn("*", allowed_origins(self.origins))
        self.assertFalse(is_trusted_browser_origin("https://example.test", self.origins))
        self.assertFalse(is_trusted_browser_origin("null", self.origins))
        self.assertFalse(is_trusted_browser_origin(None, self.origins))


class OriginMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(origin: str) -> Request:
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/api/security/admin/code",
            "headers": [(b"origin", origin.encode("ascii"))],
        })

    async def test_foreign_browser_request_is_rejected_before_the_api_handler(self) -> None:
        handler = AsyncMock(return_value=Response(status_code=200))
        with patch.object(main.settings, "cors_origins", ["http://localhost:5173"]):
            response = await main.reject_untrusted_browser_origin(
                self._request("https://example.test"),
                handler,
            )
        self.assertEqual(403, response.status_code)
        handler.assert_not_awaited()

    async def test_trusted_frontend_request_reaches_the_api_handler(self) -> None:
        expected = Response(status_code=204)
        handler = AsyncMock(return_value=expected)
        with patch.object(main.settings, "cors_origins", ["http://localhost:5173"]):
            response = await main.reject_untrusted_browser_origin(
                self._request("http://localhost:5173"),
                handler,
            )
        self.assertIs(expected, response)
        handler.assert_awaited_once()
