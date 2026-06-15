# HANDOFF — `website_url_finder`

Last updated: 2026-06-13. Read this first if you're picking up this repo.

---

## 2026-06-12 — REWORK COMPLETE (read this first)

The whole backend was restructured on branch `rework` (~38 commits over `main`). Everything below
this section predates the rework — file paths like `app.py`, `ai_mode_service.py`,
`scrapedo_finder/`, `templates/ui.html` no longer exist at those locations. Use this section as
the source of truth; stale sections below are tagged "(superseded — see top)".

### ⚡ Latest session additions (post-rework, same day) — STATE A NEW AGENT MUST KNOW

**Conventions / hard rules**
- **NEVER add `Co-Authored-By: Claude` (or any AI attribution) to commits** — user requirement,
  also saved in agent memory. All 28 rework commits were history-rewritten
  (`git filter-branch --msg-filter`) to strip the trailer, then force-pushed
  (`--force-with-lease`, user-authorized). Tell every committing subagent this rule.
- **Never `git push` without the user explicitly asking.** Never query the production Supabase
  DB (psql etc.) without the user explicitly approving that exact action.

**Branch state**: `rework` is pushed and in sync with `origin/rework` (38 commits over `main`).
NOT merged into `main` yet — merge is the user's call. Suite: **142/142** (`cd backend &&
../.venv/bin/python -m unittest discover -s tests`). Working tree clean.

**⚠️ Supabase pending — RE-CONFIRM WITH THE USER** (they've since committed Supabase
error-handling work in `48114d0`, so the `.env`/migration status below may have changed since
last check):
1. The migration (`supabase/migrations/001_init.sql`) was **NOT applied** as of last check — run it
   in the Supabase dashboard SQL Editor. It now includes `enable row level security` on both
   tables (no policies — service_role bypasses RLS; anon gets nothing; the dashboard RLS warning
   is satisfied).
2. The user's `.env` has TWO broken values (do NOT edit `.env` yourself; tell the user):
   `SUPABASE_URL` still holds the `...supabase.co:5432/postgres` connection string (must be the
   bare `https://<ref>.supabase.co`), and `SUPABASE_SERVICE_ROLE_KEY` is **truncated** (37 chars;
   real key ~200+). The project itself is alive (bare URL answers 401 to unauthenticated REST).
   Until fixed: company/tracking endpoints return 503 (after ~10–40s timeout), uploads with
   company validation fail — pipelines themselves are unaffected on disk.

**Post-plan fixes landed** (commits after the 24-task plan finished):
- Unified-CSV validation gate added to the SerpWow upload endpoints (`/uploads`, `/uploads/gmaps`,
  `/uploads/gsearch`, `/uploads/url-discovery`) — old Company-Mode CSVs now 400 there too
  (firmographics keeps its own website-column parser). `_validate_canonical_upload_csv` in
  `legacy_app.py`.
- SerpWow runs now sync `status="running"` to Supabase when processing starts (one-shot
  `supabase_running_marker` in upload state), not just at terminal status.
- `run.py` added at repo root → `python run.py` works like the old `python app.py`
  (reads API_PORT/API_HOST from `.env`; guarded for uvicorn reload re-import).
- `readme.md` rewritten — the §Run section is now an explicit **two-terminal local guide**:
  Terminal 1 = app via `.venv/bin/uvicorn run:app --reload --host 0.0.0.0 --port 8080` (`run.py`
  exposes `app`); Terminal 2 = SerpWow worker (`docker compose up -d rabbitmq` then
  `.venv/bin/python worker.py`), only needed for SerpWow pipelines.
- `.env.example` fully audited — **19 dead keys deleted** (all `MASSIVE_*`, `BROWSER_POOL_SIZE`,
  `USER_AGENT`, dead `SEARCH_FETCH_*` timeouts, etc.), restructured into 5 sections; AI Mode
  section grouped into 3a batch sizes / 3b cleanup LLM / 3c sync-vs-Gemini-Batch / 3d scraping /
  3e cost+logging, with an explicit note that `AI_MODE_LLM_BATCH` (3c) is unrelated to SerpWow's
  `ENABLE_GEMINI_BATCH_POSTPROCESS` (section 4). (User reviewed the deleted keys and chose to keep
  them deleted — none are read by code, current or `oldCode`; the real values still live in their
  untouched `.env`.)

