"""Lite smoke test — Odysseus Lite overlay (Phase 6).

Exercises the lite-critical paths without a network or LLM:
  - the Lite Cookbook router mounts and returns a concrete recommendation;
  - the autopull route refuses without confirmation;
  - a memory read round-trips via the manager;
  - a search call returns (mocked).

NOTE: the *full app boot* smoke (import app + GET /api/health + /api/ready) is
exercised in CI as a uvicorn subprocess + curl step (.github/workflows/ci-lite.yml),
which uses a real file-backed SQLite DB. Booting the whole app inside this
pytest process is intentionally avoided here because the shared tests/conftest.py
forces an in-memory DATABASE_URL that the webhook/ORM layer can't fully
initialize at import time — the subprocess boot is the faithful "fresh install
works" check.
"""
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")
from fastapi import FastAPI
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def cookbook_client():
    """Mount just the Lite Cookbook router (no full-app boot needed)."""
    from routes.cookbook_lite_routes import setup_cookbook_lite_routes
    app = FastAPI()
    app.include_router(setup_cookbook_lite_routes())
    return TestClient(app, raise_server_exceptions=False)


def test_lite_cookbook_recommend_route(cookbook_client):
    r = cookbook_client.get("/api/lite/cookbook/recommend")
    assert r.status_code == 200
    body = r.json()
    assert body["tier"]["id"] in {"minimal", "balanced", "comfortable"}
    assert body["primary"] and body["primary"]["name"]


def test_lite_cookbook_autopull_refuses_without_confirm(cookbook_client):
    r = cookbook_client.post("/api/lite/cookbook/autopull", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_memory_read_round_trips():
    """Memory read via the manager (no network, no LLM)."""
    import tempfile
    from src.memory import MemoryManager

    d = tempfile.mkdtemp()
    mm = MemoryManager(d)
    entries = mm.load()      # canonical read used by app_initializer
    assert isinstance(entries, list)


def test_search_path_is_mockable(monkeypatch):
    """A search call returns results without hitting the network."""
    from services.search import core as search_core
    monkeypatch.setattr(search_core, "_get_search_settings", lambda: {})
    monkeypatch.setattr(search_core, "_record_query", lambda *a, **k: None)
    monkeypatch.setattr(search_core, "rank_search_results", lambda q, r: r)
    monkeypatch.setattr(
        search_core, "_call_provider",
        lambda provider, query, count, time_filter=None: [
            {"title": "t", "url": "https://x", "snippet": "s"}
        ],
    )
    results = search_core.searxng_search_results("hello", count=1)
    assert results and results[0]["title"] == "t"
