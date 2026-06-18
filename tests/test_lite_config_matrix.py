"""Config-flag matrix — Odysseus Lite overlay (Phase 6).

Exercises the lite configuration surface so a flag change can't silently break a
mode. Covers: each AGENT_SHELL_MODE, each MEMORY_BACKEND (degradation path), and
the search-provider default resolution. These are fast, no-network checks of the
selection logic — not full boots.
"""
import importlib
import tempfile

import pytest


# ── AGENT_SHELL_MODE matrix ─────────────────────────────────────────────────

@pytest.mark.parametrize("mode,expect_tool,expect_shell", [
    ("sandboxed", True, True),
    ("restricted", True, False),
    ("off", False, False),
    ("unrestricted", True, True),     # tool available; sandbox bypass needs ACK
    ("garbage", True, True),          # invalid -> sandboxed
])
def test_shell_mode_matrix(monkeypatch, mode, expect_tool, expect_shell):
    monkeypatch.setenv("AGENT_SHELL_MODE", mode)
    monkeypatch.delenv("AGENT_SHELL_ACK_UNSAFE", raising=False)
    from services.shell import sandbox
    sandbox.reset_config_cache()
    cfg = sandbox.load_config(force=True)
    assert cfg.tool_available is expect_tool
    assert cfg.shell_enabled is expect_shell


def test_unrestricted_needs_ack(monkeypatch):
    from services.shell import sandbox
    monkeypatch.setenv("AGENT_SHELL_MODE", "unrestricted")
    monkeypatch.delenv("AGENT_SHELL_ACK_UNSAFE", raising=False)
    sandbox.reset_config_cache()
    assert sandbox.load_config(force=True).effective_unrestricted is False
    monkeypatch.setenv("AGENT_SHELL_ACK_UNSAFE", "true")
    sandbox.reset_config_cache()
    assert sandbox.load_config(force=True).effective_unrestricted is True


# ── MEMORY_BACKEND matrix ───────────────────────────────────────────────────

def test_memory_backend_sqlite_fts_is_keyword_only(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite_fts")
    from src.app_initializer import _init_memory_vector
    from src.memory import MemoryManager
    d = tempfile.mkdtemp()
    assert _init_memory_vector(d, MemoryManager(d), None) is None


def test_memory_backend_hybrid_degrades_without_embedder(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "none")
    from src.app_initializer import _init_memory_vector
    from src.memory import MemoryManager
    d = tempfile.mkdtemp()
    # provider=none -> store unhealthy -> keyword-only (None), no crash
    assert _init_memory_vector(d, MemoryManager(d), None) is None


def test_memory_backend_chromadb_degrades_without_server(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "chromadb")
    from src.app_initializer import _init_memory_vector
    from src.memory import MemoryManager
    d = tempfile.mkdtemp()
    # No ChromaDB installed in the lite env -> degrades to keyword-only (None).
    assert _init_memory_vector(d, MemoryManager(d), None) is None


# ── SEARCH_PROVIDER default resolution ──────────────────────────────────────

@pytest.mark.parametrize("env_provider,expected", [
    ("duckduckgo", "duckduckgo"),
    ("brave", "brave"),
    ("", "searxng"),     # no env -> upstream default passes through
])
def test_search_provider_env_default(monkeypatch, env_provider, expected):
    from services.search import core as search_core
    monkeypatch.setattr(search_core, "_get_search_settings", lambda: {})
    if env_provider:
        monkeypatch.setenv("SEARCH_PROVIDER", env_provider)
    else:
        monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    assert search_core._effective_primary("searxng") == expected


# ── LITE_MODE gating (router subset) ────────────────────────────────────────

def test_lite_mode_flag_readable(monkeypatch):
    """LITE_MODE is the single gate the app reads at module load; assert the
    parse is correct for the obvious truthy/falsey spellings."""
    import os
    for val, expect in [("true", True), ("TRUE", True), ("false", False),
                        ("0", False), ("", False)]:
        monkeypatch.setenv("LITE_MODE", val)
        assert (os.getenv("LITE_MODE", "false").lower() == "true") is expect


def teardown_module(_m):
    import os
    os.environ["AGENT_SHELL_MODE"] = "sandboxed"
    from services.shell import sandbox
    sandbox.reset_config_cache()
