"""
StandaloneAgent — the default agent that ships with SaC for standalone use.

Sibling-level to external agent systems (OpenClaw, LangGraph, Claude Agent
SDK, ...): they all interact with SaC's pure interaction core via the same
protocol contract. This one just happens to live in the same Python process
for convenience and for the standalone web UI use case.

Owns:
  - search execution (extract queries → call SearchProvider → format results)
  - intent suggestion generation (TODO: belongs in core per boundary rule;
    deferred until the OpenClaw-adapter milestone, when external-agent /inbox
    flows force a real design for content-grounded suggestions)
  - event recording around generate/evolve flows

Calls into core via `Conversation.ingest(content, intent)` for the actual
rendering. Search results, when present, are formatted into a content string
and passed via the same content path that any external agent would use —
there is no privileged in-process API.

NOTE: This class's method shape (generate / evolve / stream) is NOT a protocol
requirement. External agents only need to: (1) POST /inbox with `response`,
(2) receive POST {callback_url} with `intent`. Roughly 30 lines of HTTP.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, TYPE_CHECKING

from sac.agent.prompts.intent import (
    get_intent_suggestion_prompt,
    parse_intent_suggestions,
)
from sac.agent.prompts.search import get_search_query_extraction_prompt
from sac.runtime.providers.base import LLMProvider, SearchProvider
from sac.types import (
    App,
    EventStatus,
    GenerationEvent,
    GrowthEvent,
    IntentSuggestion,
    Message,
    MessageEvent,
    PipelineCompleteEvent,
    PipelineErrorEvent,
    PipelineEvent,
    PipelineSearchEvent,
    PipelineStageEvent,
    SearchQuery,
    SearchResult,
    StageStatus,
)

if TYPE_CHECKING:
    from sac.conversation import Conversation


class StandaloneAgent:
    """The default standalone agent. One per SaC instance; shared across conversations."""

    def __init__(self, llm: LLMProvider, search: SearchProvider | None) -> None:
        self._llm = llm
        self._search = search

    # ─── Public methods ──────────────────────────────────────────────

    async def generate(self, conv: "Conversation", intent: str, **opts: Any) -> App:
        model = str(opts.get("model", conv.model))
        web_search_opt = opts.get("web_search", conv.settings.enable_web_search)

        await conv._store.add_event(
            MessageEvent(conversation_id=conv.id, role="user", content=intent)
        )

        # Standalone agentic flow: search (if enabled) + suggestions
        enable_search = bool(web_search_opt) and self._search is not None

        search_queries: list[SearchQuery] = []
        search_results: list[SearchResult] = []
        composed_content: str | None = None

        if enable_search:
            try:
                search_queries = await _extract_search_queries(intent, model, self._llm, _conversation_context(conv))
                if search_queries:
                    qs = [q.query for q in search_queries]
                    search_results = await self._search.search(qs)  # type: ignore[union-attr]
                    composed_content = _format_search_results_as_data(search_results)
            except Exception:
                search_queries, search_results, composed_content = [], [], None

        try:
            app = await conv.ingest(content=composed_content, intent=intent, model=model)

            suggestions = await _generate_intent_suggestions(
                intent, search_results, model, self._llm, conv.settings.intent_rules
            )

            app.search_queries = search_queries
            app.search_results = search_results
            app.suggestions = suggestions

            await conv._store.add_event(
                GenerationEvent(
                    conversation_id=conv.id,
                    intent=intent,
                    model=model,
                    status=EventStatus.SUCCESS,
                    code=app.code,
                    stages=app.stages,
                    search_queries=search_queries or None,
                    search_results=search_results or None,
                    intent_suggestions=suggestions or None,
                )
            )

            if conv.version == 1:
                title = intent[:80] + ("..." if len(intent) > 80 else "")
                await conv._store.update_conversation(conv.id, title=title)

            return app
        except Exception as exc:
            await conv._store.add_event(
                GenerationEvent(
                    conversation_id=conv.id,
                    intent=intent,
                    model=model,
                    status=EventStatus.ERROR,
                    error=str(exc),
                )
            )
            raise

    async def evolve(self, conv: "Conversation", intent: str, **opts: Any) -> App:
        if conv.current_app is None:
            raise ValueError("No app to evolve. Call generate() first.")

        model = str(opts.get("model", conv.model))

        await conv._store.add_event(
            MessageEvent(conversation_id=conv.id, role="user", content=intent)
        )

        search_queries: list[SearchQuery] = []
        search_results: list[SearchResult] = []
        composed_content: str | None = None

        web_search_opt = opts.get("web_search", conv.settings.enable_web_search)
        if self._search is not None and bool(web_search_opt):
            try:
                search_queries = await _extract_search_queries(intent, model, self._llm, _conversation_context(conv))
                if search_queries:
                    qs = [q.query for q in search_queries]
                    search_results = await self._search.search(qs)
                    composed_content = _format_search_results_as_data(search_results)
            except Exception:
                search_queries, search_results, composed_content = [], [], None

        try:
            app = await conv.ingest(content=composed_content, intent=intent, model=model)

            suggestions = await _generate_intent_suggestions(
                intent, search_results, model, self._llm, conv.settings.intent_rules
            )

            app.search_queries = search_queries
            app.search_results = search_results
            app.suggestions = suggestions

            await conv._store.add_event(
                GrowthEvent(
                    conversation_id=conv.id,
                    intent=intent,
                    model=model,
                    status=EventStatus.SUCCESS,
                    code=app.code,
                    stages=app.stages,
                    search_queries=search_queries or None,
                    search_results=search_results or None,
                    intent_suggestions=suggestions or None,
                )
            )

            return app
        except Exception as exc:
            await conv._store.add_event(
                GrowthEvent(
                    conversation_id=conv.id,
                    intent=intent,
                    model=model,
                    status=EventStatus.ERROR,
                    error=str(exc),
                )
            )
            raise

    async def stream(
        self, conv: "Conversation", intent: str, **opts: Any
    ) -> AsyncIterator[PipelineEvent]:
        """Streaming variant. Yields stage/chunk/complete events."""
        from sac.runtime.pipeline.events import PipelineEmitter

        model = str(opts.get("model", conv.model))
        is_evolve = conv.current_app is not None
        web_search_opt = opts.get("web_search", conv.settings.enable_web_search)
        agent_emitter = PipelineEmitter()

        await conv._store.add_event(
            MessageEvent(conversation_id=conv.id, role="user", content=intent)
        )

        search_queries: list[SearchQuery] = []
        search_results: list[SearchResult] = []
        composed_content: str | None = None
        # Evolve respects the web_search setting too; the query-extraction
        # prompt may also return zero queries for requests that need no
        # external data (pure UI tweaks), which skips the search entirely.
        run_search = self._search is not None and bool(web_search_opt)

        if run_search:
            if not is_evolve:
                agent_emitter.start("analyze")
                yield PipelineStageEvent(name="analyze", status=StageStatus.RUNNING)
                try:
                    search_queries = await _extract_search_queries(intent, model, self._llm, _conversation_context(conv))
                    agent_emitter.complete("analyze")
                    yield PipelineStageEvent(name="analyze", status=StageStatus.COMPLETED)
                except Exception as exc:
                    agent_emitter.error("analyze")
                    yield PipelineStageEvent(name="analyze", status=StageStatus.ERROR)
                    yield PipelineErrorEvent(error=str(exc))
                    await self._record_error_event(conv, is_evolve, intent, model, str(exc))
                    return
            else:
                agent_emitter.start("search")
                yield PipelineStageEvent(name="search", status=StageStatus.RUNNING)
                try:
                    search_queries = await _extract_search_queries(intent, model, self._llm, _conversation_context(conv))
                except Exception:
                    pass

            if search_queries:
                if not is_evolve:
                    agent_emitter.start("search")
                    yield PipelineStageEvent(name="search", status=StageStatus.RUNNING)
                try:
                    qs = [q.query for q in search_queries]
                    search_results = await self._search.search(qs)  # type: ignore[union-attr]
                    agent_emitter.complete("search")
                    yield PipelineStageEvent(name="search", status=StageStatus.COMPLETED)
                    if search_results:
                        yield PipelineSearchEvent(queries=search_queries, results=search_results)
                except Exception as exc:
                    agent_emitter.error("search")
                    yield PipelineStageEvent(name="search", status=StageStatus.ERROR)
                    if not is_evolve:
                        yield PipelineErrorEvent(error=str(exc))
                        await self._record_error_event(conv, is_evolve, intent, model, str(exc))
                        return
            elif is_evolve:
                agent_emitter.complete("search")
                yield PipelineStageEvent(name="search", status=StageStatus.COMPLETED)

            if search_results:
                composed_content = _format_search_results_as_data(search_results)

        suggest_task = asyncio.create_task(
            _generate_intent_suggestions(
                intent, search_results, model, self._llm, conv.settings.intent_rules
            )
        )

        try:
            async for event in conv.stream_ingest(content=composed_content, intent=intent, model=model):
                if isinstance(event, PipelineCompleteEvent):
                    suggestions = await suggest_task
                    event.app.search_queries = search_queries
                    event.app.search_results = search_results
                    event.app.suggestions = suggestions
                    # Merge agent-level stages (analyze, search) before core stages (generate)
                    all_stages = agent_emitter.stages + (event.app.stages or [])
                    event.app.stages = all_stages

                    event_cls = GrowthEvent if is_evolve else GenerationEvent
                    await conv._store.add_event(
                        event_cls(
                            conversation_id=conv.id,
                            intent=intent,
                            model=model,
                            status=EventStatus.SUCCESS,
                            code=event.app.code,
                            stages=all_stages,
                            search_queries=search_queries or None,
                            search_results=search_results or None,
                            intent_suggestions=suggestions or None,
                        )
                    )
                    if not is_evolve and conv.version == 1:
                        title = intent[:80] + ("..." if len(intent) > 80 else "")
                        await conv._store.update_conversation(conv.id, title=title)

                yield event
        except Exception as exc:
            suggest_task.cancel()
            await self._record_error_event(conv, is_evolve, intent, model, str(exc))
            yield PipelineErrorEvent(error=str(exc))

    # ─── Helpers ──────────────────────────────────────────────────────

    async def _record_error_event(
        self,
        conv: "Conversation",
        is_evolve: bool,
        intent: str,
        model: str,
        error: str,
    ) -> None:
        event_cls = GrowthEvent if is_evolve else GenerationEvent
        await conv._store.add_event(
            event_cls(
                conversation_id=conv.id,
                intent=intent,
                model=model,
                status=EventStatus.ERROR,
                error=error,
            )
        )


# ─── Module-level helpers (no class state needed) ──────────────────


def _conversation_context(conv: "Conversation") -> str | None:
    """Compact conversation context for disambiguating follow-up intents.

    Follow-ups are often elliptical ("I need to stay in CBD", "cheaper
    options") — interpreted standalone they get globally re-resolved by
    search. Title (first intent) + recent intents anchor them to the
    conversation's actual topic.
    """
    parts: list[str] = []
    if conv.title:
        parts.append(f"Conversation topic: {conv.title}")
    intents = [a.intent for a in conv.history if a.intent]
    if intents:
        parts.append("Previous requests: " + " -> ".join(intents[-3:]))
    return "\n".join(parts) or None


async def _extract_search_queries(
    intent: str, model: str, llm: LLMProvider, context: str | None = None
) -> list[SearchQuery]:
    """Extract search queries from user intent via LLM."""
    search_prompt = get_search_query_extraction_prompt()
    user_content = (
        f"CONVERSATION CONTEXT:\n{context}\n\nNEW REQUEST: {intent}"
        if context
        else intent
    )
    response = await llm.complete(
        model,
        [
            Message(role="system", content=search_prompt),
            Message(role="user", content=user_content),
        ],
    )
    try:
        json_str = response
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
        if match:
            json_str = match.group(1)
        parsed = json.loads(json_str.strip())
        return [SearchQuery(**q) for q in parsed.get("queries", [])]
    except (json.JSONDecodeError, KeyError, ValueError):
        return [SearchQuery(query=intent, purpose="main search")]


async def _generate_intent_suggestions(
    intent: str,
    search_results: list[SearchResult],
    model: str,
    llm: LLMProvider,
    custom_rules: str | None = None,
) -> list[IntentSuggestion]:
    """Generate intent suggestions (non-critical, returns empty on failure).

    TODO(boundary): per the SaC boundary rule, content-grounded suggestions
    belong in core (`Conversation.ingest`) so that any agent posting to /inbox
    gets them for free. Currently lives here because the prompt expects a
    `list[SearchResult]` shape; making it content-grounded is a small prompt
    redesign that should land alongside the OpenClaw-adapter milestone, when
    real external-agent content gives us the input shape to design against.
    """
    try:
        prompt = get_intent_suggestion_prompt(intent, search_results, custom_rules)
        response = await llm.complete(model, [Message(role="user", content=prompt)])
        return parse_intent_suggestions(response)
    except Exception:
        return []


def _format_search_results_as_data(search_results: list[SearchResult]) -> str:
    """Format search results into a content string for the renderer's data context.

    Mirrors the rich source/image format previously inlined in the
    pre-refactor `build_search_context_prompt`, minus the wrapper. The
    renderer's unified `build_data_context_prompt` will wrap this once with
    generic instructions.
    """
    sections: list[str] = []
    for result in search_results:
        sources = "\n\n".join(
            f"  {i + 1}. {s.title}\n     {s.content}"
            for i, s in enumerate(result.sources)
        )
        images_section = ""
        if result.images:
            images_section = (
                f"\nImages ({len(result.images)} available):\n"
                + "\n".join(f"  {i + 1}. {img}" for i, img in enumerate(result.images))
            )
        section = f'### Search: "{result.query}"'
        if result.answer:
            section += f"\nSummary: {result.answer}\n"
        section += f"\nSources:\n{sources}{images_section}"
        sections.append(section)
    return "\n\n---\n\n".join(sections)
