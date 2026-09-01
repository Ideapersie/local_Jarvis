"""Web search: response shaping, and what happens when it cannot answer.

No network. Both providers are exercised against fake payloads, because the
part that breaks is the translation from each provider's JSON into the flat
text the model reads, not the HTTP call itself.
"""

from __future__ import annotations

import httpx
import pytest

from app import config
from app.integrations import search


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# --- no key configured ------------------------------------------------------


def test_reports_unavailable_rather_than_returning_nothing(monkeypatch):
    """An empty result set invites the model to invent an interview process.

    Telling it plainly that search is unavailable gets an admission instead,
    which is the behaviour brain/weaknesses.md's "never invent an entry" rule
    depends on.
    """
    monkeypatch.setattr(config, "SEARCH_API_KEY", None)
    result = search.web_search({"query": "Palantir interview process"})
    assert not result.get("is_error")
    body = _text(result)
    assert "not configured" in body
    assert "do not know" in body


def test_unknown_provider_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_API_KEY", "key")
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "altavista")
    assert "not configured" in _text(search.web_search({"query": "x"}))


def test_empty_query_is_an_error():
    result = search.web_search({"query": "   "})
    assert result["is_error"] is True


# --- provider payloads ------------------------------------------------------


def test_tavily_payload_is_flattened(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_API_KEY", "key")
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "title": "Palantir interviews",
                        "url": "https://example.com/a",
                        "content": "Three rounds, one take-home.",
                    }
                ]
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    body = _text(search.web_search({"query": "Palantir"}))
    assert "Palantir interviews" in body
    assert "https://example.com/a" in body
    assert "Three rounds" in body


def test_brave_payload_is_flattened(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_API_KEY", "key")
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "brave")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "web": {
                    "results": [
                        {
                            "title": "Grad scheme",
                            "url": "https://example.com/b",
                            "description": "Applications close in October.",
                        }
                    ]
                }
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    body = _text(search.web_search({"query": "grad scheme"}))
    assert "Grad scheme" in body
    assert "https://example.com/b" in body


def test_results_are_capped(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_API_KEY", "key")
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {"title": f"t{i}", "url": f"https://e/{i}", "content": "x"}
                    for i in range(50)
                ]
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    body = _text(search.web_search({"query": "many"}))
    assert body.count("https://e/") == search.MAX_RESULTS


# --- failure ----------------------------------------------------------------


def test_a_failed_search_does_not_kill_the_turn(monkeypatch):
    """Interview prep should continue and admit it could not check."""
    monkeypatch.setattr(config, "SEARCH_API_KEY", "key")
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")

    def _boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", _boom)
    result = search.web_search({"query": "anything"})
    assert not result.get("is_error")
    assert "could not check" in _text(result)


def test_no_results_says_so(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_API_KEY", "key")
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    assert "No results" in _text(search.web_search({"query": "obscure"}))


# --- spec -------------------------------------------------------------------


def test_spec_is_closed_and_names_the_citation_rule():
    fn = search.SPEC["function"]
    assert fn["name"] == "web_search"
    assert fn["parameters"]["additionalProperties"] is False
    assert "cite" in fn["description"].lower()


@pytest.mark.parametrize("provider", sorted(search.PROVIDERS))
def test_every_named_provider_is_callable(provider):
    assert callable(search.PROVIDERS[provider])
