"""
Anthropic LLM Provider

Direct integration with the Anthropic Messages API for users who have
an Anthropic API key and want to use Claude models without OpenRouter.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from sac.types import Message

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# reasoning_effort → extended-thinking budget. Must stay below max_tokens
# (16384 default); "none"/"default" omit thinking entirely.
_THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 8192}


def _pop_thinking(kwargs: dict) -> dict:
    effort = kwargs.pop("reasoning_effort", None)
    budget = _THINKING_BUDGETS.get(effort) if isinstance(effort, str) else None
    if budget:
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {}


MODEL_ALIASES = {
    "anthropic/claude-opus-4.5": "claude-opus-4-5-20250514",
    "anthropic/claude-sonnet-4.5": "claude-sonnet-4-5-20250514",
    "anthropic/claude-haiku-4.5": "claude-haiku-4-5-20251001",
}


class AnthropicProvider:
    """LLM provider backed by the Anthropic Messages API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=120.0)

    def _resolve_model(self, model: str) -> str:
        return MODEL_ALIASES.get(model, model)

    def _split_system(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        system = None
        msgs = []
        for m in messages:
            if m.role == "system":
                system = m.content
            else:
                msgs.append({"role": m.role, "content": m.content})
        return system, msgs

    async def complete(self, model: str, messages: list[Message], **kwargs: object) -> str:
        system, msgs = self._split_system(messages)
        body: dict = {
            "model": self._resolve_model(model),
            "messages": msgs,
            "max_tokens": kwargs.pop("max_tokens", 16384),
        }
        body.update(_pop_thinking(kwargs))
        if system:
            body["system"] = system
        body.update(kwargs)

        response = await self._client.post(
            ANTHROPIC_URL,
            headers=self._headers(),
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        return "".join(b["text"] for b in data["content"] if b["type"] == "text")

    async def stream(self, model: str, messages: list[Message], **kwargs: object) -> AsyncIterator[str]:
        system, msgs = self._split_system(messages)
        body: dict = {
            "model": self._resolve_model(model),
            "messages": msgs,
            "max_tokens": kwargs.pop("max_tokens", 16384),
            "stream": True,
        }
        body.update(_pop_thinking(kwargs))
        if system:
            body["system"] = system
        body.update(kwargs)

        async with self._client.stream(
            "POST",
            ANTHROPIC_URL,
            headers=self._headers(),
            json=body,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                    if event.get("type") == "content_block_delta":
                        text = event.get("delta", {}).get("text", "")
                        if text:
                            yield text
                except (json.JSONDecodeError, KeyError):
                    continue

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    async def close(self) -> None:
        await self._client.aclose()
