"""OpenAI access for the P4 layer.

Team credits are on OpenAI (09 §5). The layer is optional by design: 05 §5
requires the template path and the whole demo to work with no key at all, so a
missing or failing key degrades to deterministic output instead of erroring.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def is_available() -> bool:
    return bool(get_settings().openai_api_key)


def complete_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 1200,
) -> dict[str, Any] | None:
    """One JSON-mode call. Returns None if the layer is unavailable or fails.

    None is a normal outcome, not an exception: every caller has a
    deterministic fallback and the demo must survive a dead key.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            seed=7,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        return json.loads(content) if content else None
    except Exception as exc:  # noqa: BLE001 - never break the demo path
        logger.warning("openai call failed, using deterministic fallback: %s", exc)
        return None