**Changes since the last handoff write (re-read these areas — some are the user's own commits):**
- **scrape.do cost removed** (`b2af5af`): scrape.do is a flat fee, so runs no longer compute a
  scrape.do USD figure. Cost summary is now `{llm_usd, scrapedo_searches, total_usd=llm_usd}`;
  per-company `total_searches` is aggregated in `company_stats` and shown on the dashboard
  ("Searches") + run detail ("Searches / failed"). `SCRAPEDO_COST_PER_REQUEST_USD` env removed,
  and `scrapedo_client.search_google_ai_mode` now returns just the payload (no cost tuple).
  Nuance: a "search" = one scrape.do request (covers a batch of ~10 entities in `ai_bulk`), NOT
  literally one-per-company — the user may later want per-entity counting instead.
- **RabbitMQ env + worker** (`72603b5`): blank `RABBITMQ_*` env values now fall back to defaults,
  and `backend/app/services/serpwow/worker.py` surfaces the real broker error.
- **User's own commits** (`48114d0` "Enhance Supabase configuration error handling and improve UI
  theme", `d1c2076` "Refactor UI components and styles") — made outside the agent flow (capitalized
  commit style). Diff these before trusting earlier UI/Supabase descriptions in this doc.

**User's working setup & intent** (context for decisions):
- Workflow: run cheap SerpWow pipelines first (~80% coverage), then feed the residue CSV into
  AI Mode (`ai_bulk` broad, `ai_deep` thorough). Batch sizes tuned via `AI_BULK_BATCH_SIZE` /
  `AI_DEEP_BATCH_SIZE`.
- LLM cleanup: sometimes uses the **OpenCode Zen** OpenAI-compatible gateway —
  `AI_MODE_LLM_PROVIDER=openai`, `OPENAI_MODEL=deepseek-v4-flash-free`,
  `OPENAI_BASE_URL=https://opencode.ai/zen/v1`, with `AI_MODE_LLM_BATCH=false` (sync; the batch
  path is Gemini-only). This works as-is (client calls `{base_url}/chat/completions`); the
  example is documented in `.env.example` §3b. User will refine the search-prompt wording in
  `backend/app/prompts/*.txt` themselves.

**Known UI gap / likely follow-up:** the new **Runs page lists history from Supabase only**
(`GET /companies/runs`). The 3 pre-rework AI-mode runs live on disk in `ai_mode_result/`
(singular, OLD layout) and are still reachable via the API — `GET /uploads/ai-mode` plus
`/uploads/ai-mode/<id>/status` and `/result?file=...` — but **no new-UI page lists disk-based AI
runs** the way the old AI Mode tab did. They were never in Supabase (predate tracking). Follow-up
if wanted: surface `GET /uploads/ai-mode` in the UI (e.g. a "Local / untracked AI runs" section,
or merge disk + Supabase runs on the Runs page). New per-company runs go to `ai_mode_results/`
(plural) and ARE tracked once Supabase is live.

**Known minor leftovers** (from the final whole-branch review — all low/cosmetic, none blocking):
rerun re-scrapes notFound-without-error rows (plan-approved but costs tokens); duplicate `sno`s
possible in merged rerun reports; runs view lacks a date filter; `GET /batch/jobs` makes a live
Gemini call when `GEMINI_API_KEY` is set; `batch_runner.py` still at repo root.

**Plan/spec docs**: the rework spec is `docs/superpowers/specs/2026-06-11-rework-design.md`; the
executed 24-task plan is `impplan.md` (all tasks done, each implementer-reviewed twice).

### Repo layout (new)
```
backend/app/
  core/        # config.py (.env + paths), supabase_client.py
  routers/     # ai_mode.py (preview/upload/rerun/status/result), companies.py
  services/
    ai_mode/   # ai_mode_service.py (engine), mode_config.py, rerun.py, run_store.py,
               #   run_reporting.py, cost.py, cleanup.py, gemini_batch.py,
               #   scrapedo_client.py, llm_client.py, settings.py, models.py
    serpwow/   # legacy_app.py — the old ~7.6k-line monolith (still owns the FastAPI instance)
  models/      # entities.py (canonical CSV parser), results.py (EntityResult/Flag/AttemptLog)
  prompts/     # ai_bulk_search.txt, ai_deep_search.txt, ai_cleanup.txt
  static/      # css/ + js/ for the new UI
  templates/   # index.html (the new console; legacy ui.html DELETED)
backend/tests/       # full suite (unittest, offline)
backend/scripts/     # batch-recovery scripts
supabase/migrations/001_init.sql   # companies + runs tables
samples/             # test CSVs (sample_address.csv, smalltest.csv, ...)
ai_mode_results/     # run outputs (gitignored)
```

### Run it
- **Server:** `cd backend && ../.venv/bin/python -m app.main` → `http://localhost:8080/app`
  (port from `.env` `API_PORT=8080`; dev knobs `API_RELOAD`, `UVICORN_LOG_LEVEL`).
