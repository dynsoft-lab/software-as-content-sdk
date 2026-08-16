"""
Provider Protocols

Structural typing interfaces for LLM and Search providers.
Any class implementing these methods is a valid provider — no inheritance needed.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from sac.types import Message, SearchResult


def reasoning_kwargs(effort: str | None) -> dict[str, object]:
    """Provider kwargs for a reasoning-effort setting.

    Returns {} for "default"/empty so providers that never see the kwarg
    behave exactly as before; each provider maps "none"/"low"/"medium"/"high"
    onto its own API shape.
    """
    if not effort or effort == "default":
        return {}
    return {"reasoning_effort": effort}


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM providers (OpenRouter, Anthropic, OpenAI, etc.)."""

    async def complete(self, model: str, messages: list[Message], **kwargs: object) -> str:
        """Send messages to the LLM and return the complete response."""
        ...

    async def stream(self, model: str, messages: list[Message], **kwargs: object) -> AsyncIterator[str]:
        """Send messages to the LLM and stream the response token by token."""
        ...


@runtime_checkable
class SearchProvider(Protocol):
    """Protocol for web search providers (Tavily, etc.)."""

    async def search(self, queries: list[str], **kwargs: object) -> list[SearchResult]:
        """Execute one or more search queries and return results."""
        ...
