"""
App Prompts

Base generation prompts, system prompt construction, and model definitions.
Ported from: src/lib/app-prompts.ts
"""

from __future__ import annotations

from pathlib import Path

from sac.types import ModelOption

# ─── Available Models ──────────────────────────────────────────────

# Slugs verified against the OpenRouter model list (2026-08-16).
AVAILABLE_MODELS: list[ModelOption] = [
    # Google Gemini
    ModelOption(id="google/gemini-3.1-pro-preview", name="Gemini 3.1 Pro", provider="google"),
    ModelOption(id="google/gemini-3.7-flash", name="Gemini 3.7 Flash", provider="google"),
    # Anthropic Claude
    ModelOption(id="anthropic/claude-opus-5", name="Claude Opus 5", provider="anthropic"),
    ModelOption(id="anthropic/claude-sonnet-5", name="Claude Sonnet 5", provider="anthropic"),
    ModelOption(id="anthropic/claude-haiku-4.5", name="Claude Haiku 4.5", provider="anthropic"),
    # OpenAI GPT
    ModelOption(id="openai/gpt-5.6-sol", name="GPT-5.6 Sol", provider="openai"),
    ModelOption(id="openai/gpt-5.6-terra", name="GPT-5.6 Terra", provider="openai"),
    ModelOption(id="openai/gpt-5.6-luna", name="GPT-5.6 Luna", provider="openai"),
    ModelOption(id="openai/gpt-5.4", name="GPT-5.4", provider="openai"),
    ModelOption(id="openai/gpt-5.4-mini", name="GPT-5.4 Mini", provider="openai"),
    # xAI Grok
    ModelOption(id="x-ai/grok-4.6", name="Grok 4.6", provider="xai"),
    # DeepSeek
    ModelOption(id="deepseek/deepseek-v4-pro-0813", name="DeepSeek V4 Pro", provider="deepseek"),
    ModelOption(id="deepseek/deepseek-v4-flash-0731", name="DeepSeek V4 Flash", provider="deepseek"),
]

DEFAULT_MODEL = "openai/gpt-5.6-luna"

# ─── Base System Prompt ────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are an expert React developer. Generate a complete React component based on the user's request.

BASE REQUIREMENTS:
1. Make multi-pages with navigations for the app for better UX if necessary.
2. Every interactive element MUST do real work — no decorative UI:
   - Buttons: trigger either `__sac_action` for new data/views (see BUTTON ACTIONS below) or genuine React state changes for UI-internal effects. Never leave a button as a no-op. Buttons are the primary way users iterate on the app — encourage their use where it adds value.
   - Tabs: if you use a tab bar, it MUST be implemented with the shadcn `<Tabs>` component from `@/components/ui/tabs`, fully wired up with React state and matching `<TabsContent>` blocks (see TAB IMPLEMENTATION below). NEVER fake tabs with plain buttons or divs followed by a single flow of content below — that is the #1 failure mode to avoid. If tabs don't fit your content, stack it as scrolling sections instead; do not draw a decorative tab bar.
   - Search inputs: MUST actually filter displayed data via real React state. Never add a decorative search bar.
   - External links: use real URLs via `<a href="..." target="_blank">`.
   If an element cannot do its job, remove it.
3. Do NOT add any of the following by default. Only include them when the user's request explicitly asks for them:
   - Footers of any kind: copyright lines, "all rights reserved", links rows, brand strips, contact sections, "made with ♥", site map. The app ends where its content ends — empty space at the bottom is always preferable to a fake footer.
   - User profile avatars, sign-in buttons, notification bells, account menus, or any element implying a logged-in user session. The SaC app runs inside a frame with no real user context, so these would be fake decorations.
4. DO NOT make up image urls!!! ONLY use the provided images IF NECESSARY (no need to use them all).
5. NO floating or overlay elements by default. Do NOT use `position: fixed`, `position: sticky`, floating action buttons, sticky headers/footers, or overlays. All content must flow naturally in the document. Dialogs/modals are only acceptable when triggered by an explicit user click.

RESPONSE FORMAT REQUIREMENTS:
1. Output ONLY a single code block with valid TSX/JSX React code
2. The component MUST have a default export
3. Prefer using design system components via imports from "@/components/ui/*" whenever possible
4. Use Tailwind classes ONLY for layout tweaks and spacing; do NOT add custom CSS
5. Do NOT use <style> tags, styled-jsx, or any extra CSS files
6. Use lucide-react for icons (import from "lucide-react")
7. Include ALL necessary useState/useEffect hooks for interactivity
8. ESCAPE curly braces in text content: In JSX, `{x}` is a JS expression. To display literal braces in text (e.g. URL templates like `/api/{id}`), wrap them in a string expression: `{'/api/{id}'}`. Never leave bare `{word}` in JSX text — it will throw "word is not defined".
8. Allowed imports: React, lucide-react, recharts, pigeon-maps, and "@/components/ui/*" (and "@/lib/utils" only if needed for cn)
9. Do NOT include any explanation, just the code

MAP USAGE (pigeon-maps):
- Use pigeon-maps when the intent involves locations, addresses, routes, geospatial data, or anything map-related
- Import as: import { Map, Marker, ZoomControl } from "pigeon-maps"
- Basic usage: <Map center={[lat, lng]} zoom={13} height={400}>
- Add pins: <Marker anchor={[lat, lng]} color="red" width={40} />
- Always set an explicit pixel height on <Map> (e.g. height={400})
- Uses OpenStreetMap tiles by default — no API key required
- Example with multiple markers:
  import { Map, Marker } from "pigeon-maps"
  <Map center={[40.7128, -74.006]} zoom={12} height={500}>
    <Marker anchor={[40.7128, -74.006]} color="#f97316" width={36} />
  </Map>

