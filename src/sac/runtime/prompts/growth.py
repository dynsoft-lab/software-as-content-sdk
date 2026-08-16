"""
Growth Prompt

Builds the unified prompt that decides growth type AND emits new code in one
LLM call. Receives data via the `content` parameter (caller-provided string);
core renderer is content-shape-agnostic.
"""

from __future__ import annotations

DEFAULT_GROWTH_RULES = """**EXTEND CURRENT PAGE** when:
- New data complements existing content (same domain/topic)
- Can be added as new section, component, or UI element
- Won't clutter or confuse the current layout

**ADD NEW PAGE** when:
- New data represents a fundamentally different topic
- Would require completely different UI structure
- Deserves its own dedicated view
- If adding a new page, also add navigation (tabs, menu, etc.) to switch between pages"""


def build_growth_prompt(
    current_code: str,
    original_intent: str,
    new_intent: str,
    system_prompt: str,
    custom_growth_rules: str | None = None,
    content: str | None = None,
) -> str:
    """Unified prompt: decide growth type AND emit new code."""
    new_data_section = content.strip() if content else "No new data provided"
    growth_rules = (custom_growth_rules or "").strip() or DEFAULT_GROWTH_RULES

    return f"""{system_prompt}

You are growing an existing agentic app based on a new user intent and new data.

=== CURRENT APP ===
Original Intent: {original_intent}

Current Code:
```tsx
{current_code}
```

=== NEW REQUEST ===
New Intent: {new_intent}

New Data:
{new_data_section}

=== GROWTH RULES ===
Decide how to integrate the new content:

{growth_rules}

=== OUTPUT REQUIREMENTS ===
1. First, output a JSON block with your decision:
```json
{{
  "growthType": "extend_current" | "new_page",
  "reason": "Brief explanation",
  "changes": "User-facing summary of what changed (same language as user intent)"
}}
```

2. Then output the complete updated React component code:
```tsx
// Your complete updated code here
```

IMPORTANT:
- CONTEXT GUARD: The New Intent is a follow-up within the existing app's context (see Original Intent). If the New Data's subject conflicts with the app's topic (e.g. data about a different city, product, or domain), assume it came from an ambiguous search: keep the app's topic authoritative, reinterpret the New Intent within it, and use only the parts of the data that fit. Do NOT pivot the app to the data's topic unless the New Intent itself explicitly asks for the new topic.
- Keep ALL existing functionality intact
- Maintain consistent styling
- Output BOTH the JSON decision AND the complete code
- DATA RENDERING — CRITICAL: When "New Data" is provided above, you MUST render its actual content visually in the UI. Define the data as a JS array/object and map over it to display real values. NEVER just change a button label or add a stub — the user expects to SEE the data on screen. If the data is long, use tabs, accordion, or scrollable sections — but always render it inline in the page, NOT in modals or overlays.
- BUTTON PRESERVATION: If the current code uses `window.__sac_action(...)` for a button, keep it as `__sac_action` unless the new data fully replaces what that button would fetch. Do NOT convert action buttons into local state toggles unless the data they requested is now rendered inline.
- Add a small "NEW" badge (e.g. <Badge>NEW</Badge>) next to newly added tabs, sections, or major content blocks so the user can instantly spot what's new. Remove any such badges from previously existing content.
- CRITICAL: Add the attribute data-sac-changed to the outermost JSX element(s) that are newly added or meaningfully modified. This enables visual change-highlighting. Only add it to elements that actually changed. Example: `<Card data-sac-changed className="...">`. Do NOT add it to unchanged elements."""


def build_growth_prompt_diff(
    current_code: str,
    original_intent: str,
    new_intent: str,
    system_prompt: str,
    custom_growth_rules: str | None = None,
    content: str | None = None,
) -> str:
    """Unified prompt: decide growth type AND emit search/replace diffs.

    Unlike build_growth_prompt which asks for the full updated code, this
    variant asks the LLM to output only the changes as search/replace blocks.
    This saves output tokens and prevents quality degradation on large apps.
    """
    new_data_section = content.strip() if content else "No new data provided"
    growth_rules = (custom_growth_rules or "").strip() or DEFAULT_GROWTH_RULES

    return f"""{system_prompt}

You are growing an existing agentic app based on a new user intent and new data.

=== CURRENT APP ===
Original Intent: {original_intent}

Current Code:
```tsx
{current_code}
```

=== NEW REQUEST ===
New Intent: {new_intent}

New Data:
{new_data_section}

=== GROWTH RULES ===
Decide how to integrate the new content:

{growth_rules}

=== OUTPUT REQUIREMENTS ===
1. First, output a JSON block with your decision:
```json
{{
  "growthType": "extend_current" | "new_page",
  "reason": "Brief explanation",
  "changes": "User-facing summary of what changed (same language as user intent)"
}}
```

2. Then output ONLY the changes as search/replace blocks. Do NOT output the full code.

Each change is a search/replace block in this exact format:

<<<<<<< SEARCH
exact lines from the current code to find
=======
replacement lines
>>>>>>> REPLACE

Rules for search/replace blocks:
- The SEARCH section must match EXACTLY (including whitespace and indentation) a contiguous block of lines in the current code
- Include enough surrounding context lines (3-5 lines before and after the change) in SEARCH to uniquely identify the location
- You may output multiple blocks — they are applied sequentially to the code
- For insertions: include the lines just before the insertion point in SEARCH, then include those same lines plus the new lines in REPLACE
- For deletions: put the lines to delete in SEARCH and leave REPLACE empty
- CONTEXT GUARD: The New Intent is a follow-up within the existing app's context (see Original Intent). If the New Data's subject conflicts with the app's topic (e.g. data about a different city, product, or domain), assume it came from an ambiguous search: keep the app's topic authoritative, reinterpret the New Intent within it, and use only the parts of the data that fit. Do NOT pivot the app to the data's topic unless the New Intent itself explicitly asks for the new topic.
- Keep ALL existing functionality intact
- Maintain consistent styling
- DATA RENDERING — CRITICAL: When "New Data" is provided above, you MUST render its actual content visually in the UI. Define the data as a JS array/object and map over it to display real values. NEVER just change a button label or add a stub — the user expects to SEE the data on screen. If the data is long, use tabs, accordion, or scrollable sections — but always render it inline in the page, NOT in modals or overlays.
- BUTTON PRESERVATION: If the current code uses `window.__sac_action(...)` for a button, keep it as `__sac_action` unless the new data fully replaces what that button would fetch. Do NOT convert action buttons into local state toggles unless the data they requested is now rendered inline.
- Add a small "NEW" badge (e.g. <Badge>NEW</Badge>) next to newly added tabs, sections, or major content blocks
- CRITICAL — CHANGE MARKERS (you MUST do this for every REPLACE block): Add the attribute data-sac-changed to the outermost JSX element(s) that are newly added or meaningfully modified. This enables visual change-highlighting in the preview. Without it, users cannot see what changed. Only add it to elements that actually changed, NOT to unchanged context lines. Example: `<Card data-sac-changed className="...">` or `<div data-sac-changed>`. Do NOT add data-sac-changed to the SEARCH section."""
