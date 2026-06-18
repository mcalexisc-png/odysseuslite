"""Hybrid semantic memory — Odysseus Lite overlay (Phase 2).

Proves the lite-specific contract:
  - MEMORY_BACKEND=hybrid recalls semantically related memories that share NO
    literal keywords with the query (the whole point — keyword BM25 alone can't);
  - the store degrades to keyword-only (healthy=False / initializer returns None)
    when the embedding model is unavailable, without crashing;
  - the reciprocal-rank-fusion helper behaves.

Tests that need the real model are skipped when sqlite-vec / model2vec aren't
installed, so a minimal CI without the opt-in deps stays green while the
fallback + fusion logic is always exercised.
"""
import importlib.util
import os
import tempfile

import pytest

from services.memory_hybrid import reciprocal_rank_fusion, cosine

_HAS_VEC = importlib.util.find_spec("sqlite_vec") is not None
_HAS_M2V = importlib.util.find_spec("model2vec") is not None
requires_embed = pytest.mark.skipif(
    not (_HAS_VEC and _HAS_M2V),
    reason="sqlite-vec / model2vec not installed (opt-in hybrid deps)",
)


# ── always runs ──────────────────────────────────────────────────────────

def test_rrf_fuses_rankings():
    # An item ranked #1 in one list and #1 again in the other must beat an item
    # that is only ever mid-ranked. RRF reward is 1/(k+rank+1), so a top hit in
    # both lists dominates.
    fused = dict(reciprocal_rank_fusion(["a", "b", "c"], ["a", "c", "b"]))
    assert set(fused) == {"a", "b", "c"}
    assert fused["a"] > fused["b"]
    assert fused["a"] > fused["c"]

    # Convexity check: with mirror-image rankings the two extremes tie and both
    # exceed the always-middle item (1/(k+1)+1/(k+3) > 2/(k+2)).
    mirror = dict(reciprocal_rank_fusion(["a", "b", "c"], ["c", "b", "a"]))
    assert mirror["a"] == pytest.approx(mirror["c"])
    assert mirror["a"] > mirror["b"]


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1]) == 0.0


def test_provider_none_degrades(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "none")
    from services.memory_hybrid import SqliteVecMemoryStore
    store = SqliteVecMemoryStore(tempfile.mkdtemp())
    assert store.healthy is False
    assert store.search("anything") == []          # safe no-op, no crash


def test_initializer_falls_back_to_keyword(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "none")
    from src.app_initializer import _init_memory_vector
    from src.memory import MemoryManager
    d = tempfile.mkdtemp()
    mv = _init_memory_vector(d, MemoryManager(d), None)
    assert mv is None          # keyword-only


def test_initializer_sqlite_fts_skips_vector(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite_fts")
    from src.app_initializer import _init_memory_vector
    from src.memory import MemoryManager
    d = tempfile.mkdtemp()
    assert _init_memory_vector(d, MemoryManager(d), None) is None


# ── needs the real embedding model ────────────────────────────────────────

@requires_embed
def test_semantic_recall_no_shared_keywords(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    from services.memory_hybrid import SqliteVecMemoryStore
    store = SqliteVecMemoryStore(tempfile.mkdtemp())
    assert store.healthy

    store.add("m1", "The user's dog is named Biscuit and loves the park.")
    store.add("m2", "User works as a marine biologist studying coral reefs.")
    store.add("m3", "Favourite meal is spicy ramen with extra chili oil.")
    store.add("m4", "The capital of France is Paris.")
    assert store.count() == 4

    # Paraphrase sharing no literal keywords with m2.
    hits = store.search("What is their profession in the ocean sciences?", k=3)
    assert hits and hits[0]["memory_id"] == "m2"

    hits2 = store.search("Tell me about the pet.", k=2)
    assert hits2[0]["memory_id"] == "m1"


@requires_embed
def test_add_remove_and_dedupe(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    from services.memory_hybrid import SqliteVecMemoryStore
    store = SqliteVecMemoryStore(tempfile.mkdtemp())
    store.add("a", "Paris is the capital of France.")
    assert store.count() == 1
    # near-duplicate detection
    dup = store.find_similar("Paris is the capital of France.", threshold=0.9)
    assert dup == "a"
    # update in place (same id) keeps count at 1
    store.add("a", "Paris is the capital of France, a large city.")
    assert store.count() == 1
    store.remove("a")
    assert store.count() == 0


@requires_embed
def test_rebuild_reindexes(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    from services.memory_hybrid import SqliteVecMemoryStore
    store = SqliteVecMemoryStore(tempfile.mkdtemp())
    store.rebuild([
        {"id": "x", "text": "hello world"},
        {"id": "y", "text": "goodbye moon"},
        {"id": "z", "text": ""},          # empty -> skipped
    ])
    assert store.count() == 2
