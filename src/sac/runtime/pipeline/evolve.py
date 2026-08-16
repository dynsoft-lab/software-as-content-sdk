"""
Evolve Pipeline (core)

Pure rendering for version N+1. Takes prior code + new intent + optional
content (data already gathered by whoever called us) and produces a unified
LLM call that decides growth_type AND emits new code in one pass.

No search, no analyze, no intent suggestions — agent-layer concerns live
in `sac.agent` (or any external agent driving core via /inbox).
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator

import logging

from sac.runtime.pipeline._diff import _HIGHLIGHT_ATTR_RE
from sac.runtime.pipeline._diff_filter import DiffChunkFilter
from sac.runtime.pipeline._tsx_filter import TsxChunkFilter
from sac.runtime.pipeline.events import PipelineEmitter
from sac.runtime.prompts.app import build_final_system_prompt
from sac.runtime.prompts.growth import build_growth_prompt, build_growth_prompt_diff
from sac.runtime.providers.base import LLMProvider, reasoning_kwargs
from sac.types import (
    App,
    ConversationSettings,
    GrowthDecision,
    GrowthType,
    Message,
    PipelineChunkEvent,
    PipelineCompleteEvent,
    PipelineErrorEvent,
    PipelineEvent,
    PipelineSnapshotEvent,
    PipelineStageEvent,
    StageStatus,
)

logger = logging.getLogger(__name__)


async def evolve_pipeline(
    new_intent: str,
    current_code: str,
    original_intent: str,
    model: str,
    llm: LLMProvider,
    settings: ConversationSettings,
    version: int = 2,
    parent_version: int = 1,
    content: str | None = None,
) -> App:
    """Render the next App version. content (if present) is the new data."""
    emitter = PipelineEmitter()

    system_prompt = build_final_system_prompt(
        custom_instructions=settings.custom_instructions,
        include_design_system=settings.use_design_system,
    )

    emitter.start("generate")
    try:
        growth_prompt = build_growth_prompt(
            current_code=current_code,
            original_intent=original_intent,
            new_intent=new_intent,
            system_prompt=system_prompt,
            custom_growth_rules=settings.growth_rules or None,
            content=content,
        )
        response = await llm.complete(
            model,
            [Message(role="user", content=growth_prompt)],
            **reasoning_kwargs(settings.reasoning_effort),
        )
        decision, code = _parse_growth_response(response)
        emitter.complete("generate")
    except Exception:
        emitter.error("generate")
        raise

    return App(
        code=code,
        version=version,
        intent=new_intent,
        parent_version=parent_version,
        model=model,
        growth_decision=decision,
        stages=emitter.stages,
    )


async def stream_evolve_pipeline(
    new_intent: str,
    current_code: str,
    original_intent: str,
    model: str,
    llm: LLMProvider,
    settings: ConversationSettings,
    version: int = 2,
    parent_version: int = 1,
    content: str | None = None,
) -> AsyncIterator[PipelineEvent]:
    """Streaming variant of evolve_pipeline."""
    emitter = PipelineEmitter()
    system_prompt = build_final_system_prompt(
        custom_instructions=settings.custom_instructions,
        include_design_system=settings.use_design_system,
    )

    emitter.start("generate")
    yield PipelineStageEvent(name="generate", status=StageStatus.RUNNING)

    growth_prompt = build_growth_prompt(
        current_code=current_code,
        original_intent=original_intent,
        new_intent=new_intent,
        system_prompt=system_prompt,
        custom_growth_rules=settings.growth_rules or None,
        content=content,
    )

    # Stream LLM tokens through TsxChunkFilter so the frontend only sees TSX
    # body, not the JSON decision envelope. `full_content` accumulates the raw
    # response so `_parse_growth_response` can extract the JSON below.
    full_content = ""
    tsx_filter = TsxChunkFilter()
    try:
        async for token in llm.stream(
            model,
            [Message(role="user", content=growth_prompt)],
            **reasoning_kwargs(settings.reasoning_effort),
        ):
            full_content += token
            for chunk in tsx_filter.feed(token):
                yield PipelineChunkEvent(data=chunk)
        for chunk in tsx_filter.finalize():
            yield PipelineChunkEvent(data=chunk)
    except Exception as exc:
        emitter.error("generate")
        yield PipelineStageEvent(name="generate", status=StageStatus.ERROR)
        yield PipelineErrorEvent(error=str(exc))
        return

    decision, code = _parse_growth_response(full_content)

    emitter.complete("generate")
    yield PipelineStageEvent(name="generate", status=StageStatus.COMPLETED)
    yield PipelineCompleteEvent(app=App(
        code=code,
        version=version,
        intent=new_intent,
        parent_version=parent_version,
        model=model,
        growth_decision=decision,
        stages=emitter.stages,
    ))


# ─── Progressive Evolve (diff-based) ──────────────────────────────


async def stream_evolve_pipeline_diff(
    new_intent: str,
    current_code: str,
    original_intent: str,
    model: str,
    llm: LLMProvider,
    settings: ConversationSettings,
    version: int = 2,
    parent_version: int = 1,
    content: str | None = None,
) -> AsyncIterator[PipelineEvent]:
    """Progressive evolve: LLM outputs search/replace diffs instead of full code.

    On each completed diff block, emits a PipelineSnapshotEvent with the full
    updated code so the frontend can render incrementally. If any diff fails
    to apply, falls back to the standard full-code evolve pipeline.
    """
    system_prompt = build_final_system_prompt(
        custom_instructions=settings.custom_instructions,
        include_design_system=settings.use_design_system,
    )

    emitter = PipelineEmitter()
    emitter.start("generate")
    yield PipelineStageEvent(name="generate", status=StageStatus.RUNNING)

    # Strip old highlight markers so LLM sees clean code and new markers
    # don't nest inside old ones. Previous versions keep their markers intact.
    clean_code = _HIGHLIGHT_ATTR_RE.sub("", current_code)

    growth_prompt = build_growth_prompt_diff(
        current_code=clean_code,
        original_intent=original_intent,
        new_intent=new_intent,
        system_prompt=system_prompt,
        custom_growth_rules=settings.growth_rules or None,
        content=content,
    )

    diff_filter = DiffChunkFilter(clean_code)
    try:
        async for token in llm.stream(
            model,
            [Message(role="user", content=growth_prompt)],
            **reasoning_kwargs(settings.reasoning_effort),
        ):
            for snapshot in diff_filter.feed(token):
                yield PipelineSnapshotEvent(code=snapshot)
        for snapshot in diff_filter.finalize():
            yield PipelineSnapshotEvent(code=snapshot)
    except Exception as exc:
        emitter.error("generate")
        yield PipelineStageEvent(name="generate", status=StageStatus.ERROR)
        yield PipelineErrorEvent(error=str(exc))
        return

    # Check if diff application succeeded
    if diff_filter.failed:
        logger.warning(
            "Progressive evolve failed (%s), falling back to full-code evolve",
            diff_filter.error,
        )
        # Don't emit ERROR stage — fallback is a seamless internal mechanism,
        # not a user-facing failure. Just continue with full-code evolve.
        async for event in stream_evolve_pipeline(
            new_intent=new_intent,
            current_code=clean_code,
            original_intent=original_intent,
            model=model,
            llm=llm,
            settings=settings,
            version=version,
            parent_version=parent_version,
            content=content,
        ):
            # Skip the duplicate "generate RUNNING" stage from the fallback —
            # we already emitted one at the start of this function
            if isinstance(event, PipelineStageEvent) and event.name == "generate" and event.status == StageStatus.RUNNING:
                continue
            yield event
        return

    # Success — parse growth decision from the raw response
    decision = _parse_growth_decision(diff_filter.raw_response)
    final_code = diff_filter.result_code

    # Debug: check if LLM actually added data-sac-changed markers
    raw_has_marker = "data-sac-changed" in diff_filter.raw_response
    current_has_marker = "data-sac-changed" in diff_filter._current_code
    logger.info(
        "Progressive evolve succeeded: %d blocks applied, "
        "raw_has_highlight=%s, code_has_highlight=%s",
        diff_filter.blocks_applied,
        raw_has_marker,
        current_has_marker,
    )

    emitter.complete("generate")
    yield PipelineStageEvent(name="generate", status=StageStatus.COMPLETED)
    yield PipelineCompleteEvent(app=App(
        code=final_code,
        version=version,
        intent=new_intent,
        parent_version=parent_version,
        model=model,
        growth_decision=decision,
        stages=emitter.stages,
    ))


# ─── Internal helpers ──────────────────────────────────────────────


def _parse_growth_decision(response: str) -> GrowthDecision:
    """Extract just the JSON growth decision from the LLM response."""
    decision = GrowthDecision(growth_type=GrowthType.EXTEND_CURRENT, reason="Default to extend")

    json_match = re.search(r"```json\s*([\s\S]*?)```", response)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1).strip())
            growth_type = parsed.get("growthType", "extend_current")
            if growth_type in ("extend_current", "new_page"):
                decision = GrowthDecision(
                    growth_type=GrowthType(growth_type),
                    reason=parsed.get("reason", "No reason provided"),
                    changes=parsed.get("changes", ""),
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    return decision


def _parse_growth_response(response: str) -> tuple[GrowthDecision, str]:
    """Parse unified growth response containing decision + code."""
    decision = GrowthDecision(growth_type=GrowthType.EXTEND_CURRENT, reason="Default to extend")

    json_match = re.search(r"```json\s*([\s\S]*?)```", response)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1).strip())
            growth_type = parsed.get("growthType", "extend_current")
            if growth_type in ("extend_current", "new_page"):
                decision = GrowthDecision(
                    growth_type=GrowthType(growth_type),
                    reason=parsed.get("reason", "No reason provided"),
                    changes=parsed.get("changes", ""),
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    code_match = re.search(r"```tsx\s*([\s\S]*?)```", response)
    code = code_match.group(1).strip() if code_match else response.strip()

    return decision, code
