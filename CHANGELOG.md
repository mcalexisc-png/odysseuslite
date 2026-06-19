# Changelog

All notable changes to Odysseus Lite are documented here.

## [1.1-lite] — 2026-06-19: Opt-in Host File Access

Adds a documented, opt-in way to let the agent read (and optionally edit) files
on the host machine, built entirely on the existing workspace-jail + sandbox
model. **Off by default** — a fresh `docker compose -f docker-compose.lite.yml up`
mounts no host folder, keeps `AGENT_SHELL_MODE=sandboxed`, and leaves the
file-tool jail and sensitive-path denylist enforced.

### Added
- **Env-driven host mount.** `HOST_FS_PATH` / `HOST_FS_MODE` (`ro` default) in
  `.env` mount a chosen folder at `/host`. When `HOST_FS_PATH` is unset the
  compose volume maps the already-shared `data/` dir (inert no-op — no new
  access). `docker-compose.lite.yml` now also passes `AGENT_SHELL_ACK_UNSAFE`
  through, so the read-write tier is actually reachable.
- **Status endpoint** `GET /api/workspace/host-access` (admin-gated) reporting
  `none` / `read-only` / `read-write`, inferred from mount presence, writability,
  and the shell sandbox mode (`src/host_fs.py`).
- **Settings → File Access** panel: status-only display of the current mode with a
  plain-language trade-off explanation and a read-write warning banner. Cannot
  escalate to read-write from the UI (that requires the `.env` change + restart).
- **Workspace picker** gains a one-click “Shared folder (/host)” shortcut and a
  mode-aware confinement note when a folder is shared.
- **Startup warning** extended: a writable `/host` under `unrestricted` mode logs
  a loud banner that the agent can modify/delete real files.
- README “Giving the agent access to your files” (read-only quickstart, gated
  read-write tier, safety model) and `.env.example` documentation.

### Safety model (unchanged invariants)
- Read-only is the default opt-in; read-write requires a writable mount **and**
  `AGENT_SHELL_MODE=unrestricted` **and** `AGENT_SHELL_ACK_UNSAFE=true`.
- The sensitive-path denylist (`.ssh`, `.gnupg`, `id_rsa`, shell rc files) stays
  enforced inside any shared folder, in every mode.
- `vet_workspace('/')` still returns `None` — whole-disk access remains impossible.

### Fixed
- `host_access_status()` now uses `os.path.samefile` (dev+inode) rather than a
  realpath string compare to detect the inert default. Inside the container
  `/host` and `/app/data` are the same directory but have different realpath
  strings (each is its own bind mount), so the string compare wrongly reported
  the default state as `read-only`; it now correctly reports `none`.

## [1.0-lite] — 2026-06-18: True Lite upgrade

Upgrades Odysseus Lite from a *stripped-down* fork into a *true lite* version:
small footprint **and** still capable and safe at the low end (target: 4GB RAM,
any dual-core CPU, no GPU). Every capability that costs RAM/CPU/complexity is
**opt-in via an env flag** with graceful fallback. The default
`docker compose -f docker-compose.lite.yml up` stays one container, CPU-only, no
GPU, no new always-on background service.

### Baseline (Phase 0)
- **Vendored the implementation.** This checkout previously contained only
  `app.py`, `setup.py`, config, and docs — the entire app (`core/`, `src/`,
  `routes/`, `services/`, `static/`, `tests/`) was missing, so it could not boot.
  Vendored from upstream `pewdiepie-archdaemon/odysseus` `main` @ `d9ebdd6`. The
  lite `app.py` imports a strict subset of upstream's routers, so no import
  re-pointing was needed.
- **Fixed** `docker-compose.lite.yml` healthcheck (`/health` 404 → `/api/health`).
- Rewrote `LITE_NOTES.md` to reflect the real layout (the previously-claimed
  `memory_sqlite_fts.py` / `search_duckduckgo.py` modules never existed).

### Security — Agent shell sandbox (Phase 1)
- **Sandboxed by default.** The agent `bash`/`python` tools (the
  prompt-injection-reachable surface) now run under `AGENT_SHELL_MODE`
  (`sandboxed` default | `restricted` | `off` | `unrestricted`).
  - `sandboxed`: OS sandbox via bubblewrap → firejail → nsjail (read-only host,
    writable workspace only, fresh tmpfs `/tmp`, **no network** unless
    `AGENT_SHELL_NET=true`), with `resource.setrlimit` (CPU + address space) and a
    **120s** wall-clock cap (`AGENT_SHELL_TIMEOUT`) replacing the old 1-hour
    default. Degrades to a workspace jail + denylist if no launcher is installed.
  - `unrestricted` (legacy, no sandbox) requires `AGENT_SHELL_ACK_UNSAFE=true`
    and logs a loud startup warning.
- New: `services/shell/sandbox.py`. Container ships `bubblewrap`; runs non-root.
- Docs: `THREAT_MODEL.md` (gap "no shell sandbox" resolved), `SECURITY.md`.
- **Migration:** none. Defaults are safe; set `AGENT_SHELL_NET=true` if your
  agent shell needs network, or `AGENT_SHELL_MODE=unrestricted` +
  `AGENT_SHELL_ACK_UNSAFE=true` to restore the old behavior.

### Memory — Cheap hybrid semantic recall (Phase 2)
- `MEMORY_BACKEND` gains **`hybrid`**: dense vectors in **sqlite-vec** (a single
  SQLite extension, no server) + tiny **model2vec** static embeddings
  (`minishlab/potion-base-8M`, ~30MB, cached under `data/`). Fuses with the
  existing BM25 keyword recall. New: `services/memory_hybrid.py`.
