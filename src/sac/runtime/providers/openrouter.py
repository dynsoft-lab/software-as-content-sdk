"""
OpenAI-compatible LLM Provider

Works with OpenRouter (default), OpenAI, Google, DeepSeek, xAI, ollama, vLLM —
any endpoint that speaks the OpenAI chat completions format.

Set SAC_API_BASE to override the endpoint URL.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from sac.types import Message

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _pop_reasoning(kwargs: dict) -> dict:
    """Translate the neutral reasoning_effort kwarg into OpenRouter's unified
    `reasoning` field. Models without reasoning support ignore it."""
    effort = kwargs.pop("reasoning_effort", None)
    if effort == "none":
        return {"reasoning": {"enabled": False}}
    if effort in ("low", "medium", "high"):
        return {"reasoning": {"effort": effort}}
    return {}


class OpenRouterProvider:
    """LLM provider for any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        referer: str = "https://fellou.ai",
        title: str = "Software as Content",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or OPENROUTER_URL
        self._referer = referer
        self._title = title
        self._client = httpx.AsyncClient(timeout=120.0)

    async def complete(self, model: str, messages: list[Message], **kwargs: object) -> str:
        """Send messages to the LLM and return the complete response."""
        extra = dict(kwargs)
        reasoning = _pop_reasoning(extra)
        response = await self._client.post(
            self._base_url,
            headers=self._headers(),
            json={
                "model": model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                **reasoning,
                **extra,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"] or ""

    async def stream(self, model: str, messages: list[Message], **kwargs: object) -> AsyncIterator[str]:
        """Send messages to the LLM and stream the response token by token."""
        extra = dict(kwargs)
        reasoning = _pop_reasoning(extra)
        async with self._client.stream(
            "POST",
            self._base_url,
            headers=self._headers(),
            json={
                "model": model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "stream": True,
                **reasoning,
                **extra,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": self._referer,
            "X-Title": self._title,
        }

    async def close(self) -> None:
        await self._client.aclose()
