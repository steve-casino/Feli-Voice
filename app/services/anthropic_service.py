"""Anthropic Claude streaming client.

Thin async wrapper that streams Claude's response token-by-token for a given
conversation history. The orchestrator pipes these tokens into ElevenLabs for
sentence-by-sentence TTS.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger(__name__)


class AnthropicService:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self._model = model or settings.anthropic_model

    async def stream_reply(
        self,
        system: str,
        messages: list[dict],
        *,
        max_tokens: int = 160,
        temperature: float = 0.6,
    ) -> AsyncIterator[str]:
        """Stream Claude's response as text deltas.

        `messages` is in standard Anthropic format:
            [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]

        Yields plain string deltas. Caller is responsible for accumulating
        them into sentences before handing off to TTS.

        Uses prompt caching on the system prompt so per-turn cost drops to
        ~10% of an uncached call after the first turn (the persona prompt
        is ~1k tokens and is identical every turn).
        """
        async with self._client.messages.stream(
            model=self._model,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text


# Module-level singleton.
anthropic_service = AnthropicService()