- **Tests:** `cd backend && ../.venv/bin/python -m unittest discover -s tests` — all offline,
  no live API calls.

### Unified CSV input (ALL pipelines, `backend/app/models/entities.py`)
- **Required:** a company-name column (`company_name|company|name|entity_name|entity|organization|
  organisation|legal_name`) + a country column (`country|country_name|nation`).
- **Optional:** `company_local_name`, `address|full_address|input_full_address`, `firm_id|id`,
  `industry|input_industry`. Headerless 2+-column files parse positionally (col 1 = company,
  col 2 = country).
- **The old Company-Mode format (`Company Name ENG`, `Country Code`, `ISIC`) is REJECTED with a
  400** and a message listing accepted headers — there is no auto-detect split anymore.

### Two AI modes, one engine (`mode_config.py` + `ai_mode_service.py`)
- `ai_bulk` ("AI Mode 1 — Bulk"): `AI_BULK_BATCH_SIZE` (10); legacy `SCRAPEDO_BATCH_SIZE` still
  read as fallback. Prompt: `prompts/ai_bulk_search.txt`.
- `ai_deep` ("AI Mode 2 — Deep Search"): `AI_DEEP_BATCH_SIZE` (3). Prompt: `prompts/ai_deep_search.txt`.
- Both share one scrape→clean engine and ONE unified output schema per entity:
  `website_url, confidence (0-100), flags[{flag,why}], attempt_log[{query,result,url}], error`
  (the old address/company prompt split + schema mismatch is gone).

### Output layout (per run)
`ai_mode_results/<company-slug>/<run_id>/` — `input.csv`, `status.json`, **one** `final_report.json`
(summary + per-entity results + per-request records + cost), `found.csv`, `notFound.csv`, **one**
`run.log` (level via `AI_MODE_LOG_LEVEL`), `raw_responses/request_NNN.json`, `carryover.json`
(reruns only). The old `report.json` and `ai_mode_debug.log` are gone.

### Supabase company tracking (all pipelines)
- Apply `supabase/migrations/001_init.sql` (creates `companies` + `runs`; run rows carry pipeline,
  status, counts, token_usage, cost, file_links, `rerun_of`).
- Env: `SUPABASE_URL` (**bare project URL** `https://xyz.supabase.co` — NOT the `:5432` postgres
  string) + `SUPABASE_SERVICE_ROLE_KEY`.
- Unconfigured/unreachable → upload & company endpoints return **503** with a clear message.
- **Never-fails-runs design:** once a run is accepted, Supabase write failures are logged and
  swallowed — pipelines always finish on disk.

### Cost tracking (updated — scrape.do is now a search count, not USD)
`final_report.json.summary.cost` = `{llm_usd, scrapedo_searches, total_usd}` where `total_usd` is
**LLM only** (env token rates, batch rates when `AI_MODE_LLM_BATCH`) — scrape.do is a flat fee, so
there is no scrape.do dollar figure; `scrapedo_searches` just counts scrape.do requests. Per company,
`company_stats` aggregates `total_searches`; the dashboard shows "Searches" and run detail shows
"Searches / failed". (`SCRAPEDO_COST_PER_REQUEST_USD` and the response-header credit reading were
removed.)

### Endpoints (new/changed)
- `POST /uploads/preview` — parse a CSV without running; returns detected columns/warnings/rows
  (400 + helpful message on bad format).
- `POST /uploads/ai-mode` — multipart `file` + `mode` (`ai_bulk|ai_deep`) + company fields;
  503 if Supabase unconfigured.
- `POST /uploads/ai-mode/{run_id}/rerun` — re-runs only failed/unscraped rows of a
  `failed`/`completed_with_errors` run; previous successes merge into the new run via
  `carryover.json` (flagged `carried_over`, deduped across chained reruns).
- `GET /uploads/ai-mode`, `/{run_id}/status`, `/{run_id}/result?file=...` — as before (allowlist
  updated to the new file set).
- `GET/POST /companies`, `GET /companies/stats`, `GET /companies/runs` — Supabase-backed views.

### UI
New console at **`/app`** (FastAPI-served `templates/index.html` + `static/js/*` views: dashboard,
companies, new-run with CSV preview, runs history, run detail, operations). **`/ui` → 307 redirect
to `/app`; the legacy `templates/ui.html` was deleted.**

### Deleted in the rework
8 dead scrapedo CLI modules (`cli.py`, `runner.py`, `cleanup_*`, `csv_loader.py`, `reporting.py`,
`__main__.py`, ...) + 7 mode-split modules (`extraction.py`, `company_extraction.py`,
`prompting.py`, `company_prompting.py`, `cleanup_reporting.py`, `company_reporting.py`,
`company_csv_loader.py`), the old prompt templates (`search_query_template.txt`,
`company_search_template.txt`), per-run `report.json` + `ai_mode_debug.log`, and `templates/ui.html`.

