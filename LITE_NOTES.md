# Odysseus Lite — Developer Notes

This fork is a CPU-first, single-container build of upstream
[`pewdiepie-archdaemon/odysseus`](https://github.com/pewdiepie-archdaemon/odysseus),
targeting low-end machines (4GB RAM, dual-core, no GPU). Upstream is
AGPL-3.0-or-later (per upstream's `LICENSE` file and `README.md`); this fork
keeps the same license. Keep upstream attribution and license notices intact
in any file derived from upstream — see [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)
for the full upstream-license verification and a note on an inconsistency in
upstream's own `ACKNOWLEDGMENTS.md` prose.

## Lite contract (do not break)

The default `docker compose -f docker-compose.lite.yml up` must stay:
**one container, CPU-only, no GPU, no new always-on background service.** Every capability that
costs RAM/CPU/complexity is **opt-in via an env flag** with graceful fallback, matching the existing
pattern (`LITE_MODE`, `MEMORY_BACKEND`, `SEARCH_PROVIDER`, `CHROMADB_ENABLED`).

## How "lite" is actually achieved

The lite fork does **not** maintain forked copies of the memory/search engines. Instead it reuses
upstream's modules and selects the lightweight paths via flags + a trimmed dependency list:

- **Memory:** upstream `src/chat_processor.py` + `src/session_search.py` already implement BM25-style
  keyword scoring over SQLite **FTS5** (no separate `memory_sqlite_fts.py` module exists). With
  `CHROMADB_ENABLED=false` / `MEMORY_BACKEND=sqlite_fts`, the ChromaDB vector path degrades cleanly
  and recall stays keyword-only. **Phase 2** adds an opt-in `hybrid` tier: `services/memory_hybrid.py`
  provides a `sqlite-vec` + `model2vec` dense store implementing the same interface as
  `src/memory_vector.py`, so `chat_processor`'s existing BM25 + vector fusion lights up unchanged.
  `src/app_initializer.py` selects the backend (`_init_memory_vector`) and degrades to keyword-only
  on any failure.
- **Search:** upstream `services/search/{providers,core,cache}.py` already supports keyless
  **DuckDuckGo** plus keyed providers (Brave, Tavily, SearXNG, Serper, Google PSE), provider-chain
  fallback, and TTL/LRU caching. **Phase 4** closes the lite gaps: `services/search/core.py` now
  honors the `SEARCH_PROVIDER` / `SEARCH_FALLBACK_ORDER` **env vars** as the effective default on a
  fresh lite box (upstream read only admin settings, which default to `searxng` — which lite omits),
  logs *which* provider served (at warning when a fallback steps in), and surfaces a **non-silent
  error result** when the whole chain fails under `LITE_MODE=true` instead of an empty "no results"
  list. Admin settings still win over env; non-lite keeps the historical `[]` contract.
- **Image:** the heavy `requirements.txt` (chromadb, fastembed/onnxruntime, etc.) is replaced by the
  trimmed `requirements.lite.txt`, which is the **source of truth** for the lite image.

> Historical note: earlier versions of this file claimed standalone `services/memory_sqlite_fts.py`
> and `services/search_duckduckgo.py` modules. Those never existed — the functionality lives in the
> upstream modules above, gated by flags. This file now reflects the real layout.

## What was changed from upstream

### Trimmed dependency list (`requirements.lite.txt`)
- Drops `chromadb-client`, `fastembed`, `onnxruntime` — vector memory is opt-in only.
- Drops the SearXNG container — DuckDuckGo is the keyless default.
- TODO (tracked): `rank-bm25` is listed but not imported by upstream (BM25 is hand-rolled);
  `duckduckgo-search==6.2.13` is pinned but upstream imports the `ddgs` package. Reconcile in Phase 4.

### Lite-divergent root files (kept; NOT overwritten by upstream sync)
| File | Why it diverges |
|---|---|
| `app.py` | Imports a strict **subset** of upstream's routers (drops calendar/codex/contacts/editor-draft/email/workspace). Title "Odysseus Lite". |
| `docker-compose.lite.yml` | Single-container, no ChromaDB/SearXNG/ntfy; lite env defaults; mem limit. |
| `requirements.lite.txt` | Trimmed deps (see above). |
| `Dockerfile` | Installs `requirements.lite.txt` (NOT the heavy `requirements.txt`); adds `bubblewrap` for the agent-shell sandbox; non-root via `docker/entrypoint.sh` + gosu; pinned `python:3.12-slim-bookworm`. Apt deps install from Debian's live `bookworm` archive (no `snapshot.debian.org` date pin — a dated snapshot's `Release` file expires after ~7-9 days and would break builds for anyone cloning the repo later; the base image tag already pins the OS major version). |
| `setup.py` | Lite first-run flow. |
| `README.md`, `ROADMAP.md`, `THREAT_MODEL.md`, `SECURITY.md`, `ACKNOWLEDGMENTS.md`, `CONTRIBUTING.md` | Lite-specific docs. |
| `LICENSE` | AGPL-3.0-or-later (same as upstream — kept verbatim). |

Everything else (`core/`, `src/`, `routes/`, `services/`, `static/`, `tests/`, `docker/`, `.github/`,
`.env.example`, `.gitignore`, …) is **vendored from upstream** and should be kept in sync.

### Lite overlay modules (new files added on top of upstream — keep on sync)
| File | Purpose | Phase |
|---|---|---|
| `services/shell/sandbox.py` | Agent shell sandbox (modes, launchers, rlimits, denylist) | 1 |
| `services/memory_hybrid.py` | `sqlite-vec` + `model2vec` dense memory store for `MEMORY_BACKEND=hybrid` | 2 |
| `services/cookbook_lite.py` + `cookbook_lite_models.json` | first-run hardware-tiered model recommender + opt-in Ollama autopull | 3 |
| `routes/cookbook_lite_routes.py` | `/api/lite/cookbook/{recommend,ollama,autopull}` | 3 |
| `tests/test_agent_shell_sandbox.py`, `tests/test_memory_hybrid_lite.py`, `tests/test_cookbook_lite.py`, `tests/test_search_lite_fallback.py`, `tests/test_resource_budget_lite.py`, `tests/test_lite_smoke.py`, `tests/test_lite_config_matrix.py` | Lite overlay tests | 1–6 |
| `.github/workflows/ci-lite.yml` | Lite-scoped CI: overlay tests + app boot + Docker build/boot | 6 |

### Upstream files with small lite edits (thin, flag-gated — review on sync)
| File | Edit |
|---|---|
| `src/agent_tools/subprocess_tools.py` | route bash/python through `services.shell.sandbox`; cap timeout |
| `src/app_initializer.py` | `_init_memory_vector()` selects backend by `MEMORY_BACKEND`; graceful fallback |
| `services/search/core.py` | honor `SEARCH_PROVIDER`/`SEARCH_FALLBACK_ORDER` env; log serving provider; non-silent error under `LITE_MODE` |
| `src/agent_tools/__init__.py` | `MAX_AGENT_ROUNDS` now reads `AGENT_MAX_STEPS` env (default 50; lite compose ships 12) |
| `src/research_handler.py` | enforce `RESEARCH_MAX_STEPS`/`RESEARCH_CONCURRENCY` env as ceilings (upstream defined but never wired them) |
| `docker-compose.lite.yml` | `mem_limit`/`cpus` (apply under `compose up`, not just Swarm); ship `AGENT_MAX_STEPS`/`AGENT_SHELL_*` env |
| `app.py` | log the active shell sandbox mode at startup; mount `/api/lite/cookbook` router; gate `cookbook_routes` + `hwfit_routes` behind `if not LITE_MODE` |
| `core/auth.py` | add `admin_reset_password()` — operator recovery (no old password needed) |
| `setup.py` | add `reset-password` CLI subcommand |
| `static/index.html` | Lite-config gate script (inline `<script>`) hides email/calendar/gallery UI in lite mode; intercepts Cookbook rail to open Lite Cookbook modal |

### Why `calendar.js` and `gallery.js` are not conditionally skipped

`static/app.js` uses **static ES6 imports** for both modules (lines 24, 26):

```js
import galleryModule  from './js/gallery.js';   // line 24
import calendarModule from './js/calendar.js';  // line 26
```

ES6 static imports are resolved at parse time — they cannot be made conditional without
dynamic `import()` or rewriting the module orchestrator. Rewriting `app.js` carries high
breakage risk and touches many things that are not lite-specific. The correct lite approach
is **hiding the entry points** (`#rail-calendar`, `#rail-gallery`, `#tool-gallery-btn`), which
the gate script does. Both modules load silently but are never activated because the UI elements
that would call into them are hidden with `display:none; aria-hidden=true`.

### Baseline provenance
Implementation vendored from upstream `main` @ `d9ebdd6` (clone date 2026-06-16).

### Environment variables
| Variable | Default | Description |
|---|---|---|
| `LITE_MODE` | `true` (compose) | Disables Email, Calendar, Image Editor, Theme Editor, ChromaDB UI |
| `MEMORY_BACKEND` | `sqlite_fts` | `sqlite_fts` (keyword) · `hybrid` (keyword + sqlite-vec/model2vec, Phase 2) · `chromadb` (vector, heavy opt-in) |
| `EMBEDDING_PROVIDER` | `local` | `local` (model2vec) · `openai` · `none` (Phase 2; hybrid only) |
| `EMBEDDING_MODEL` | `minishlab/potion-base-8M` | static-embedding model for `hybrid` (~30MB, cached under `data/`) |
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`, `searxng`, or a keyed provider |
| `CHROMADB_ENABLED` | `false` | Set `true` to re-enable ChromaDB vector memory |
| `RESEARCH_MAX_STEPS` | `5` | Max steps for Deep Research runs |
| `RESEARCH_CONCURRENCY` | `1` | Max parallel fetches in Deep Research |

### Bug fixes applied to lite root files
- `docker-compose.lite.yml` healthcheck pointed at `/health` (404) → corrected to `/api/health`
  (the real liveness endpoint; readiness is `/api/ready`).
- `.gitignore`: added `.venv-lite/` and explicit `uploads/` (lite uses a top-level `./uploads` mount).

## Syncing with upstream Odysseus

When merging upstream changes:
1. Do **NOT** merge `docker-compose.yml` into `docker-compose.lite.yml`.
2. Do **NOT** merge `requirements.txt` into `requirements.lite.txt` directly — review and apply by hand.
3. Keep email/calendar/image-editor/theme-editor routes gated behind `LITE_MODE` in `app.py`.
4. The memory/search **engines are upstream code** — sync them normally; only the *flags and deps*
   are lite-divergent. (No `memory_sqlite_fts.py` / `search_duckduckgo.py` to protect.)
5. Re-verify the lite divergence table above after each sync; prefer flags/overlays over forking.

## Re-enabling heavier features
- ChromaDB vector memory: `CHROMADB_ENABLED=true` + `MEMORY_BACKEND=chromadb` + `pip install chromadb-client`.
- SearXNG: `SEARCH_PROVIDER=searxng` + `SEARXNG_INSTANCE=http://your-searxng:8080`.
- Full Odysseus: use the original `docker-compose.yml` + `requirements.txt`.
