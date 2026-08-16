"""
HTTP/SSE Server

FastAPI app that exposes the SaC SDK as an HTTP service.
Requires: pip install sac-sdk[server]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse
    from pydantic import BaseModel
    from sse_starlette.sse import EventSourceResponse
except ImportError as e:
    raise ImportError(
        "Server dependencies not installed. Run: pip install sac-sdk[server]"
    ) from e

from sac.runtime.prompts.app import AVAILABLE_MODELS, DEFAULT_MODEL
from sac.runtime.store.file import FileStore
from sac.sac import SaC
from sac.server.http.callbacks import CallbackManager
from sac.types import ConversationSettings

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_RENDERER_DIR = Path(__file__).parent.parent.parent / "renderer"

_VERSION = "0.1.0"


# ─── Pub/Sub for SSE conversation updates ────────────────────────


class _PubSub:
    """In-memory per-conversation event broker for SSE subscribers.

    One SaC server process manages all subscribers. For multi-process
    deployments swap this for redis/nats; the publish/subscribe surface
    stays the same.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, conv_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subs[conv_id].append(q)
        return q

    def unsubscribe(self, conv_id: str, q: asyncio.Queue) -> None:
        if conv_id in self._subs:
            try:
                self._subs[conv_id].remove(q)
            except ValueError:
                pass
            if not self._subs[conv_id]:
                del self._subs[conv_id]

    def publish(self, conv_id: str, event_type: str, data: dict[str, Any]) -> None:
        payload = {"event": event_type, "data": json.dumps(data)}
        subs = list(self._subs.get(conv_id, []))
        if event_type == "chat":
            logger.info("PubSub chat publish: conv=%s subscribers=%d", conv_id, len(subs))
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer — drop silently rather than block agent flow.
                # Use debug level to avoid flooding logs with chunk drops.
                logger.debug("PubSub queue full: conv=%s event=%s", conv_id, event_type)


