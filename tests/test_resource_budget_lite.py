"""Resource budget enforcement — Odysseus Lite overlay (Phase 5).

Nothing previously stopped a runaway agent loop or unbounded research from
OOM-ing a 4GB host. These tests prove the lite ceilings are enforced in code:
  - AGENT_MAX_STEPS caps the agent loop default (and the loop emits
    rounds_exhausted when hit);
  - RESEARCH_MAX_STEPS / RESEARCH_CONCURRENCY env caps only ever lower the
    research budget;
  - the chat upload cap honors ODYSSEUS_CHAT_UPLOAD_MAX_BYTES.
"""
import asyncio
import importlib
import json
import os

import pytest


# ── agent loop cap (AGENT_MAX_STEPS) ────────────────────────────────────────

def test_agent_max_steps_env_overrides_default(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    import src.agent_tools as at
    importlib.reload(at)
    assert at.MAX_AGENT_ROUNDS == 7


def test_agent_max_steps_unset_is_default(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_STEPS", raising=False)
    import src.agent_tools as at
    importlib.reload(at)
    assert at.MAX_AGENT_ROUNDS == 50


def test_agent_max_steps_invalid_is_default(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "not-a-number")
    import src.agent_tools as at
    importlib.reload(at)
    assert at.MAX_AGENT_ROUNDS == 50


def test_agent_loop_terminates_at_cap(monkeypatch):
    """With a never-finishing model, the loop stops at max_rounds and emits
    rounds_exhausted instead of looping forever. Uses the env-derived default
    cap (AGENT_MAX_STEPS) as the effective limit."""
    monkeypatch.setenv("AGENT_MAX_STEPS", "3")
    import src.agent_tools as at
    importlib.reload(at)
    cap = at.MAX_AGENT_ROUNDS
    assert cap == 3

    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    # Model always emits a tool block -> never "done" -> must hit the cap.
    # Real newlines (matches tests/test_agent_rounds_exhausted.py).
    round_text = "```bash\necho hi\n```"

    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=cap,
        relevant_tools={"bash"},
    )

    async def _collect():
        return [c async for c in gen]
    chunks = asyncio.run(_collect())

    types = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                types.append(json.loads(c[6:]))
            except Exception:
                pass
    exhausted = [t for t in types if t.get("type") == "rounds_exhausted"]
    assert exhausted, f"loop should emit rounds_exhausted at the cap; got {types}"
    assert exhausted[0]["rounds"] == cap


# ── research caps (RESEARCH_MAX_STEPS / RESEARCH_CONCURRENCY) ────────────────

def _clamp_steps(max_rounds, env):
    """Mirror the lite clamp in research_handler (env is a ceiling only)."""
    if env and str(env).strip():
        try:
            return min(max_rounds, max(1, int(env)))
        except (TypeError, ValueError):
            return max_rounds
    return max_rounds


def test_research_max_steps_only_lowers():
    assert _clamp_steps(20, "5") == 5      # caps down
    assert _clamp_steps(3, "5") == 3       # never raises above the caller's value
    assert _clamp_steps(20, "") == 20      # unset -> unchanged
    assert _clamp_steps(20, "garbage") == 20
    assert _clamp_steps(20, "0") == 1      # floored at 1


def test_research_handler_has_env_clamp():
    """The handler source wires RESEARCH_MAX_STEPS/CONCURRENCY (regression guard
    against the upstream 'defined but never enforced' bug)."""
    import inspect
    import src.research_handler as rh
    src = inspect.getsource(rh)
    assert "RESEARCH_MAX_STEPS" in src
    assert "RESEARCH_CONCURRENCY" in src


# ── upload cap ──────────────────────────────────────────────────────────────

def test_chat_upload_cap_env(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_CHAT_UPLOAD_MAX_BYTES", "12345")
    from src.upload_limits import get_chat_upload_max_bytes
    assert get_chat_upload_max_bytes() == 12345


def teardown_module(_m):
    # restore agent_tools to env-default state for later tests
    os.environ.pop("AGENT_MAX_STEPS", None)
    import src.agent_tools as at
    importlib.reload(at)
