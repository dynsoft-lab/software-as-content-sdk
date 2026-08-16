"""
CodeProducer — the seam between SaC's protocol layer and code production.

SaC's core responsibility is conversation state, version chain, and the
generate/evolve state transitions. The actual "given an intent + optional
content + optional prior_app, produce a new App version" body is delegated
to a CodeProducer.

Pure rendering only. No search, no analyze, no agent-side concerns. Search
execution + suggestion generation live in `sac.agent` (or any external
agent driving core via /inbox).
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from sac.runtime.pipeline.evolve import evolve_pipeline, stream_evolve_pipeline, stream_evolve_pipeline_diff
from sac.runtime.pipeline.generate import generate_pipeline, stream_generate_pipeline
from sac.runtime.providers.base import LLMProvider
from sac.types import App, ConversationSettings, PipelineEvent


class CodeProducer(Protocol):
    """Strategy that turns (intent, content?, prior_app?) into a new App version."""

    async def produce(
        self,
        intent: str,
        prior_app: App | None,
        settings: ConversationSettings,
        model: str,
        version: int,
        content: str | None = None,
        original_intent: str | None = None,
    ) -> App: ...

    def stream(
        self,
        intent: str,
        prior_app: App | None,
        settings: ConversationSettings,
        model: str,
        version: int,
        content: str | None = None,
        original_intent: str | None = None,
    ) -> AsyncIterator[PipelineEvent]: ...


class DefaultCodeProducer:
    """Default producer — wraps the core (search-free) generate/evolve pipelines."""

    # Intents matching these patterns trigger full-code evolve instead of diff.
    # Diff evolve makes minimal edits, which compounds structural errors when
    # the source code is already broken (mismatched tags, syntax errors).
    _ERROR_FIX_KEYWORDS = ("fix", "error", "render", "transpile", "syntax", "bug")

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @staticmethod
    def _is_error_fix(intent: str) -> bool:
        """Detect intents that are fixing render/transpile errors."""
        low = intent.lower()
        return sum(1 for kw in DefaultCodeProducer._ERROR_FIX_KEYWORDS if kw in low) >= 2

    async def produce(
        self,
        intent: str,
        prior_app: App | None,
        settings: ConversationSettings,
        model: str,
        version: int,
        content: str | None = None,
        original_intent: str | None = None,
    ) -> App:
        if prior_app is None:
            return await generate_pipeline(
                intent=intent,
                model=model,
                llm=self._llm,
                settings=settings,
                version=version,
                content=content,
            )
        return await evolve_pipeline(
            new_intent=intent,
            current_code=prior_app.code,
            original_intent=original_intent or prior_app.intent,
            model=model,
            llm=self._llm,
            settings=settings,
            version=version,
            parent_version=prior_app.version,
            content=content,
        )

    def stream(
        self,
        intent: str,
        prior_app: App | None,
        settings: ConversationSettings,
        model: str,
        version: int,
        content: str | None = None,
        original_intent: str | None = None,
    ) -> AsyncIterator[PipelineEvent]:
        if prior_app is None:
            return stream_generate_pipeline(
                intent=intent,
                model=model,
                llm=self._llm,
                settings=settings,
                version=version,
                content=content,
            )
        # Error-fix intents use full-code evolve — diff on broken code compounds
        # structural errors (mismatched JSX tags, wrong nesting levels).
        if self._is_error_fix(intent):
            return stream_evolve_pipeline(
                new_intent=intent,
                current_code=prior_app.code,
                original_intent=original_intent or prior_app.intent,
                model=model,
                llm=self._llm,
                settings=settings,
                version=version,
                parent_version=prior_app.version,
                content=content,
            )
        return stream_evolve_pipeline_diff(
            new_intent=intent,
            current_code=prior_app.code,
            original_intent=original_intent or prior_app.intent,
            model=model,
            llm=self._llm,
            settings=settings,
            version=version,
            parent_version=prior_app.version,
            content=content,
        )
