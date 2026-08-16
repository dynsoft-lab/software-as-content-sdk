"""
Search Query Extraction Prompt — used by the bundled agent to derive
web search queries from a user's intent before fetching real-time data.

Lives at the agent layer (not core) because deciding what to search is
"info essential" — the agent's job, not the renderer's.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _get_current_date_info() -> dict[str, object]:
    """Get the current date formatted for prompts."""
    now = datetime.now(timezone.utc)
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    return {
        "year": now.year,
        "month": months[now.month - 1],
        "month_num": now.month,
        "day": now.day,
        "full_date": f"{months[now.month - 1]} {now.day}, {now.year}",
    }


def get_search_query_extraction_prompt() -> str:
    """Build the search query extraction prompt with current date."""
    date = _get_current_date_info()

    return f"""You are an expert at understanding user requests and determining what information needs to be searched on the web.

CURRENT DATE: {date['full_date']}
CURRENT YEAR: {date['year']}

Given a user's request for a UI component, analyze what real-world data would be needed and generate appropriate search queries.

RULES:
1. Generate 1-5 search queries that would retrieve the most relevant real-time data
2. Each query should be specific and focused on retrieving factual, up-to-date information
3. IMPORTANT: Include the current year ({date['year']}) in queries when searching for recent/current information
4. Consider what data the UI would display and search for that specific information
5. Output ONLY valid JSON, no explanations
6. NO-SEARCH CASES: If the request needs NO real-world data — pure UI/layout/style changes ("make it darker", "rearrange the tabs", "bigger font"), edits that reuse data already in the app, or bug fixes — output {{"queries": []}} so no search runs.
7. CONTEXT DISAMBIGUATION: When a CONVERSATION CONTEXT is provided, the NEW REQUEST is a follow-up within that context — resolve ambiguous or elliptical requests against it (e.g. in a Norway trip conversation, "stay in CBD" means the city centre of the Norwegian destination, NOT Melbourne/Sydney CBD). Include the disambiguating place/subject from the context in your queries. Only ignore the context when the new request explicitly introduces a different topic.

OUTPUT FORMAT:
{{
  "queries": [
    {{ "query": "search query here", "purpose": "what this data will be used for in the UI" }}
  ]
}}

EXAMPLES:

User: "Create a dashboard showing Tesla stock performance"
Output:
{{
  "queries": [
    {{ "query": "Tesla TSLA stock price today {date['year']}", "purpose": "current stock price and daily change" }},
    {{ "query": "Tesla stock performance last 6 months {date['year']}", "purpose": "historical data for charts" }},
    {{ "query": "Tesla latest news {date['month']} {date['year']}", "purpose": "recent news and events" }}
  ]
}}

User: "Build a weather widget for San Francisco"
Output:
{{
  "queries": [
    {{ "query": "San Francisco weather today forecast", "purpose": "current weather conditions" }},
    {{ "query": "San Francisco 7 day weather forecast", "purpose": "weekly forecast data" }}
  ]
}}

User: "Show me a comparison of iPhone 16 vs Samsung S25"
Output:
{{
  "queries": [
    {{ "query": "iPhone 16 specifications price {date['year']}", "purpose": "iPhone specs and pricing" }},
    {{ "query": "Samsung Galaxy S25 specifications price {date['year']}", "purpose": "Samsung specs and pricing" }},
    {{ "query": "iPhone 16 vs Samsung S25 comparison review {date['year']}", "purpose": "comparison insights" }}
  ]
}}"""