### Stale-section index
- §1/§2 (two-phase build + `plancl.md`): the two-phase engine survived the rework, but
  **`plancl.md` is superseded** — the rework plan replaced it.
- §3 repo map, §4 AI Mode reference, §0/§7 run instructions: paths/format/endpoints superseded
  by this section.
- §6a address-prompt mismatch: **FIXED** — prompts unified with confidence+flags.
- §6d scrapeDo file deletion: **DONE** (see "Deleted" above).
- §6e SerpWow caveats: still true (legacy service untouched apart from the move to
  `backend/app/services/serpwow/legacy_app.py` + recovery-script fixes + company tracking).

This project contains **two independent website-discovery systems** sharing one FastAPI app + one UI:

1. **SerpWow pipelines** (the original) — RabbitMQ + worker + S3, 5 modes (`full`, `url_discovery`,
   `firmographics`, `gmaps`, `gsearch`). Documented in `docs/` (start at [`docs/README.md`](docs/README.md)).
2. **AI Mode** — a standalone scrape.do "Google AI Mode" + LLM-cleanup pipeline, vendored from the
   sibling `scrapeDo` project. Documented in [`docs/13.ai-mode.md`](docs/13.ai-mode.md). **Most recent
   work has been here** (§1).

Python 3.12 recommended (the code runs on 3.9+ but 3.9 is EOL). There is now a **`requirements.txt`**
and a **`.venv/`** — see §0.

---

## 0. Environment & how to run (superseded — see top; `app.py` no longer exists)

- **venv:** `.venv/` was created with Homebrew Python 3.12 (`/opt/homebrew/bin/python3.12`).
  Run: `cd website_url_finder && source .venv/bin/activate && python app.py`
  (or just `.venv/bin/python app.py`).
- **`requirements.txt`** (new): 8 pinned runtime deps — `fastapi`, `uvicorn`, `httpx`, `pydantic`,
  `aio-pika`, `boto3`, `python-dotenv`, `python-multipart`. **NOT needed:** `pandas` (only the
  standalone `convertjtox.py` uses it) and `openpyxl` (XLSX is hand-built with `zipfile`/`xml`).
  `aio-pika` + `boto3` ARE required even for AI-Mode-only use because `app.py` imports them at module top.
- **Python version:** ideal **3.12** (matches the modern type-hint syntax; all deps ship `cp312`
  wheels). Hard floor is 3.9 (PEP 585 generics), but don't use it — EOL.
- **UI:** open `http://localhost:<API_PORT>/ui`. `.env` sets **`API_PORT=8080`** (the code default in
  `app.py.__main__` is still `11500`). RabbitMQ is **not** required — startup wraps `init_rabbitmq()`
  in try/except, and AI Mode runs fully in-process.
- **New `__main__` env knobs** (app.py): `API_RELOAD` (uvicorn auto-reload on code change — set this
  instead of manually restarting), `UVICORN_LOG_LEVEL`.
- **Common gotcha:** open the UI from the app's own port, **not** VS Code Live Server (`:5500`). The
  AI Mode upload posts to a relative path; a static server returns **405** on the POST.
- **Sample CSVs** for quick tests: `sample_company.csv` (company mode, header `Company Name ENG`) and
  `sample_address.csv` (address mode) — 3 rows each = 1 batch.
- **Offline checks:** `.venv/bin/python -c "import app"` (needs the venv deps);
  `python3 -c "import ast; ast.parse(open('app.py').read())"` to syntax-check without deps.

---

## 1. What changed in the latest session (2026-06-10)

**AI Mode is now a TWO-PHASE run with an `AI_MODE_LLM_BATCH` toggle — `plancl.md` is now BUILT (§2).**
The old interleaved scrape→clean loop in `ai_mode_service.run_ai_mode_sync` is gone, replaced by:

1. **Phase 1 — scrape ALL batches first, in parallel** (`ThreadPoolExecutor`, `SCRAPEDO_CONCURRENCY`,
   default 5; raise toward scrape.do's ~100 cap). **Resume:** a batch whose `request_NNN.json` already
   exists & parses is reused, not re-scraped.
2. **Phase 2 — clean ALL scraped batches**, engine chosen by `AI_MODE_LLM_BATCH`:
   - **false (normal)** → today's synchronous per-batch LLM (`AI_MODE_LLM_PROVIDER` = gemini *or*
     openai); same behavior, just run as a second phase.
   - **true (batch)** → **Gemini Batch API only**. One request per scraped batch, **global key
     `batch-NNNNNN`**, sharded by `GEMINI_BATCH_SHARD_SIZE` (5000) into File-API JSONL jobs, ≤
     `GEMINI_BATCH_MAX_INFLIGHT` (5) jobs in flight, poll each to terminal, results mapped **by key**.
     Missing/failed keys → entities land in notFound (`llm_errors`). Per-batch `llm_seconds` is 0 in
     batch mode; `llm_seconds_total` = whole-cleanup duration.
