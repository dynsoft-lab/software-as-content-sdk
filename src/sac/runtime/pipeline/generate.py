"""
Generate Pipeline (core)

Pure rendering: take an intent + optional content (data already gathered by
whoever called us) and produce a React App. No search, no analyze, no
intent suggestions — those are agent-layer concerns and live in
`sac.agent` (or in any external agent that drives core via /inbox).
"""

from __future__ import annotations

import re

from typing import AsyncIterator

from sac.runtime.pipeline.events import PipelineEmitter
from sac.runtime.prompts.app import build_final_system_prompt, build_generation_prompt
from sac.runtime.prompts.data import build_data_context_prompt
from sac.runtime.providers.base import LLMProvider, reasoning_kwargs
from sac.types import (
    App,
    ConversationSettings,
    Message,
    PipelineChunkEvent,
    PipelineCompleteEvent,
    PipelineErrorEvent,
    PipelineEvent,
    PipelineStageEvent,
    StageStatus,
)


async def generate_pipeline(
    intent: str,
    model: str,
    llm: LLMProvider,
    settings: ConversationSettings,
    version: int = 1,
    content: str | None = None,
) -> App:
    """
    Run the rendering pipeline.

    If `content` is provided, wraps it in the data-context prompt and renders.
    Otherwise emits a bare-intent generation prompt (minimal LLM call without
    any data — typical fallback).
    """
    emitter = PipelineEmitter()

    system_prompt = build_final_system_prompt(
        custom_instructions=settings.custom_instructions,
        include_design_system=settings.use_design_system,
    )

    emitter.start("generate")
    try:
        if content is not None:
            data_context = build_data_context_prompt(content)
            prompt = f"{system_prompt}\n\n{data_context}\n\nUSER INTENT: {intent}"
        else:
            prompt = build_generation_prompt(intent, system_prompt)
        response = await llm.complete(
            model,
            [Message(role="user", content=prompt)],
            **reasoning_kwargs(settings.reasoning_effort),
        )
        emitter.complete("generate")
    except Exception:
        emitter.error("generate")
        raise

    return App(
        code=_extract_code(response),
        version=version,
        intent=intent,
        model=model,
        stages=emitter.stages,
    )


async def stream_generate_pipeline(
    intent: str,
    model: str,
    llm: LLMProvider,
    settings: ConversationSettings,
    version: int = 1,
    content: str | None = None,
) -> AsyncIterator[PipelineEvent]:
    """Streaming variant of generate_pipeline."""
    emitter = PipelineEmitter()
    system_prompt = build_final_system_prompt(
        custom_instructions=settings.custom_instructions,
        include_design_system=settings.use_design_system,
    )

    emitter.start("generate")
    yield PipelineStageEvent(name="generate", status=StageStatus.RUNNING)

    if content is not None:
        data_context = build_data_context_prompt(content)
        prompt = f"{system_prompt}\n\n{data_context}\n\nUSER INTENT: {intent}"
    else:
        prompt = build_generation_prompt(intent, system_prompt)

    full_content = ""
    try:
        async for token in llm.stream(
            model,
            [Message(role="user", content=prompt)],
            **reasoning_kwargs(settings.reasoning_effort),
        ):
            full_content += token
            yield PipelineChunkEvent(data=token)
    except Exception as exc:
        emitter.error("generate")
        yield PipelineStageEvent(name="generate", status=StageStatus.ERROR)
        yield PipelineErrorEvent(error=str(exc))
        return

    emitter.complete("generate")
    yield PipelineStageEvent(name="generate", status=StageStatus.COMPLETED)
    yield PipelineCompleteEvent(app=App(
        code=_extract_code(full_content),
        version=version,
        intent=intent,
        model=model,
        stages=emitter.stages,
    ))


# ─── Internal helpers ──────────────────────────────────────────────


def _extract_code(content: str) -> str:
    """Extract code from LLM response, stripping markdown fences if present."""
    match = re.search(r"```(?:tsx|jsx)?\s*([\s\S]*?)```", content)
    if match:
        return match.group(1).strip()
    return content.strip()
