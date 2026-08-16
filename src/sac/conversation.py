"""
Conversation — SaC's pure-interaction-layer primitive.

Owns: id, version chain, history, callback_url, settings.
Owns: `ingest()` — the single core entry that turns (content, intent) into
       a new App version via the CodeProducer.

Does NOT own: search, classify, intent suggestions, send/dispatch — those
are agent-layer concerns. Legacy `generate / evolve / stream / send /
classify` methods are kept as thin DELEGATE shims:
  - generate / evolve / stream → StandaloneAgent (real default agent)
  - send / classify           → LegacyShim (transitional, removed when
                                sac-web + MCP migrate to /inbox protocol)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator, Optional

from sac.runtime.producer import CodeProducer
from sac.runtime.prompts.app import DEFAULT_MODEL
from sac.runtime.providers.base import LLMProvider
from sac.runtime.store.base import ConversationStore
from sac.types import (
    App,
    ConversationData,
    ConversationSettings,
    EventStatus,
    PipelineCompleteEvent,
    PipelineEvent,
    SendResult,
)

if TYPE_CHECKING:
    from sac.agent.agent import StandaloneAgent
    from sac.agent.legacy import LegacyShim


class Conversation:
    """
    A stateful conversation that produces and evolves App versions.

    Direct (core) usage:
        conv = sac.conversation()
        app = await conv.ingest(content="...", intent="...")

    Agent-shaped usage — generate/evolve/stream go through StandaloneAgent;
    send/classify go through LegacyShim:
        app = await conv.generate("travel guide for Hangzhou")
        app = await conv.evolve("add restaurants")
        result = await conv.send("looks great!")
    """

    def __init__(
        self,
        data: ConversationData,
        llm: LLMProvider,
        producer: CodeProducer,
        store: ConversationStore,
        standalone_agent: Optional["StandaloneAgent"] = None,
        legacy_shim: Optional["LegacyShim"] = None,
    ) -> None:
        self._data = data
        self._llm = llm
        self._producer = producer
        self._store = store
        self._standalone_agent = standalone_agent
        self._legacy_shim = legacy_shim
        self._apps: list[App] = []

    async def _load_from_store(self) -> None:
        """Load conversation state from store, rebuilding _apps from events."""
        stored = await self._store.get_conversation(self._data.id)
        if not stored:
            return

        self._data.title = stored.title
        self._data.model = stored.model or self._data.model
        if stored.settings:
            self._data.settings = stored.settings

        events = await self._store.get_events(self._data.id)
        self._apps = []
        for event in events:
            if hasattr(event, 'code') and event.code and hasattr(event, 'status'):
                if event.status == EventStatus.SUCCESS:
                    version = len(self._apps) + 1
                    intent = event.intent if hasattr(event, 'intent') else ""
                    self._apps.append(App(
                        version=version,
                        intent=intent,
                        code=event.code,
                        model=event.model if hasattr(event, 'model') else "",
                    ))

    # ─── Properties ────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return self._data.id

    @property
    def title(self) -> str:
        return self._data.title

    @property
    def settings(self) -> ConversationSettings:
        return self._data.settings

    @property
    def model(self) -> str:
        return self._data.model or DEFAULT_MODEL

    @property
    def current_app(self) -> App | None:
        return self._apps[-1] if self._apps else None

    @property
    def history(self) -> list[App]:
        return list(self._apps)

    @property
    def version(self) -> int:
        return len(self._apps)

    def _intent_context(self) -> str | None:
        """Conversation-level intent for the evolve prompt's "Original Intent".

        Passing only the prior app's intent makes memory one-step deep — after
        a few evolves the original topic is gone. Anchor on the FIRST intent
        and append the recent request chain instead.
        """
        if not self._apps:
            return None
        first = self._apps[0].intent
        chain = [a.intent for a in self._apps[1:] if a.intent]
        if chain:
            return f"{first} (subsequent requests: {' -> '.join(chain[-3:])})"
        return first

    # ─── Core: pure render entry ────────────────────────────────────

    async def ingest(
        self,
        content: str | None,
        intent: str,
        *,
        model: str | None = None,
    ) -> App:
        """Pure render: produce next App version from (content, intent).

        Does NOT record events, run searches, or do any agent flow. Caller is
        responsible for surrounding bookkeeping. Used by StandaloneAgent (and
        by any other agent driving core directly).
        """
        prior = self.current_app
        app = await self._producer.produce(
            intent=intent,
            prior_app=prior,
            settings=self._data.settings,
            model=model or self.model,
            version=self.version + 1,
            content=content,
            original_intent=self._intent_context(),
        )
        self._apps.append(app)
        return app

    async def stream_ingest(
        self,
        content: str | None,
        intent: str,
        *,
        model: str | None = None,
    ) -> AsyncIterator[PipelineEvent]:
        """Streaming variant of ingest. Appends App on CompleteEvent."""
        prior = self.current_app
        gen = self._producer.stream(
            intent=intent,
            prior_app=prior,
            settings=self._data.settings,
            model=model or self.model,
            version=self.version + 1,
            content=content,
            original_intent=self._intent_context(),
        )
        async for event in gen:
            if isinstance(event, PipelineCompleteEvent):
                self._apps.append(event.app)
            yield event

    # ─── Delegates ──────────────────────────────────────────────────
    #
    # generate / evolve / stream → StandaloneAgent (real default agent)
    # send / classify            → LegacyShim (transitional, removed when
    #                              sac-web + MCP move to /inbox protocol)

    def _standalone(self) -> "StandaloneAgent":
        if self._standalone_agent is None:
            raise RuntimeError(
                "This conversation has no StandaloneAgent attached. The "
                "generate/evolve/stream API requires one. Use "
                "conv.ingest(content, intent) for the pure-core path, or "
                "construct SaC normally so a StandaloneAgent is provided."
            )
        return self._standalone_agent

    def _legacy(self) -> "LegacyShim":
        if self._legacy_shim is None:
            raise RuntimeError(
                "This conversation has no LegacyShim attached. The send/"
                "classify API is part of the pre-protocol-pivot legacy "
                "surface; if you don't need it, prefer conv.ingest() and the "
                "/inbox protocol instead."
            )
        return self._legacy_shim

    async def generate(self, intent: str, **opts: object) -> App:
        return await self._standalone().generate(self, intent, **opts)

    async def evolve(self, intent: str, **opts: object) -> App:
        return await self._standalone().evolve(self, intent, **opts)

    def stream(self, intent: str, **opts: object) -> AsyncIterator[PipelineEvent]:
        return self._standalone().stream(self, intent, **opts)

    async def send(self, message: str, **opts: object) -> SendResult:
        return await self._legacy().send(self, message, **opts)

    async def classify(self, message: str) -> dict:
        return await self._legacy().classify(self, message)
