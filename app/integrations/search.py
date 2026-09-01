"""Web search, replacing the SDK's WebSearch tool.

The interview-prep skill researches a firm's actual interview process and is
told to cite URLs and admit when it does not know. That is the one place in this
project where the answer genuinely cannot come from the database or brain/, so
losing web access would quietly turn a specific prep plan into a generic one.

Two providers because their free tiers differ and neither is obviously better:
Tavily returns answer-shaped extracts, Brave returns a plain result list. Both
are read-only and cannot change anything, which is why the loop can call this
without a permission prompt.

With no key configured this returns an explicit "search is unavailable" result
rather than raising or returning nothing. A model that is told the tool is
unavailable will say so; one that gets an empty list tends to invent a plausible
interview process instead, which is worse than an admission.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app import config

log = logging.getLogger("jarvis.search")

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

MAX_RESULTS = 5
SNIPPET_CHARS = 400
TIMEOUT_S = 20.0


def available() -> bool:
    return bool(config.SEARCH_API_KEY)


def _unavailable() -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "Web search is not configured, so I cannot look this up. "
                    "Say that you do not know rather than guessing."
                ),
            }
        ]
    }


def _format(results: list[dict[str, str]], query: str) -> dict[str, Any]:
    if not results:
        return {"content": [{"type": "text", "text": f"No results for {query!r}."}]}
    lines = []
    for r in results[:MAX_RESULTS]:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        body = " ".join((r.get("body") or "").split())[:SNIPPET_CHARS]
        lines.append(f"{title}\n{url}\n{body}")
    return {"content": [{"type": "text", "text": "\n\n".join(lines)}]}


def _tavily(query: str) -> list[dict[str, str]]:
    r = httpx.post(
        TAVILY_URL,
        json={
            "api_key": config.SEARCH_API_KEY,
            "query": query,
            "max_results": MAX_RESULTS,
            "search_depth": "basic",
        },
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    return [
        {
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "body": x.get("content", ""),
        }
        for x in r.json().get("results", [])
    ]


def _brave(query: str) -> list[dict[str, str]]:
    r = httpx.get(
        BRAVE_URL,
        params={"q": query, "count": MAX_RESULTS},
        headers={
            "X-Subscription-Token": config.SEARCH_API_KEY or "",
            "Accept": "application/json",
        },
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    return [
        {
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "body": x.get("description", ""),
        }
        for x in r.json().get("web", {}).get("results", [])
    ]


PROVIDERS = {"tavily": _tavily, "brave": _brave}


def web_search(args: dict[str, Any]) -> dict[str, Any]:
    """Search the web. Never raises: the turn continues without the answer."""
    query = str(args.get("query", "")).strip()
    if not query:
        return {
            "content": [{"type": "text", "text": "web_search needs a query."}],
            "is_error": True,
        }
    if not available():
        return _unavailable()

    fetch = PROVIDERS.get(config.SEARCH_PROVIDER)
    if fetch is None:
        log.warning("unknown SEARCH_PROVIDER %r", config.SEARCH_PROVIDER)
        return _unavailable()

    try:
        return _format(fetch(query), query)
    except httpx.HTTPError as exc:
        # A failed search must not kill an interview-prep run. Tell the model
        # plainly so it says it could not check, rather than inventing.
        log.warning("search failed: %s", exc)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"The search for {query!r} failed. Say you could not check."
                    ),
                }
            ]
        }


SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current, public information such as a company's "
            "interview process or a role's requirements. Use this when the answer "
            "is about the outside world and cannot come from the database or the "
            "brain/ files. Always cite the URLs it returns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}
