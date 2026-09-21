"""Small, deterministic origin checks for Saksham's loopback API.

The desktop frontend is the only browser client that should be able to call
state-changing local endpoints.  CORS alone protects response readability,
not every possible request, so HTTP routes also reject an explicit untrusted
browser origin before reaching an API handler.  Native/local tooling without
an ``Origin`` header remains available for development and diagnostics.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional


def _normalise(origin: str) -> str:
    return origin.strip().rstrip("/")


def allowed_origins(origins: Iterable[str]) -> frozenset[str]:
    """Return exact, non-wildcard browser origins configured for this app."""
    return frozenset(
        candidate
        for item in origins
        if (candidate := _normalise(str(item))) and candidate != "*"
    )


def is_trusted_browser_origin(origin: Optional[str], origins: Iterable[str]) -> bool:
    """Match an explicit browser origin exactly; never treat ``*`` as trusted."""
    if not origin:
        return False
    return _normalise(origin) in allowed_origins(origins)