3. **Phase 3 — assemble** (the existing final-report block, unchanged): `found.csv` / `notFound.csv` /
   `final_report.json` / `report.json` / `run.log`; status `completed` | `completed_with_errors`.

- **NEW `gemini_batch.py`** (repo root, self-contained, stdlib `urllib`, **no `app.py` import**):
  `messages_to_gemini_request` (mirrors `GeminiClient.complete_json`), `create_batch` (inline if
  <18 MB else File API), `upload_jsonl_file` (resumable), `get_batch`/`state_name`/`is_terminal`/
  `is_success`, `collect_results` (inline OR downloaded result file, keyed), `parse_json_from_text`,
  `calculate_gemini_batch_cost_usd`. HTTP isolated in `_http_post_json`/`_http_get_json`/`_http_get_bytes`.
- **`build_ai_mode_llm_config`** forces `provider="gemini"` + `GEMINI_BATCH_MODEL`/`GEMINI_MODEL` when
  `AI_MODE_LLM_BATCH=true` (so `GEMINI_API_KEY` is validated at upload, fail-fast).
- **Same AI Mode tab, same 4 endpoints, same output files — NO `app.py` / `templates/ui.html` changes.**
- **New env** (in `.env.example`): `AI_MODE_LLM_BATCH=false`, `SCRAPEDO_CONCURRENCY=5`,
  `GEMINI_BATCH_SHARD_SIZE=5000`, `GEMINI_BATCH_MAX_INFLIGHT=5`, `AI_MODE_BATCH_POLL_SEC=15`,
  `AI_MODE_BATCH_TIMEOUT_SEC=172800` (48 h — **deliberately separate** from SerpWow's
  `GEMINI_BATCH_TIMEOUT_SEC=1800`, which is too short for multi-hour batch jobs). Dead `REDIS_*` removed.
- **NEW `tests/test_gemini_batch.py`** (12 offline tests). Verified this session: `import app` ✓,
  full suite **17/17** ✓, end-to-end smoke test (scrape.do + Gemini mocked) in **both** modes ✓.
- **Architecture decision — NO RabbitMQ / Redis for AI Mode.** Its throughput is gated by scrape.do's
  ~100-concurrent **vendor cap**, which a single process saturates → a queue's horizontal-scale benefit
  doesn't apply (1M rows ≈ ~39 h with or without a queue). The on-disk run dir gives the
  durability/retry a queue would. **SerpWow keeps RabbitMQ, untouched.** Reconsider only if AI Mode ever
  pushes past one scrape.do account's cap (multiple accounts/vendors → then queue + Redis).

### Earlier session (2026-06-08 → 09) — still-relevant context
1. **References dropped from the LLM prompt** (`extraction.py`/`company_extraction.py`): builders send
   only flattened `text_blocks` via `text_blocks_to_text`, not `references` (~40% smaller input).
2. **Per-batch geo-targeting** (`_geo_params_for_group` → scrape.do `extra_params`; recorded as
   `scrapedo_params`).
3. **scrape.do client hardening** (`scrapedo_client.py`: log callback, `extra_params`, per-attempt
   logging, `>=400`→RuntimeError, `estimated_url_chars()`).
4. **Per-run debug log + secret redaction** (`ai_mode_debug.log`, `AI_MODE_LOG_LEVEL`;
   `sanitize_secret_text`/`sanitize_for_response`).
5. **Failure tracking + `completed_with_errors`** (also wired into SerpWow upload paths in `app.py`).
6. **Address research prompt rewritten** to an OSINT agent prompt — ⚠️ mismatch with the address
   cleanup prompt/schema, see §6a.
7. **SerpWow per-flow timing** (`build_processing_timing_summary`; `tests/test_timing_summary.py`).
8. **Tooling:** venv + `requirements.txt` + Python 3.12; sample CSVs. Earlier sessions built AI Mode v1
   and the full `docs/` set (§4/§5).

---

## 2. `plancl.md` — the design, now BUILT (superseded — `plancl.md` itself is superseded by the rework; see top)