class _ActionQueue:
    """Per-conversation action queue for MCP pull mode.

    When no callback_url is registered, user actions from the viewer are
    queued here instead of dispatched. MCP tools (wait_for_action) or the
    /c/{id}/wait-action endpoint consume from this queue.

    Only ONE consumer per conversation is allowed at a time. If a new
    wait() call arrives while an old one is still active (e.g. zombie
    from a cancelled MCP tool call), the old one is evicted and returns
    None immediately.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._listeners: set[str] = set()  # conv_ids with active wait()
        self._pending: dict[str, dict[str, Any]] = {}  # conv_id -> last pushed action
        self._evict_events: dict[str, asyncio.Event] = {}  # signal old waiter to stop

    def _get_queue(self, conv_id: str) -> asyncio.Queue:
        if conv_id not in self._queues:
            self._queues[conv_id] = asyncio.Queue(maxsize=32)
        return self._queues[conv_id]

    def has_listener(self, conv_id: str) -> bool:
        """True if an MCP host is actively waiting for actions on this conv."""
        return conv_id in self._listeners

    def push(self, conv_id: str, intent: str, context: dict[str, Any] | None = None) -> None:
        q = self._get_queue(conv_id)
        action = {"intent": intent, "context": context, "timestamp": datetime.now(timezone.utc).isoformat()}
        try:
            q.put_nowait(action)
        except asyncio.QueueFull:
            # Drop oldest and retry
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            q.put_nowait(action)
        # Track pending action for TTL
        self._pending[conv_id] = action

    async def wait(self, conv_id: str, timeout: float = 300.0) -> dict[str, Any] | None:
        # Evict any previous waiter on this conversation so zombie polls
        # from cancelled MCP tool calls don't compete for queue items.
        old_evict = self._evict_events.get(conv_id)
        if old_evict:
            old_evict.set()  # signal old waiter to bail out
        evict = asyncio.Event()
        self._evict_events[conv_id] = evict

        q = self._get_queue(conv_id)
        self._listeners.add(conv_id)
        try:
            # Race: either we get an action, or we're evicted by a newer waiter.
            get_task = asyncio.ensure_future(q.get())
            evict_task = asyncio.ensure_future(evict.wait())
            done, pending = await asyncio.wait(
                {get_task, evict_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()

            if get_task in done:
                result = get_task.result()
                self._pending.pop(conv_id, None)
                return result

            # Evicted or timed out — return None
            return None
        finally:
            self._listeners.discard(conv_id)
            # Only clean up evict event if we're still the current waiter
            if self._evict_events.get(conv_id) is evict:
                self._evict_events.pop(conv_id, None)

    def is_pending(self, conv_id: str) -> bool:
        """True if an action was pushed but not yet consumed by wait()."""
        return conv_id in self._pending

    def expire(self, conv_id: str) -> dict[str, Any] | None:
        """Remove and return a pending action (TTL expired). Drains the queue entry too."""
        action = self._pending.pop(conv_id, None)
        if action:
            q = self._queues.get(conv_id)
            if q:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
        return action


# ─── Request/Response Models ──────────────────────────────────────


class GenerateRequest(BaseModel):
    intent: str
    model: str | None = None
    conversation_id: str | None = None
    web_search: bool = True
    custom_instructions: str = ""
    use_design_system: bool = True
    reasoning_effort: str = "default"


class EvolveRequest(BaseModel):
    intent: str
    conversation_id: str
    model: str | None = None


class StreamRequest(BaseModel):
    intent: str
    conversation_id: str | None = None
    model: str | None = None
    web_search: bool | None = None
    custom_instructions: str | None = None
    use_design_system: bool | None = None
    reasoning_effort: str | None = None


class SendRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    model: str | None = None
    web_search: bool | None = None
    custom_instructions: str | None = None
    use_design_system: bool | None = None
    reasoning_effort: str | None = None


class UpdateConversationRequest(BaseModel):
    title: str | None = None
    settings: dict[str, Any] | None = None


class InboxRequest(BaseModel):
    """Protocol contract: upstream agent posts content for SaC to render.

    - `content`: the actual data the agent wants rendered (any string form).
    - `intent`: optional human-readable label/hint for the rendering brain.
    - `type`: agent's channel decision — "ui" (render app) or "chat" (store
      as chat message). If omitted, SaC falls back to its built-in classifier
      for backward compatibility with the product frontend.
    - `conversation_id`: omit for first call; include to evolve an existing
      conversation. SaC decides generate vs evolve based on conversation state.
    - `callback_url`: where SaC should POST user actions (button clicks,
      follow-up intents). Set on first call; subsequent calls may omit.
    - `suggestions`: optional list of follow-up actions (label/prompt/type)
      the agent wants to expose. If omitted, SaC's default agent generates
      content-grounded suggestions automatically.
    - `context`: opaque metadata echoed back on callbacks; SaC does not parse.
    """

    content: str
    intent: str | None = None
    type: str | None = None  # "ui" | "chat" | None (None = legacy classify fallback)
    conversation_id: str | None = None
    user_message: str | None = None  # Original user input before agent expansion
    callback_url: str | None = None
    callback_format: str | None = None  # "default" | "openclaw_gateway" | "codex_exec_resume"
    callback_auth: str | None = None    # e.g. "Bearer <token>"
    suggestions: list[dict[str, Any]] | None = None
    context: dict[str, Any] | None = None


class ActionRequest(BaseModel):
    """User action (button click in App, or text input in chat panel) that
    SaC forwards to the conversation's registered callback_url.

    - `intent`: human-readable description of what the user did/said.
    - `context`: opaque metadata to attach (echoed to the agent).
    """

    intent: str
    context: dict[str, Any] | None = None


# ─── App Factory ──────────────────────────────────────────────────


def create_app(sac: SaC | None = None) -> FastAPI:
    """
    Create a FastAPI app wired to a SaC instance.

    If no SaC instance is provided, one is created from environment variables:
      - SAC_API_KEY (required) — API key (OpenRouter, Anthropic, OpenAI, etc.)
      - SAC_API_BASE (optional) — custom endpoint URL (auto-detects Anthropic keys)
      - SAC_SEARCH_API_KEY (optional) — Tavily API key
      - SAC_MODEL (optional) — default model
    """
    data_dir = os.environ.get("SAC_DATA_DIR") or str(Path.home() / ".sac")
    if sac is None:
        api_key = os.environ.get("SAC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "SAC_API_KEY not set. Either:\n"
                "  1. Run: export SAC_API_KEY=your-key\n"
                "  2. Create ~/.sac/.env with SAC_API_KEY=your-key\n"
                "  3. Run: sac setup claude-code (writes config automatically)"
            )
        sac = SaC(
            api_key=api_key,
            api_base=os.environ.get("SAC_API_BASE"),
            search_api_key=os.environ.get("SAC_SEARCH_API_KEY"),
            model=os.environ.get("SAC_MODEL", DEFAULT_MODEL),
            store=FileStore(data_dir),
        )

    server_cwd = Path.cwd()
    app = FastAPI(title="SaC SDK Server", version=_VERSION)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Keep track of active conversations
    _conversations: dict[str, Any] = {}

    # Pub/sub broker for /c/{id}/events SSE subscribers
    pubsub = _PubSub()

    # Action queue for MCP pull mode (no callback_url conversations)
    action_queue = _ActionQueue()
    app.state.action_queue = action_queue

    async def _pin_codex_thread(conv_id: str, thread_id: str) -> None:
        """Pin the resolved Codex thread id onto the conversation.

        After the first callback, replace the bootstrap `thread=last` in
        the stored callback_url with the concrete thread id so subsequent
        user actions resume this exact thread (not whatever Codex session
        happens to be most recent). No-op if already pinned or explicit.
        """
        conv = await sac._store.get_conversation(conv_id)
        if conv is None or not conv.callback_url:
            return
        new_url = _pin_thread_url(conv.callback_url, thread_id)
        if new_url is not None:
            await sac._store.update_conversation(conv_id, callback_url=new_url)

    callback_manager = CallbackManager(
        publish=pubsub.publish,
        server_cwd=server_cwd,
        pin_thread=_pin_codex_thread,
        runs_dir=Path(data_dir) / "_runs",
    )

    def _get_user_id(request: Request) -> str:
        """Extract user ID from X-User-Id header, default to 'anonymous'."""
        return request.headers.get("x-user-id", "anonymous")

    async def _get_or_create_conv(conv_id: str | None, settings: ConversationSettings | None = None, user_id: str = "") -> Any:
        if conv_id and conv_id in _conversations:
            conv = _conversations[conv_id]
            # Per-request settings win over cached/stored ones — the web
            # client sends the full current settings on every request, so a
            # mid-conversation change (e.g. reasoning effort) takes effect.
            if settings is not None:
                conv._data.settings = settings
            return conv
        conv = sac.conversation(id=conv_id, settings=settings)
        if user_id:
            conv._data.user_id = user_id
        # If an existing conv_id was provided, load state from store
        if conv_id:
            await conv._load_from_store()
            if settings is not None:
                conv._data.settings = settings
        else:
            # Brand-new conversation: `sac.conversation()` schedules creation
            # as a fire-and-forget task. Explicitly await it here so the conv
            # is fully registered in the store BEFORE any subsequent
            # update_conversation calls (e.g. callback_url persistence in
            # /inbox). create_conversation is idempotent — safe to call again.
            await sac._store.create_conversation(conv._data)
        _conversations[conv.id] = conv
        return conv

    # ─── Health & Discovery ──────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": _VERSION}

    @app.get("/models")
    async def list_models() -> dict[str, Any]:
        return {
            "models": [m.model_dump() for m in AVAILABLE_MODELS],
            "default": DEFAULT_MODEL,
        }

    # ─── Generation ──────────────────────────────────────────────

    @app.post("/generate")
    async def generate(req: GenerateRequest, request: Request) -> dict[str, Any]:
        settings = ConversationSettings(
            custom_instructions=req.custom_instructions,
            use_design_system=req.use_design_system,
            reasoning_effort=req.reasoning_effort,
            enable_web_search=req.web_search,
        )
        conv = await _get_or_create_conv(req.conversation_id, settings, user_id=_get_user_id(request))

        opts: dict[str, object] = {}
        if req.model:
            opts["model"] = req.model
        app_result = await conv.generate(req.intent, **opts)
        return {
            "success": True,
            "conversation_id": conv.id,
            "app": app_result.model_dump(),
        }

    @app.post("/evolve")
    async def evolve(req: EvolveRequest) -> dict[str, Any]:
        if req.conversation_id not in _conversations:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conv = _conversations[req.conversation_id]

        opts: dict[str, object] = {}
        if req.model:
            opts["model"] = req.model
        app_result = await conv.evolve(req.intent, **opts)
        return {
            "success": True,
            "conversation_id": conv.id,
            "app": app_result.model_dump(),
        }

    @app.post("/stream")
    async def stream_generate(req: StreamRequest, request: Request) -> EventSourceResponse:
        settings: ConversationSettings | None = None
        if req.web_search is not None or req.custom_instructions is not None or req.use_design_system is not None or req.reasoning_effort is not None:
            settings = ConversationSettings(
                custom_instructions=req.custom_instructions or "",
                use_design_system=req.use_design_system if req.use_design_system is not None else True,
                enable_web_search=req.web_search if req.web_search is not None else True,
                reasoning_effort=req.reasoning_effort or "default",
            )

        conv = await _get_or_create_conv(req.conversation_id, settings, user_id=_get_user_id(request))

        opts: dict[str, object] = {}
        if req.model:
            opts["model"] = req.model

        async def event_generator():
            async for event in conv.stream(req.intent, **opts):
                payload = event.model_dump()
                # Attach conversation_id to complete events
                if event.type == "complete":
                    payload["conversation_id"] = conv.id
                yield {"event": event.type, "data": json.dumps(payload)}

        return EventSourceResponse(event_generator())

    @app.post("/send")
    async def send_message(req: SendRequest, request: Request) -> dict[str, Any]:
        """
        Unified entry point. Classifies the message and either:
        - Returns a chat reply directly (type: "chat")
        - Returns classification only (type: "generate" or "evolve") —
          caller should then use /stream to execute
        """
        settings: ConversationSettings | None = None
        if req.web_search is not None or req.custom_instructions is not None or req.use_design_system is not None or req.reasoning_effort is not None:
            settings = ConversationSettings(
                custom_instructions=req.custom_instructions or "",
                use_design_system=req.use_design_system if req.use_design_system is not None else True,
                enable_web_search=req.web_search if req.web_search is not None else True,
                reasoning_effort=req.reasoning_effort or "default",
            )

        conv = await _get_or_create_conv(req.conversation_id, settings, user_id=_get_user_id(request))

        classification = await conv.classify(req.message)

        if classification["type"] == "chat":
            reply = classification.get("reply", "")
            # Store user message and assistant reply
            from sac.types import MessageEvent
            await sac._store.add_event(
                MessageEvent(conversation_id=conv.id, role="user", content=req.message)
            )
            await sac._store.add_event(
                MessageEvent(conversation_id=conv.id, role="assistant", content=reply)
            )
            # Set title from first message if not already set
            conv_data = await sac._store.get_conversation(conv.id)
            if conv_data and not conv_data.title:
                title = req.message[:80] + ("..." if len(req.message) > 80 else "")
                await sac._store.update_conversation(conv.id, title=title)
            return {
                "type": "chat",
                "conversation_id": conv.id,
                "reply": reply,
            }

        # It's an "update" — return classification, let frontend call /stream
        # Set title from first message if not already set
        conv_data = await sac._store.get_conversation(conv.id)
        if conv_data and not conv_data.title:
            title = req.message[:80] + ("..." if len(req.message) > 80 else "")
            await sac._store.update_conversation(conv.id, title=title)

        action = "evolve" if conv.current_app else "generate"
        return {
            "type": action,
            "conversation_id": conv.id,
        }

    # ─── Protocol: /inbox (upstream agent → SaC) ─────────────────

    @app.post("/inbox")
    async def inbox(req: InboxRequest, request: Request) -> dict[str, Any]:
        """Receive content from an upstream agent and render it.

        SaC inspects the response shape and dispatches to one of two channels:
          - Φⁿˡ (chat) — short / conversational response → assistant message
            in the NL chat panel; no new App version.
          - Φˢ  (ui)   — substantive content → render as new App version
            (generate first, evolve subsequently).

        The chat-vs-ui decision currently reuses the LegacyShim classifier
        (small extra LLM call). A future fused-LLM-call milestone collapses
        this into one call alongside code generation.

        Returns conversation_id, the URL to view, version (null for chat-
        only responses), and `type` indicating which channel was used.
        """
        from sac.types import MessageEvent

        conv = await _get_or_create_conv(req.conversation_id, user_id=_get_user_id(request))

        # Mark source so /action knows this conversation is agent-driven
        # (MCP pull or callback), not product-driven (/send).
        if not req.conversation_id:
            # First call — mark as inbox-created
            await sac._store.update_conversation(conv.id, source="inbox")

        # Persist callback settings on the conversation so future user actions
        # know where and how to POST. First call sets them; later calls may update.
        callback_updates: dict[str, Any] = {}
        if req.callback_url:
            callback_updates["callback_url"] = req.callback_url
        if req.callback_format:
            callback_updates["callback_format"] = req.callback_format
        if req.callback_auth:
            callback_updates["callback_auth"] = req.callback_auth
        if callback_updates:
            await sac._store.update_conversation(conv.id, **callback_updates)

        base = str(request.base_url).rstrip("/")

        # Determine channel: ui (render app) vs chat (store message).
        #
        # Priority:
        #   1. Agent explicitly sets req.type → trust the agent's decision
        #   2. Active callback run → always "update" (agent is responding
        #      to a user action, content should be rendered as UI)
        #   3. Fallback → legacy classifier (for product frontend that
        #      doesn't pass type yet)
        if req.type == "chat":
            classification = {"type": "chat"}
        elif req.type == "ui" or callback_manager.has_active_run(conv.id):
            classification = {"type": "update"}
        elif req.callback_url or getattr(await sac._store.get_conversation(conv.id), "callback_url", None):
            # Agent-owned conversation (has callback_url) but no explicit
            # type → default to UI.  Agents POST content to render, not chat.
            classification = {"type": "update"}
        else:
            # Legacy fallback: no explicit type → classify via LLM
            classification = await sac._legacy_shim.classify(conv, req.content)

        if classification["type"] == "chat":
            # Show the agent's content directly as an assistant message in
            # the NL channel. We ignore classify's generated `reply` field —
            # that was meant for the legacy "assistant replies to user
            # message" flow; here the agent IS the message.
            logger.info("Inbox chat path: conv=%s content_len=%d", conv.id, len(req.content))
            await sac._store.add_event(
                MessageEvent(
                    conversation_id=conv.id,
                    role="assistant",
                    content=req.content,
                )
            )
            pubsub.publish(
                conv.id,
                "chat",
                {"role": "assistant", "content": req.content},
            )
            callback_manager.mark_inbox_result(
                conv.id, kind="chat", version=None
            )
            return {
                "conversation_id": conv.id,
                "url": f"{base}/c/{conv.id}",
                "version": None,
                "type": "chat",
            }

        # Φˢ — render new App version via streaming ingest.
        # Chunks are broadcast through PubSub so any connected viewer
        # (via /c/{id}/events SSE) sees progressive code generation in
        # real-time. The REST response is still synchronous — callers
        # (agents) get the final result when generation completes.
        from sac.types import (
            EventStatus,
            GenerationEvent,
            GrowthEvent,
            IntentSuggestion,
            PipelineChunkEvent,
            PipelineCompleteEvent,
            PipelineErrorEvent,
            PipelineSearchEvent,
            PipelineSnapshotEvent,
            PipelineStageEvent,
        )
        from sac.agent.prompts.intent import (
            get_intent_suggestion_prompt,
            parse_intent_suggestions,
        )
        from sac.types import Message as SaCMessage

        intent = req.intent or req.content[:200]
        is_evolve = conv.current_app is not None

        # Record user intent as a message event.
        # Skip if source="inbox" + evolve: the short label was already stored
        # by /action before the agent rewrote it into a detailed prompt.
        conv_data = await sac._store.get_conversation(conv.id)
        is_agent_evolve = is_evolve and conv_data and conv_data.source == "inbox"
        if not is_agent_evolve:
            # Prefer user_message (original user input) over intent (agent-expanded)
            display_msg = req.user_message or intent
            await sac._store.add_event(
                MessageEvent(conversation_id=conv.id, role="user", content=display_msg)
            )

        try:
            # Stream generation — publish each event to PubSub for
            # live viewer updates while awaiting the final App result.
            app_result = None
            async for event in conv.stream_ingest(
                content=req.content, intent=intent
            ):
                if isinstance(event, PipelineChunkEvent):
                    pubsub.publish(conv.id, "chunk", {"data": event.data})
                elif isinstance(event, PipelineSnapshotEvent):
                    pubsub.publish(conv.id, "snapshot", {"code": event.code})
                elif isinstance(event, PipelineStageEvent):
                    stage_data: dict = {"name": event.name, "status": event.status}
                    if hasattr(event, "duration") and event.duration is not None:
                        stage_data["duration"] = event.duration
                    pubsub.publish(conv.id, "stage", stage_data)
                elif isinstance(event, PipelineSearchEvent):
                    pubsub.publish(conv.id, "search", {
                        "results": [
                            {"query": r.query, "sources": [{"title": s.title, "url": s.url} for s in (r.sources or [])]}
                            for r in (event.results or [])
                        ]
                    })
                elif isinstance(event, PipelineCompleteEvent):
                    app_result = event.app
                elif isinstance(event, PipelineErrorEvent):
                    raise RuntimeError(event.error)

            if app_result is None:
                raise RuntimeError("Stream ended without producing an App")

            # Suggestions: use agent-provided override, or generate defaults
            if req.suggestions is not None:
                try:
                    suggestions = [IntentSuggestion(**s) for s in req.suggestions]
                except Exception as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid suggestions payload: {e}",
                    )
            else:
                try:
                    prompt = get_intent_suggestion_prompt(
                        intent, [], conv.settings.intent_rules
                    )
                    response = await sac._llm.complete(
                        conv.model,
                        [SaCMessage(role="user", content=prompt)],
                    )
                    suggestions = parse_intent_suggestions(response)
                except Exception:
                    suggestions = []

            app_result.suggestions = suggestions

            event_cls = GrowthEvent if is_evolve else GenerationEvent
            await sac._store.add_event(
                event_cls(
                    conversation_id=conv.id,
                    intent=intent,
                    model=conv.model,
                    status=EventStatus.SUCCESS,
                    code=app_result.code,
                    stages=app_result.stages,
                    intent_suggestions=suggestions or None,
                )
            )

            # Set title from first generation
            if not is_evolve and conv.version == 1:
                title = intent[:80] + ("..." if len(intent) > 80 else "")
                await sac._store.update_conversation(conv.id, title=title)

        except HTTPException:
            raise
        except Exception as exc:
            event_cls = GrowthEvent if is_evolve else GenerationEvent
            await sac._store.add_event(
                event_cls(
                    conversation_id=conv.id,
                    intent=intent,
                    model=conv.model,
                    status=EventStatus.ERROR,
                    error=str(exc),
                )
            )
            pubsub.publish(conv.id, "error", {"error": str(exc)})
            raise HTTPException(status_code=500, detail=str(exc))

        pubsub.publish(
            conv.id,
            "version",
            {
                "conversation_id": conv.id,
                "version": app_result.version,
            },
        )
        callback_manager.mark_inbox_result(
            conv.id, kind="ui", version=app_result.version
        )

        return {
            "conversation_id": conv.id,
            "url": f"{base}/c/{conv.id}",
            "version": app_result.version,
            "type": "ui",
        }

    # ─── Protocol: /c/{id}/events (browser ← SaC live updates) ───

    @app.get("/c/{conv_id}/events")
    async def conversation_events(conv_id: str, request: Request) -> EventSourceResponse:
        """SSE stream of conversation updates.

        Emits events whenever the conversation gets new state from /inbox:
          - `version` — new App version produced (browser should reload App)
          - `chat`    — new assistant message (browser should append bubble)

        Browser fetches initial state via GET /c/{conv_id} or similar; SSE
        only delivers deltas. Sends a 15-second keepalive `ping` to survive
        proxies that drop idle connections.
        """
        conv_data = await sac._store.get_conversation(conv_id)
        if conv_data is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        q = pubsub.subscribe(conv_id)

        async def event_generator():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield event
                    except asyncio.TimeoutError:
                        # Keepalive — many proxies kill idle SSE after ~30s.
                        yield {"event": "ping", "data": "{}"}
            finally:
                pubsub.unsubscribe(conv_id, q)

        return EventSourceResponse(event_generator())

    @app.post("/c/{conv_id}/action")
    async def conversation_action(conv_id: str, req: ActionRequest, request: Request) -> dict[str, Any]:
        """Forward a user action to the agent or queue it for MCP pull.

        Two modes:
          - callback mode (callback_url set): dispatch to external agent
          - pull mode (no callback_url): queue for wait_for_action / MCP

        Pre-classifies the intent: chat-type messages (greetings, small
        talk) get a direct NL reply without invoking the external agent,
        saving 60-90s of unnecessary round-trip.
        """
        conv_data = await sac._store.get_conversation(conv_id)
        if conv_data is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # ── Pre-classify: intercept chat-type intents before dispatching ──
        # Only applies in "product mode" — conversations created via /send
        # (product frontend) with no external agent connected.
        #
        # When an external agent owns this conversation (source="inbox",
        # callback_url set, or MCP pull), the agent decides chat-vs-ui.
        # We pass everything through without classifying.
        is_agent_owned = (
            conv_data.source == "inbox"       # created via /inbox (MCP or agent)
            or bool(conv_data.callback_url)   # has callback (Codex/OpenClaw)
        )

        if not is_agent_owned and req.context is None:
            conv = await _get_or_create_conv(conv_id, user_id=_get_user_id(request))
            try:
                classification = await conv.classify(req.intent)
                if classification.get("type") == "chat":
                    from sac.types import MessageEvent

                    reply = classification.get("reply", "")
                    # Store user message + assistant reply
                    await sac._store.add_event(
                        MessageEvent(conversation_id=conv_id, role="user", content=req.intent)
                    )
                    await sac._store.add_event(
                        MessageEvent(conversation_id=conv_id, role="assistant", content=reply)
                    )
                    # Push to frontend via SSE
                    pubsub.publish(conv_id, "chat", {"role": "assistant", "content": reply})
                    return {"ok": True, "type": "chat", "reply": reply}
            except Exception:
                # Classify failed — fall through to agent dispatch
                logger.debug("Pre-classify failed for conv %s, dispatching to agent", conv_id, exc_info=True)

        # ── Pull mode: no callback_url → queue for MCP wait_for_action ──
        if not conv_data.callback_url:
            logger.info("Action push: conv=%s intent=%r listener=%s", conv_id, req.intent, action_queue.has_listener(conv_id))
            # Store the original short label as user message before agent rewrites it
            from sac.types import MessageEvent
            await sac._store.add_event(
                MessageEvent(conversation_id=conv_id, role="user", content=req.intent)
            )
            action_queue.push(conv_id, req.intent, req.context)

            # TTL: if no agent consumes this action within 45s, notify frontend
            # but do NOT expire/remove the action — the agent may still pick
            # it up when it reconnects (e.g. between wait_for_action calls).
            async def _action_ttl(cid: str, ttl: float = 45.0) -> None:
                await asyncio.sleep(ttl)
                if action_queue.is_pending(cid):
                    pubsub.publish(cid, "action_timeout", {
                        "message": "Agent did not respond. Tell your agent to use SaC MCP to restart server and wait for action.",
                    })

            asyncio.create_task(_action_ttl(conv_id))
            return {"ok": True, "type": "queued", "intent": req.intent}

        # ── Callback mode: dispatch to external agent ──
        # Store user message so it survives page refresh
        from sac.types import MessageEvent
        await sac._store.add_event(
            MessageEvent(conversation_id=conv_id, role="user", content=req.intent)
        )
        run = await callback_manager.dispatch(
            conv_id=conv_id,
            intent=req.intent,
            context=req.context,
            callback_url=conv_data.callback_url,
            callback_format=conv_data.callback_format,
            callback_auth=conv_data.callback_auth,
            sac_url=str(request.base_url).rstrip("/"),
        )
        return {
            "ok": True,
            "type": "callback",
            "callback_url": conv_data.callback_url,
            "run_id": run["id"],
        }

    @app.get("/c/{conv_id}/wait-action")
    async def wait_for_action(conv_id: str, timeout: float = 300.0) -> dict[str, Any]:
        """Long-poll endpoint: block until a user action is available.

        Used by MCP tools (wait_for_action) to pull user actions from the
        viewer. Returns the action or null on timeout.
        """
        conv_data = await sac._store.get_conversation(conv_id)
        if conv_data is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        timeout = min(max(timeout, 1.0), 600.0)
        logger.info("Wait-action start: conv=%s timeout=%.0f", conv_id, timeout)
        action = await action_queue.wait(conv_id, timeout=timeout)
        logger.info("Wait-action done: conv=%s got=%s", conv_id, action is not None)
        if action is None:
            return {"action": None, "timed_out": True}

        # Include recent conversation context so the agent understands
        # what "retry", "undo", etc. mean in context.
        from sac.types import MessageEvent as _ME
        events = await sac._store.get_events(conv_id)
        recent_msgs = []
        for evt in reversed(events):
            if isinstance(evt, _ME):
                recent_msgs.append({"role": evt.role, "content": evt.content[:200]})
                if len(recent_msgs) >= 6:
                    break
        recent_msgs.reverse()

        return {
            "action": action,
            "timed_out": False,
            "recent_messages": recent_msgs,
            "current_version": conv_data.event_count if conv_data else None,
        }

    # ─── Conversation Management ─────────────────────────────────

    @app.get("/c/{conv_id}/callback-runs")
    async def conversation_callback_runs(conv_id: str) -> dict[str, Any]:
        conv_data = await sac._store.get_conversation(conv_id)
        if conv_data is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"runs": callback_manager.list_runs(conv_id)}

    @app.get("/conversations")
    async def list_conversations(request: Request) -> dict[str, Any]:
        user_id = _get_user_id(request)
        convs = await sac._store.list_conversations(user_id=user_id)
        return {"conversations": [c.model_dump() for c in convs]}

    @app.get("/conversations/{conv_id}")
    async def get_conversation(conv_id: str) -> dict[str, Any]:
        conv = await sac._store.get_conversation(conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        events = await sac._store.get_events(conv_id)
        return {
            "conversation": conv.model_dump(),
            "events": [e.model_dump() for e in events],
        }

    @app.patch("/conversations/{conv_id}")
    async def update_conversation(conv_id: str, req: UpdateConversationRequest) -> dict[str, Any]:
        conv = await sac._store.get_conversation(conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        updates: dict[str, object] = {}
        if req.title is not None:
            updates["title"] = req.title
        if req.settings is not None:
            # Merge with existing settings
            current_settings = conv.settings.model_dump()
            current_settings.update(req.settings)
            updates["settings"] = ConversationSettings(**current_settings)
        if updates:
            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            await sac._store.update_conversation(conv_id, **updates)

        updated = await sac._store.get_conversation(conv_id)
        return {"conversation": updated.model_dump() if updated else conv.model_dump()}

    @app.delete("/conversations/{conv_id}")
    async def delete_conversation(conv_id: str) -> dict[str, bool]:
        conv = await sac._store.get_conversation(conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        await sac._store.delete_conversation(conv_id)
        # Also remove from active conversations cache
        _conversations.pop(conv_id, None)
        return {"success": True}

    # ─── Web Preview UI ─────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def preview_page():
        return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/c/{conv_id}", response_class=HTMLResponse)
    async def conversation_page(conv_id: str):
        """Pretty URL form for a specific conversation; serves the same viewer.

        The static page reads the conversation id from the URL path (or `?c=`
        query param) and auto-loads it on init.
        """
        return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{path:path}")
    async def serve_static(path: str):
        file = (_STATIC_DIR / path).resolve()
        static_root = _STATIC_DIR.resolve()
        if not file.is_relative_to(static_root) or not file.exists() or not file.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        suffix = file.suffix
        media_types = {
            ".html": "text/html",
            ".js": "application/javascript",
            ".css": "text/css",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }
        return FileResponse(
            file,
            media_type=media_types.get(suffix, "application/octet-stream"),
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/renderer/{path:path}")
    async def serve_renderer(path: str):
        file = _RENDERER_DIR / path
        if not file.exists() or not file.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        suffix = file.suffix
        media_types = {
            ".html": "text/html",
            ".js": "application/javascript",
            ".md": "text/markdown",
            ".css": "text/css",
        }
        return FileResponse(
            file,
            media_type=media_types.get(suffix, "application/octet-stream"),
            headers={"Cache-Control": "no-cache"},
        )

    return app


# ─── Standalone runner ────────────────────────────────────────────


def _pin_thread_url(callback_url: str, thread_id: str) -> str | None:
    """Rewrite `thread=last` in a Codex callback_url to a concrete id.

    Returns the new URL, or ``None`` if no rewrite is needed (the URL
    already carries an explicit/pinned thread id, so we must not clobber
    it). Pure function — unit-testable without a store or server.
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(callback_url)
    qs = parse_qs(parsed.query)
    current = (qs.get("thread") or qs.get("thread_id") or [""])[0]
    if current != "last":
        return None
    flat = {k: v[0] for k, v in qs.items()}
    flat.pop("thread_id", None)
    flat["thread"] = thread_id
    return urlunparse(parsed._replace(query=urlencode(flat)))


