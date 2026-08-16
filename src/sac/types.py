"""
SaC SDK Type Definitions

All data contracts as Pydantic models, ported from the TypeScript product.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ─── Enums ──────────────────────────────────────────────────────────


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class GrowthType(str, Enum):
    EXTEND_CURRENT = "extend_current"
    NEW_PAGE = "new_page"


class IntentType(str, Enum):
    ACTION = "action"
    EXPLORE = "explore"
    REFINE = "refine"
    ENHANCE = "enhance"


class SendResultType(str, Enum):
    CHAT = "chat"
    GENERATE = "generate"
    EVOLVE = "evolve"


class EventType(str, Enum):
    MESSAGE = "message"
    GENERATION = "generation"
    GROWTH = "growth"
    ERROR = "error"


class EventStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"


# ─── Core Models ────────────────────────────────────────────────────


class SearchQuery(BaseModel):
    query: str
    purpose: str


class SearchSource(BaseModel):
    title: str
    url: str
    content: str


class SearchResult(BaseModel):
    query: str
    answer: str | None = None
    sources: list[SearchSource] = Field(default_factory=list)
    images: list[str] | None = None


class IntentSuggestion(BaseModel):
    label: str
    prompt: str
    type: IntentType = IntentType.ACTION


class StageSnapshot(BaseModel):
    name: str
    status: StageStatus = StageStatus.PENDING
    duration: float | None = None


class GrowthDecision(BaseModel):
    growth_type: GrowthType = GrowthType.EXTEND_CURRENT
    reason: str = ""
    changes: str = ""


class ModelOption(BaseModel):
    id: str
    name: str
    provider: str


# ─── App ────────────────────────────────────────────────────────────


class App(BaseModel):
    """The core output of SaC — a versioned, generated UI artifact."""

    code: str
    version: int = 1
    intent: str = ""
    parent_version: int | None = None
    search_queries: list[SearchQuery] = Field(default_factory=list)
    search_results: list[SearchResult] = Field(default_factory=list)
    suggestions: list[IntentSuggestion] = Field(default_factory=list)
    growth_decision: GrowthDecision | None = None
    stages: list[StageSnapshot] = Field(default_factory=list)
    model: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Conversation ──────────────────────────────────────────────────


class ConversationSettings(BaseModel):
    custom_instructions: str = ""
    use_design_system: bool = True
    enable_web_search: bool = True
    intent_rules: str = ""
    growth_rules: str = ""
    # Reasoning/thinking effort for app generation: "default" leaves it to the
    # provider, "none" disables thinking, "low"/"medium"/"high" request it.
    reasoning_effort: str = "default"


class ConversationData(BaseModel):
    """Conversation metadata (stored in the store)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    title: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model: str = ""
    settings: ConversationSettings = Field(default_factory=ConversationSettings)
    latest_code: str | None = None
    latest_intent: str | None = None
    event_count: int = 0
    callback_url: str | None = None
    callback_format: str | None = None  # "default" | "openclaw_gateway" | "codex_exec_resume" | ...
    callback_auth: str | None = None    # e.g. "Bearer <token>"
    source: str | None = None           # "inbox" | "send" | None — how the conversation was created


# ─── Conversation Events (discriminated union) ─────────────────────


class BaseEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MessageEvent(BaseEvent):
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    content: str


class GenerationEvent(BaseEvent):
    type: Literal["generation"] = "generation"
    intent: str
    model: str = ""
    status: EventStatus = EventStatus.PENDING
    code: str | None = None
    stages: list[StageSnapshot] | None = None
    search_queries: list[SearchQuery] | None = None
    search_results: list[SearchResult] | None = None
    intent_suggestions: list[IntentSuggestion] | None = None
    error: str | None = None


class GrowthEvent(BaseEvent):
    type: Literal["growth"] = "growth"
    intent: str
    model: str = ""
    status: EventStatus = EventStatus.PENDING
    code: str | None = None
    stages: list[StageSnapshot] | None = None
    search_queries: list[SearchQuery] | None = None
    search_results: list[SearchResult] | None = None
    intent_suggestions: list[IntentSuggestion] | None = None
    error: str | None = None


class ErrorEvent(BaseEvent):
    type: Literal["error"] = "error"
    message: str
    context: str | None = None


ConversationEvent = MessageEvent | GenerationEvent | GrowthEvent | ErrorEvent


# ─── Pipeline Events (streaming) ──────────────────────────────────


class PipelineStageEvent(BaseModel):
    type: Literal["stage"] = "stage"
    name: str
    status: StageStatus


class PipelineSearchEvent(BaseModel):
    type: Literal["search"] = "search"
    queries: list[SearchQuery] = Field(default_factory=list)
    results: list[SearchResult] = Field(default_factory=list)


class PipelineChunkEvent(BaseModel):
    type: Literal["chunk"] = "chunk"
    data: str


class PipelineSnapshotEvent(BaseModel):
    """Full code snapshot emitted by progressive evolve after each diff block is applied.

    Unlike PipelineChunkEvent (append to buffer), the frontend should REPLACE
    its buffer with this code for rendering.
    """
    type: Literal["snapshot"] = "snapshot"
    code: str


class PipelineCompleteEvent(BaseModel):
    type: Literal["complete"] = "complete"
    app: App


class PipelineErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    error: str


PipelineEvent = PipelineStageEvent | PipelineSearchEvent | PipelineChunkEvent | PipelineSnapshotEvent | PipelineCompleteEvent | PipelineErrorEvent


# ─── LLM Message ──────────────────────────────────────────────────


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


# ─── Send Result ──────────────────────────────────────────────────


class SendResult(BaseModel):
    """Result of Conversation.send() — the unified entry point."""

    type: SendResultType
    reply: str | None = None        # For chat responses
    app: App | None = None          # For generate/evolve responses
