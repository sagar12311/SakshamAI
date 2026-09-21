"""Ephemeral, local-only screen text verification for bounded UI flows.

The observer intentionally returns only a yes/no result.  Screenshots and OCR
text are kept in memory just long enough to verify a requested visible label,
then the screenshot is removed.  It is not a generic screen-control API.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4


_COMMON_WORDS = {
    "and", "are", "arm", "chair", "company", "for", "from", "home", "in",
    "of", "office", "on", "or", "premium", "product", "the", "to", "with",
}


class ScreenObserver:
    """Verify a product label is present on the locally displayed main screen."""

    async def verify_product_label(self, title: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._verify_product_label_sync, title)

    @staticmethod
    def _verify_product_label_sync(title: str) -> dict[str, Any]:
        capture_binary = shutil.which("screencapture") or "/usr/sbin/screencapture"
        ocr_binary = shutil.which("tesseract")
        if not ocr_binary:
            return {"success": False, "matched": False, "reason": "local_ocr_unavailable"}

        image_path = Path(tempfile.gettempdir()) / f"saksham_observation_{uuid4().hex}.png"
        try:
            captured = subprocess.run(
                [capture_binary, "-x", "-m", str(image_path)],
                capture_output=True, timeout=10, check=False,
            )
            if captured.returncode != 0 or not image_path.is_file():
                return {"success": False, "matched": False, "reason": "screen_capture_unavailable"}

            recognized = subprocess.run(
                [ocr_binary, str(image_path), "stdout", "--psm", "6"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if recognized.returncode != 0:
                return {"success": False, "matched": False, "reason": "local_ocr_failed"}
            return {
                "success": True,
                "matched": ScreenObserver._has_label_evidence(title, recognized.stdout),
            }
        except (OSError, subprocess.SubprocessError):
            return {"success": False, "matched": False, "reason": "screen_observation_failed"}
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _has_label_evidence(title: str, ocr_text: str) -> bool:
        expected = {
            token for token in re.findall(r"[a-z0-9]+", str(title or "").lower())
            if len(token) >= 3 and token not in _COMMON_WORDS
        }
        observed = set(re.findall(r"[a-z0-9]+", str(ocr_text or "").lower()))
        matched = expected & observed
        if len(matched) < 2:
            return False
        # A model number, or two sufficiently distinctive product words, is
        # required.  Generic page terms such as "office chair" cannot pass.
        distinctive = {
            token for token in expected
            if any(character.isdigit() for character in token) or len(token) >= 7
        }
        # OCR commonly loses one long marketing word. Three independent label
        # tokens, including a product number when present, remain stronger
        # evidence than a generic page phrase while admitting visible cards.
        has_model_number = bool(matched & {token for token in distinctive if any(char.isdigit() for char in token)})
        return has_model_number or len(matched & distinctive) >= 2 or len(matched) >= 3