def _probe_existing_server(port: int) -> str:
    """Classify what (if anything) is already listening on ``port``.

    Returns one of: ``"free"`` (nothing listening), ``"sac"`` (a healthy SaC
    server already running), ``"foreign"`` (port occupied by something that is
    not a healthy SaC server, e.g. another app or a stale/zombie listener).
    """
    import socket

    # Is anything accepting connections on the loopback address?
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            return "free"

    # Something is listening — is it a healthy SaC server?
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=1.5
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("status") == "ok":
            return "sac"
    except Exception:
        return "foreign"
    return "foreign"


def _warn_if_not_repo_root() -> None:
    """Warn when launched outside a project root.

    Codex callbacks resolve ``cwd=server`` to this process's working
    directory; starting from an unrelated dir silently breaks the loop.
    """
    cwd = Path.cwd()
    markers = (".git", "pyproject.toml", "package.json", "src")
    if not any((cwd / m).exists() for m in markers):
        print(
            f"⚠  sac serve started from {cwd}\n"
            "   This does not look like a project root. Codex callbacks "
            "resolve cwd=server to this directory, so the agent loop will "
            "run here. Start `sac serve` from the SDK repo root for the "
            "Codex/OpenClaw bidirectional loop to work.",
            file=sys.stderr,
        )