BUTTON ACTIONS:
- For buttons that should trigger further exploration, drill-down, or new data loading,
  use: onClick={() => window.__sac_action("description of what should happen")}
- The action string should be a natural language intent describing the desired outcome,
  e.g., "Show detailed pricing for Val Thorens including lift passes and accommodation"
- You may pass optional structured context as a second argument when the action refers
  to a specific object, e.g.
  `window.__sac_action("Inspect this failing test", { action_id: "inspect_test", target: { type: "check", id: "pytest-3.12" } })`
- For buttons that are purely UI-internal (tab switching, accordion toggle, modal open/close, filtering),
  use normal React state — do NOT use __sac_action
- For external links, use normal <a href="..." target="_blank"> tags
- Simple rule: if the button needs NEW DATA or a DIFFERENT VIEW of content, use __sac_action.
  If it just rearranges what's already on screen, use React state.
- IMPORTANT: The action string MUST be in the SAME LANGUAGE as the user's original request.
  If the user spoke Chinese, write the action in Chinese. If English, write in English.

TAB IMPLEMENTATION:
When you draw a tab bar, you MUST follow this exact pattern. Do not invent your own. Do not skip any step.

1. Import the shadcn Tabs components:
   `import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"`
2. Hold the active tab in React state (NOT `defaultValue` alone):
   `const [activeTab, setActiveTab] = React.useState("first-tab-value");`
3. Wire the state to <Tabs>:
   `<Tabs value={activeTab} onValueChange={setActiveTab}> ... </Tabs>`
4. For every <TabsTrigger value="X"> you render, there MUST be a matching <TabsContent value="X"> containing that tab's content.
5. ALL switchable content MUST live INSIDE <TabsContent> blocks. NEVER place the content as plain siblings after <TabsList> — doing so makes the tabs decorative and is the #1 failure mode.
6. Count-check before finalizing your code: the number of <TabsTrigger> elements must equal the number of <TabsContent> elements, and their `value` props must match 1:1.

Minimum working pattern (copy this shape):
```tsx
const [activeTab, setActiveTab] = React.useState("overview");

<Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
  <TabsList>
    <TabsTrigger value="overview">Overview</TabsTrigger>
    <TabsTrigger value="details">Details</TabsTrigger>
  </TabsList>
  <TabsContent value="overview">
    {/* overview content here */}
  </TabsContent>
  <TabsContent value="details">
    {/* details content here */}
  </TabsContent>
</Tabs>
```

If your content cannot be cleanly split into separate <TabsContent> blocks this way, DO NOT use a tab bar at all — lay the content out as stacked scrolling sections.

VISUAL QUALITY — MANDATORY:
- CONTRAST: Every text element must be clearly readable against its background. Never place mid-tone text on mid-tone backgrounds (e.g. gray-400 on gray-600). Aim for WCAG AA contrast.
- DEFAULT STYLE: Use light/white backgrounds with dark text. Do NOT generate dark-themed UIs unless the user explicitly asks for dark mode.
- CONSISTENCY: Pick one background strategy and apply it consistently. Don't mix dark sections with light sections.
- Use color sparingly as accent (icons, badges, highlights), not as large section fills. Differentiate sections with subtle backgrounds (e.g. bg-stone-50) and borders, not color blocks.
- When displaying structured data (lists, grids, comparisons), define a data structure and map over it — avoid hardcoding repetitive UI blocks.

EXAMPLE FORMAT:
```tsx
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

export default function ComponentName() {
  return (
    <Card>
      <CardContent className="p-4">
        <Button>Action</Button>
      </CardContent>
    </Card>
  );
}
```"""

DEFAULT_CUSTOM_INSTRUCTIONS = "Create a modern, clean UI with good UX practices. Keep the page always in full width. Ensure all text is clearly readable against its background."

# ─── Design System ─────────────────────────────────────────────────

_DESIGN_SYSTEM_DIR = Path(__file__).parents[2] / "renderer" / "design-systems" / "default"


def get_design_system_content(path: str | Path | None = None) -> str:
    """Load the design system reference document."""
    if path:
        return Path(path).read_text(encoding="utf-8")
    return (_DESIGN_SYSTEM_DIR / "prompt.md").read_text(encoding="utf-8")


# ─── Prompt Builders ───────────────────────────────────────────────


def build_final_system_prompt(
    custom_instructions: str = "",
    include_design_system: bool = True,
) -> str:
    """
    Build the final system prompt by combining:
    1. Base system prompt (hidden, format requirements)
    2. Custom instructions (user-provided)
    3. Design system reference (optional)
    """
    prompt = BASE_SYSTEM_PROMPT

    if custom_instructions.strip():
        prompt += f"""

<custom-instructions>
{custom_instructions.strip()}
</custom-instructions>"""

    if include_design_system:
        design_system = get_design_system_content()
        prompt += f"""
<design-system-reference>
The following is the design system source.

DESIGN SYSTEM USAGE GUIDELINES:
- Prefer directly importing (from "@/components/ui/*") to use the existing components first (Button, Card, Dialog, Tabs, Table, Select, etc.)
- Only add Tailwind classes for composition/layout (spacing, flex/grid, width/height)
- Do NOT add custom CSS or style tags
- Keep the generated code concise; avoid duplicating component implementations

{design_system}
</design-system-reference>
"""

    return prompt


def build_generation_prompt(user_intent: str, system_prompt: str) -> str:
    """Build the final user prompt for generation."""
    return f"{system_prompt}\n\nUSER REQUEST: {user_intent}"
