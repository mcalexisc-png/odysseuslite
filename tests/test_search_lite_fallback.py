"""Web search hardening — Odysseus Lite overlay (Phase 4).

The default DuckDuckGo path is rate-limited and breaks silently. Upstream
already has provider chaining + caching; the lite gaps are:
  - honor SEARCH_PROVIDER / SEARCH_FALLBACK_ORDER env on a fresh box (upstream
    only reads admin settings, which default to searxng — which lite omits);
  - fall back automatically and log which provider served;
  - surface a non-silent error instead of an empty "no results" list.

These tests mock the provider calls so they never hit the network.
"""
import os

import pytest

from services.search import core as search_core


@pytest.fixture(autouse=True)
def _no_admin_settings(monkeypatch):
    """Pretend there's no admin search config so env defaults apply."""
    monkeypatch.setattr(search_core, "_get_search_settings", lambda: {})
    # Avoid touching the real cache dir / analytics during the test.
    monkeypatch.setattr(search_core, "_record_query", lambda *a, **k: None)
    yield


def test_lite_env_provider_becomes_primary(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    # admin settings empty -> upstream would pick "searxng"; lite picks env.
    assert search_core._effective_primary("searxng") == "duckduckgo"


def test_admin_setting_wins_over_env(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(search_core, "_get_search_settings",
                        lambda: {"search_provider": "brave"})
    # admin pinned brave -> env must not override
    assert search_core._effective_primary("brave") == "brave"


def test_fallback_chain_from_env(monkeypatch):
    monkeypatch.setenv("SEARCH_FALLBACK_ORDER", "brave, tavily ,searxng")
    chain = search_core._build_provider_chain("duckduckgo")
    assert chain == ["duckduckgo", "brave", "tavily", "searxng"]


def test_fallback_used_when_primary_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("SEARCH_FALLBACK_ORDER", "duckduckgo")
    monkeypatch.setattr(search_core, "SEARCH_CACHE_DIR", tmp_path)

    calls = []

    def fake_call(provider, query, count, time_filter=None):
        calls.append(provider)
        if provider == "brave":
            return []                       # primary returns nothing
        if provider == "duckduckgo":
            return [{"title": "ok", "url": "https://x", "snippet": "s"}]
        return []

    monkeypatch.setattr(search_core, "_call_provider", fake_call)
    monkeypatch.setattr(search_core, "rank_search_results", lambda q, r: r)

    results = search_core.searxng_search_results("anything")
    assert results and results[0]["title"] == "ok"
    assert calls[0] == "brave" and "duckduckgo" in calls   # tried primary, then fell back


def test_all_fail_surfaces_error_in_lite(monkeypatch, tmp_path):
    monkeypatch.setenv("LITE_MODE", "true")
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setenv("SEARCH_FALLBACK_ORDER", "brave")
    monkeypatch.setattr(search_core, "SEARCH_CACHE_DIR", tmp_path)

    def fake_call(provider, query, count, time_filter=None):
        raise search_core.NetworkError(f"{provider} down")

    monkeypatch.setattr(search_core, "_call_provider", fake_call)

    results = search_core.searxng_search_results("anything")
    assert len(results) == 1
    assert results[0]["error"] is True
    assert "duckduckgo" in results[0]["providers_tried"]
    assert "brave" in results[0]["providers_tried"]
    # must NOT look like an empty "no matches"
    assert results[0]["snippet"]


def test_all_fail_non_lite_keeps_empty_contract(monkeypatch, tmp_path):
    monkeypatch.delenv("LITE_MODE", raising=False)
    monkeypatch.setenv("SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(search_core, "SEARCH_CACHE_DIR", tmp_path)

    monkeypatch.setattr(search_core, "_call_provider",
                        lambda *a, **k: (_ for _ in ()).throw(search_core.NetworkError("x")))
    results = search_core.searxng_search_results("anything")
    assert results == []