def run(host: str = "0.0.0.0", port: int = 18420) -> None:
    """Run the server with uvicorn.

    `sac serve` is idempotent: if a healthy SaC server is already serving
    this port, reuse it and exit 0 instead of fighting for the socket. This
    lets an agent run `sac serve` safely without restart/port-migration
    logic. If the port is held by something else (foreign app or a stale
    listener), fail loudly with actionable guidance rather than producing a
    half-bound "answers once then refuses" zombie.
    """
    import uvicorn

    state = _probe_existing_server(port)
    if state == "sac":
        print(
            f"SaC server already running and healthy at "
            f"http://127.0.0.1:{port} — reusing it (nothing to do).\n"
            f"Open http://127.0.0.1:{port}",
        )
        return
    if state == "foreign":
        pid = ""
        try:
            import subprocess

            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            if out:
                pid = out.splitlines()[0]
        except Exception:
            pass
        held_by = f" (held by PID {pid})" if pid else ""
        kill_hint = f"kill {pid}" if pid else "kill <pid>"
        print(
            f"Error: port {port} is occupied{held_by} but is not a healthy "
            f"SaC server.\n"
            f"  - If it is a stale SaC process, stop it: {kill_hint}\n"
            f"  - Or start on another port: sac serve --port <port>\n"
            f"sac serve will not bind a contended socket (this is what "
            f"caused the 'answers once then refuses' zombie before).",
            file=sys.stderr,
        )
        sys.exit(1)

    app = create_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