- `EMBEDDING_PROVIDER` (`local` | `openai` | `none`) and `EMBEDDING_MODEL`.
- **Graceful fallback** to keyword-only `sqlite_fts` when the embedder can't load.
- **Migration:** existing memories are re-indexed into the vector store on first
  `hybrid` boot. `sqlite_fts` (default) and `chromadb` are unchanged.

### Models — Lite Cookbook + first-run bootstrap (Phase 3)
- New first-run, hardware-aware model recommender: `services/cookbook_lite.py`
  + a small curated `cookbook_lite_models.json`, tiered by RAM (2–4 / 4–8 / 8GB+)
  with concrete picks and honest tokens/sec. Routes under `/api/lite/cookbook`.
- Opt-in Ollama bootstrap (`LITE_AUTOPULL_MODEL=true`) — never downloads without
  explicit confirmation; suggests API mode when no local model fits.
- README: "what to expect on low-end hardware" table.

### Search — Resilient web search (Phase 4)
- `services/search/core.py` now honors `SEARCH_PROVIDER` / `SEARCH_FALLBACK_ORDER`
  **env** as the effective default on a fresh box (upstream read only admin
  settings, which default to `searxng` — which lite omits), logs which provider
  served, and under `LITE_MODE` returns a **non-silent error** instead of an
  empty "no results" list when the whole chain fails. (Provider chaining +
  TTL/LRU caching already existed upstream.)

### Resource budget (Phase 5)
- `AGENT_MAX_STEPS` caps the agent loop (default 50; lite compose ships **12**).
- `RESEARCH_MAX_STEPS` / `RESEARCH_CONCURRENCY` are now **enforced** as ceilings
  in `src/research_handler.py` (upstream defined them but never wired them).
- `docker-compose.lite.yml` sets `mem_limit` (1g) + `cpus` (2.0) that apply under
  `docker compose up`, raisable via `MEM_LIMIT` / `CPU_LIMIT`.

### Tests & CI (Phase 6)
- Lite overlay test suite (7 files) + a config-flag matrix + smoke test.
- `.github/workflows/ci-lite.yml`: lite requirements + bubblewrap, overlay tests,
  app boot + `/api/health`, and Docker image build + container boot.
- `Dockerfile` now installs **`requirements.lite.txt`** (not the heavy
  `requirements.txt`) and adds `bubblewrap`.

### Quality of life (Phase 7)
- `python setup.py reset-password [user] [--password ...]` — recover a lost admin
  password **without wiping the database** (`core/auth.py:admin_reset_password`).
  README makes this the primary recovery path.
- `CONTRIBUTING.md`: pre-commit `git status --short` / `git check-ignore` reminder.
- `.gitignore`: `.venv-lite/`, explicit `uploads/`; full exclusion audit passed.

### Lite-mode gating audit (R1–R5)
Two follow-up passes auditing which upstream features still leaked into the
lite build, and gating each one:
- **R1 — Dependencies.** Dropped `caldav` + `icalendar` from `requirements.lite.txt`;
  both packages were only imported by calendar routes/sync code already gated
  behind `if not LITE_MODE` and never loaded in the default lite build.
- **R2 — Gallery.** Gallery's CRUD/image-editor backend routes stay registered
  unconditionally (chat's AI-image cleanup and the agent's `app_api` tool share
  those paths), but the image-editor *endpoints* and the Gallery UI entry point
  are gated/hidden behind `LITE_MODE`.
- **R3 — Cookbook.** Upstream's heavy Cookbook (`routes/cookbook_routes.py`: GPU
  scan, VRAM column, 270-model table) and `routes/hwfit_routes.py` are no longer
  registered at all in lite mode — replaced by the Lite Cookbook modal
  (`/api/lite/cookbook/*`, Phase 3).
- **R4 — Calendar/Gallery front-end.** `calendar.js`/`gallery.js` are static ES6
  imports in `static/app.js` and can't be conditionally skipped without rewriting
  the module orchestrator; documented this constraint and hid the entry points
  (`#rail-calendar`, `#rail-gallery`, `#tool-gallery-btn`) via the lite-config gate
  script instead — the modules load but are never reachable.
- **R5 — Email.** `settings.js` now checks `_liteEmailHidden()` before every
  `/api/email/accounts` fetch, eliminating noisy 404s in lite mode where email
  routes are not registered.

### New environment variables
`AGENT_SHELL_MODE`, `AGENT_SHELL_NET`, `AGENT_SHELL_ACK_UNSAFE`,
`AGENT_SHELL_TIMEOUT`, `AGENT_SHELL_MAX_MEM_MB`, `AGENT_SHELL_CPU_SECONDS`,
`EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `LITE_AUTOPULL_MODEL`, `AGENT_MAX_STEPS`,
`SEARCH_FALLBACK_ORDER`, `MEM_LIMIT`, `CPU_LIMIT`, `MEM_RESERVATION`.
`MEMORY_BACKEND` gains `hybrid`. All documented in `.env.example`.

### Notes
- New deps (`sqlite-vec`, `model2vec`) are permissively licensed (AGPL-compatible)
  and inert unless `MEMORY_BACKEND=hybrid`.
- Upstream is AGPL-3.0-or-later, same as this fork — no relicensing happened.
  See `ACKNOWLEDGMENTS.md` for upstream credit and `LITE_NOTES.md` for the full
  divergence + upstream-sync table.
