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
    schema: dict[str, Any] | None = None,
    schema_name: str = "response",
) -> dict[str, Any] | None:
    """One structured call. Returns None if the layer is unavailable or fails.

    With `schema` the model is held to it by the API (strict Structured
    Outputs), which removes a whole class of failure — a missing field or an
    invented one — before the response is even parsed. Without it this falls
    back to plain JSON mode.

    None is a normal outcome, not an exception: every caller has a
    deterministic fallback and the demo must survive a dead key. A schema the
    account cannot honour retries once without it rather than losing the call.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return None

    response_format: dict[str, Any] = (
        {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
        if schema
        else {"type": "json_object"}
    )

    content = _call(settings, system_prompt, user_prompt, response_format, max_tokens)
    if content is None and schema is not None:
        logger.warning("structured output rejected, retrying in plain JSON mode")
        content = _call(
            settings, system_prompt, user_prompt, {"type": "json_object"}, max_tokens
        )

    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("model returned unparsable JSON: %s", exc)
        return None


def _call(
    settings,
    system_prompt: str,
    user_prompt: str,
    response_format: dict[str, Any],
    max_tokens: int,
) -> str | None:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=response_format,
            temperature=0,
            seed=7,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content
    except Exception as exc:  # noqa: BLE001 - never break the demo path
        logger.warning("openai call failed: %s", exc)
        return None