`plancl.md` (repo root) holds the implemented design. **Note:** it was rewritten this session — the
original "separate **LLM Cleaner tab** + `scrape_manifest.json`" two-stage idea was **dropped** in favor
of the simpler **in-AI-Mode two-phase** flow now shipped (scrape-all → clean-all in one run, one tab,
no new endpoints, no separate manifest file — the per-run dir + `report.json` are the durable record).
Read `plancl.md` for the file-by-file record; §1 here is the summary.

**Not built / deferred** (kept in `plancl.md`'s "future" notes): `(name,country)` **dedup/cache** (the
biggest cost lever at millions of rows — skip re-scraping repeated entities), and a **queue/worker
split** (only needed if you ever exceed one scrape.do account's ~100-concurrent cap).

---

## 3. Repo map (superseded — see the new layout at top)

```
app.py                  # the whole SerpWow service (FastAPI + worker + Gemini batch + XLSX), ~7.6k lines
                        #   + 4 AI-mode endpoints near the end (POST/GET /uploads/ai-mode*); result
                        #   endpoint now sanitizes JSON; __main__ supports API_RELOAD/UVICORN_LOG_LEVEL
ai_mode_service.py      # AI Mode orchestrator — TWO-PHASE (parallel scrape-all → clean-all: sync or Gemini batch)
gemini_batch.py         # NEW — self-contained Gemini Batch API helper (File API + inline); used by AI Mode batch mode
scrapedo_finder/        # vendored scrape.do package (clients in scrapedo_client.py / llm_client.py)
worker.py               # SerpWow worker entrypoint (NOT needed for AI Mode)
gmaps.py, codetails.py  # SerpWow Google-Maps + firmographics clients
templates/ui.html       # single-page UI; AI Mode is the 7th tab (handles completed_with_errors, debug log)
tests/                  # test_timing_summary.py (SerpWow) + test_gemini_batch.py (NEW — Gemini batch helper, offline)
docs/                   # numbered SerpWow documentation set (01..13)
ai_mode_result/         # per-run output (gitignored): input.csv, status.json, report.json,
                        #   final_report.json, found.csv, notFound.csv, run.log, ai_mode_debug.log,
                        #   raw_scrapedo_response/request_NNN.json
requirements.txt        # NEW — pinned runtime deps (§0)
.venv/                  # NEW — Python 3.12 virtualenv
plancl.md               # design for the AI Mode two-phase / Gemini-batch rework — BUILT this session (§2)
sample_company.csv,
sample_address.csv      # 3-row test inputs (company / address mode)
.env.example            # all env vars (now includes the AI Mode batch keys; dead REDIS_* removed — §1)
```

---

## 4. AI Mode reference (superseded — input format, endpoints and output files changed; see top)

**Goal:** upload CSV → for each batch of `SCRAPEDO_BATCH_SIZE` (10) entities, send ONE prompt to
scrape.do Google AI Mode (geo-targeted), then clean the response with an LLM (Gemini *or* OpenAI) →
save raw responses + cleaned results + timings under `ai_mode_result/<run_id>/`. Standalone:
**no RabbitMQ/S3/state.json**.

**Flow (now two-phase):** `POST /uploads/ai-mode` → `prepare_ai_mode_run` (validate CSV, detect mode,
register run) → API schedules `asyncio.to_thread(run_ai_mode_sync, run_id)` → orchestrator runs
**Phase 1** = scrape ALL batches in parallel (`SCRAPEDO_CONCURRENCY`, geo-targeted, resume-aware) →
**Phase 2** = clean ALL scraped batches (sync per-batch, OR one+ Gemini Batch jobs when
`AI_MODE_LLM_BATCH=true`) → **Phase 3** = assemble outputs. `status["phase"]` = `scraping`|`cleaning`.
UI polls `/uploads/ai-mode/<run_id>/status` every 2 s. (See §1 for the full breakdown.)

**Input auto-detect:** header `Company Name ENG` → **company** mode (`load_company_entities`);
otherwise **address** mode (flexible mapping: `entity_name|company_name|company|name|legal_name|entity`
+ `country*` + `address|input_full_address|full_address`). So existing SerpWow CSVs work as address mode.

**LLM provider (env-switchable):** `AI_MODE_LLM_PROVIDER=gemini|openai` (default `gemini`). Gemini reuses
`GEMINI_API_KEY` + `GEMINI_MODEL`; OpenAI uses `OPENAI_API_KEY` + `OPENAI_MODEL` (+ optional
`OPENAI_BASE_URL`, e.g. an OpenAI-compatible gateway). Switching is inside `llm_client.make_llm_client`;
the glue maps env → `LLMConfig`. LLM config is validated at **upload** time (missing key → HTTP 400);
`SCRAPEDO_TOKEN` is validated at **run** time (missing → run `status=failed`).

**4 endpoints** (`app.py`, before `if __name__=="__main__"`), module-level `ai_mode_tasks` set keeps tasks alive:
- `POST /uploads/ai-mode` (multipart `file`) → run info dict
- `GET  /uploads/ai-mode` → `{count, runs:[...]}`
- `GET  /uploads/ai-mode/{run_id}/status` → status dict
- `GET  /uploads/ai-mode/{run_id}/result?file=<name>&download=true` → file. **Allowlist:**
  `final_report.json, report.json, found.csv, notFound.csv, run.log, ai_mode_debug.log, input.csv`.
  JSON files are passed through `sanitize_for_response` (token redaction).

**`ai_mode_service.py` public API:** `prepare_ai_mode_run(raw_csv, filename)`, `run_ai_mode_sync(run_id)`,
`list_ai_mode_runs()`, `get_ai_mode_status(run_id)`, `get_ai_mode_result_path(run_id, file_name)`.
Helpers worth knowing: `_geo_params_for_group`, `sanitize_for_response`, `_reconcile_status_from_report`,
`build_ai_mode_settings`, `build_ai_mode_llm_config`, `load_address_entities`.

**Status fields:** `status` (`queued|running|completed|completed_with_errors|failed`), `input_type`,
`total_rows`, `batch_size`, `llm_provider/model`, `batches_total/done`, `entities_processed`,
`entities_without_scrape_data`, `llm_errors`, `websites_found/not_found`,
`failed_request_count`/`scrapedo_request_count`/`scrapedo_failed_requests`,
`scrapedo_seconds_total`, `llm_seconds_total`, `batch_duration_seconds`, `token_usage`,
plus (this session) **`phase`** (`scraping`|`cleaning`) and **`gemini_batch_jobs`** (list of batch job
names, batch mode only — persisted before polling for recovery).
`report.json` has per-request `{scrapedo_seconds, llm_seconds, combined_seconds, raw_json_file,
scrapedo_params, status, error}` (in batch mode per-request `llm_seconds`=0).

**Env (AI Mode):** `SCRAPEDO_TOKEN` (required), `SCRAPEDO_BATCH_SIZE` (10), `SCRAPEDO_*` timeouts/locale,
`SCRAPEDO_GL/HL/GOOGLE_DOMAIN` (geo fallbacks), `AI_MODE_LLM_PROVIDER`, Gemini/OpenAI keys+models,
`LLM_MAX_RETRIES`, `LLM_TIMEOUT_SECONDS`, `AI_MODE_LOG_LEVEL`.
**Two-phase / batch env (this session):** `AI_MODE_LLM_BATCH` (toggle; true ⇒ Gemini batch, forces
Gemini provider), `SCRAPEDO_CONCURRENCY` (parallel scrape, 5), `GEMINI_BATCH_SHARD_SIZE` (5000),
`GEMINI_BATCH_MAX_INFLIGHT` (5), `AI_MODE_BATCH_POLL_SEC` (15), `AI_MODE_BATCH_TIMEOUT_SEC` (172800 =
48 h; **separate** from SerpWow's `GEMINI_BATCH_TIMEOUT_SEC`), `GEMINI_BATCH_MODEL` (optional → `GEMINI_MODEL`).

---

## 5. SerpWow documentation index (`docs/`)

`README.md` (index) · `01` architecture · `02` config (used vs legacy env) · `03` data models ·
`04` app.py reference · `05` pipelines & flow · `06` API · `07` UI · `08` modules & scripts ·
`09` how to run · `10` data files · `11` handoff (SerpWow) · `12` per-tab LLM map · `13` AI Mode.
(These describe the SerpWow service; some predate the latest AI-Mode changes in §1.)

---

## 6. Outstanding decisions, TODOs & caveats

### 6a. Address template ↔ cleanup-prompt mismatch (FIXED in the rework — prompts unified with confidence+flags; see top)
The rewritten address `search_query_template.txt` (the **query** sent to Google AI Mode) now asks for a
`Confidence / Flags / Investigation Summary / **Website:**` format. But the address **cleanup**
`SYSTEM_PROMPT` in `scrapedo_finder/extraction.py` (and the `EntityCleanResult` schema: `short_details`,
`official_website`, `found_at_attempt`, `attempt_log`) still expects an **"attempt log" with
`URL: https://...` lines** and even hints "the last attempt-log line containing 'URL: https://...'".
The cleanup LLM probably still finds the URL (it reads the whole text), but the richer confidence/flags
output is partly discarded and the prompt hint is stale. **Decide whether to align the address cleanup
prompt + schema with the new template** (company mode already uses confidence + flags).

### 6b. `AI_MODE_LLM_BATCH` two-phase / Gemini-batch rework — ✅ BUILT (this session, §1)
Implemented in `ai_mode_service.run_ai_mode_sync` (Phase 1 parallel scrape → Phase 2 sync/Gemini-batch
clean → Phase 3 assemble) + new `gemini_batch.py`. Deferred follow-ups (kept in `plancl.md`):
`(name,country)` dedup/cache, and a worker/queue split (only if you exceed the scrape.do ~100 cap).

### 6c. `.env.example` — partially updated
This session **added** the AI Mode batch keys (`AI_MODE_LLM_BATCH`, `SCRAPEDO_CONCURRENCY`,
`GEMINI_BATCH_SHARD_SIZE`, `GEMINI_BATCH_MAX_INFLIGHT`, `AI_MODE_BATCH_POLL_SEC`,
`AI_MODE_BATCH_TIMEOUT_SEC`) and **removed** dead `REDIS_*`. Still missing: `AI_MODE_LOG_LEVEL`,
`API_RELOAD`, `UVICORN_LOG_LEVEL`, and `GEMINI_MODEL` (AI Mode falls back to `gemini-2.5-flash-lite`).

### 6d. scrapeDo file reduction — DONE in the rework (all 8 deleted; see top)
`scrapedo_finder/` has 20 `.py` files; the AI Mode integration uses 12. These **8 are imported by
nothing the integration uses and are safe to delete** (package still imports, AI Mode still works):
`__main__.py`, `cli.py`, `cleanup_cli.py`, `runner.py`, `cleanup_runner.py`,
`company_cleanup_runner.py`, `reporting.py`, `csv_loader.py` (scrapeDo's standalone-CLI layer).
**Kept / used:** `__init__.py`, `models.py`, `settings.py`, `scrapedo_client.py`, `llm_client.py`,
`prompting.py`, `company_prompting.py`, `extraction.py`, `company_extraction.py`,
`cleanup_reporting.py`, `company_reporting.py`, `company_csv_loader.py` + `prompts/`.
→ Awaiting user go-ahead before deleting. (And the requested per-file `scrapedo_finder/README.md` is
still owed.)

### 6e. Carry-over SerpWow caveats (still true)
- **Batch-pending row marking is not pipeline-scoped** (`app.py process_upload_job`): with
  `ENABLE_GEMINI_BATCH_POSTPROCESS=true`, no-URL rows in *any* pipeline are marked completed/pending,
  but the batch only runs for `full`. Enable the flag for `full` only. (`docs/02`, `docs/05` §4.)
- **Two batch-recovery scripts are broken:** `scripts/push_processed_rows_to_gemini_batch.py` (only with
  `--force-new-job`) and `scripts/requeue_wait_and_push_remaining_to_gemini_batch.py` (main path)
  reference `app.upload_lock`, which doesn't exist → `AttributeError`. Fix: `app.get_upload_lock(upload_id)`.
- **No browser/proxy crawling** in the SerpWow service; `massive_proxy_cost_usd` is always `0.0`; several
  `.env.example` keys (`MASSIVE_*`, `BROWSER_POOL_SIZE`, …) are read by nothing (`docs/02` §B).
  (`REDIS_*` was also dead and was **removed** from `.env.example` this session.)
- `analyze_with_gemini` (app.py) is dead code; `templates/ui copy.html` is an unused backup.

---

## 7. Fast orientation for a new agent (superseded — see "Run it" at top)

- **Run it:** `cd website_url_finder && .venv/bin/python app.py` → `http://localhost:8080/ui`
  (port from `.env`). Set `SCRAPEDO_TOKEN` + an LLM key in `.env` first. Don't use Live Server (§0).
- **Work on AI Mode:** read `docs/13.ai-mode.md` + `ai_mode_service.py` (self-contained) +
  `scrapedo_finder/` (clients in `scrapedo_client.py` / `llm_client.py`; prompts in `prompts/`).
- **AI Mode two-phase + Gemini batch (BUILT this session):** read §1, `plancl.md`, `gemini_batch.py`,
  and `run_ai_mode_sync` (Phase 1 parallel scrape → Phase 2 sync/batch clean → Phase 3 assemble).
- **Understand the SerpWow service:** `docs/README.md` → `docs/01` → `docs/04`/`docs/05`.
- **Run tests:** `.venv/bin/python -m unittest discover -s tests` (pytest is **not** installed in
  `.venv`) — `test_timing_summary.py` + `test_gemini_batch.py` (17 tests total).
- **Don't** wire AI Mode into RabbitMQ/state.json — it's intentionally standalone. **Before deleting any
  scrapeDo file**, re-check the import graph and get user confirmation (§6d).
