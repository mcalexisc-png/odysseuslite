# Odysseus Lite — Roadmap

## Current Status: v1.1-lite

Odysseus Lite is a performance-focused fork of Odysseus for low-end PCs and laptops.

## ✅ Included in v1.0-lite
- Chat (all LLM providers: Ollama, OpenAI, OpenRouter, llama.cpp, vLLM)
- Agent + Shell Tool + MCP + Web + File tools
- Deep Research (capped at 5 steps, concurrency 1 by default)
- Compare (multi-model blind test)
- Documents (markdown/HTML/CSV editor with AI assist)
- Notes & Tasks (with reminders)
- Memory (SQLite FTS5 + BM25 — no ChromaDB needed)
- Web Search (DuckDuckGo — no SearXNG container needed)
- Auth + 2FA
- PWA / Mobile support
- Single Docker container deployment

## ✅ True-Lite upgrade (`feat/true-lite`)
Small footprint **and** safe/capable at the low end. See `CHANGELOG.md`.
- **Sandboxed agent shell by default** (`AGENT_SHELL_MODE=sandboxed`): no network,
  workspace-jailed, rlimits, 120s timeout; bubblewrap in the image; non-root container.
- **Hybrid semantic memory** (`MEMORY_BACKEND=hybrid`): sqlite-vec + model2vec static
  embeddings (~30MB, no GPU), graceful fallback to keyword-only.
- **Lite Cookbook**: first-run, hardware-aware model recommendation + opt-in Ollama
  autopull (`/api/lite/cookbook`).
- **Resilient search**: env-driven provider default + fallback chaining + non-silent errors.
- **Enforced resource budget**: `AGENT_MAX_STEPS`, enforced research caps, compose mem/cpu limits.
- **Tests + CI**: lite overlay suite + `.github/workflows/ci-lite.yml` (tests, app boot, Docker boot).
- **`reset-password` CLI**: recover a lost admin password without wiping the DB.

## ❌ Removed in Lite
- Email (IMAP/SMTP) — too complex, not core to AI workspace
- Calendar (CalDAV) — not essential for Lite
- Image Editor — heavy canvas UI
- Theme Editor — cosmetic only
- ChromaDB / Vector Memory — replaced by SQLite FTS5
- SearXNG container — replaced by DuckDuckGo
- ntfy container — replaced by browser notifications
- GPU overlays (NVIDIA/AMD) — CPU-first; use Ollama for model serving

## 🔮 Possible Future Additions
- Optional ChromaDB re-enable via env flag (already supported: CHROMADB_ENABLED=true)
- Optional SearXNG re-enable via env flag (already supported: SEARCH_PROVIDER=searxng)
- ~~Lighter Cookbook variant for CPU model discovery~~ ✅ shipped (Lite Cookbook)
- ARM/Raspberry Pi optimization
- Tighten the lite CI pytest gate from advisory to required once the full
  upstream suite is green under the lite Python target
