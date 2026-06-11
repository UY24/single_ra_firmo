# Design — `website_url_finder` rework (unified input, Supabase company tracking, AI Mode 1/2, new HTML UI, full restructure)

Date: 2026-06-11. Status: approved by user (brainstorming session). Supersedes nothing; builds on the
state described in `HANDOFF.md` (2026-06-10).

## 1. Goals

1. **One canonical CSV input format** across every pipeline (gmaps, gsearch, full, firmographics,
   url_discovery, AI modes). Non-conforming uploads are rejected at upload time with HTTP 400.
2. **Two AI modes, one engine**: **AI Mode 1 — Bulk** (`ai_bulk`, large batches, maximize coverage)
   and **AI Mode 2 — Deep Search** (`ai_deep`, 2–3 entities per request, thorough detailed prompt).
3. **Supabase company tracking**: every run belongs to a client *company*. Per-company stats
   (success/failed counts, token usage, cost, output file links) are queryable.
4. **Per-company output layout** on disk: `ai_mode_results/<company_name>/<run_id>/…`, lean file set
   (single `final_report.json`, single `run.log`).
5. **Full backend restructure**: break the 7.6k-line `app.py` into a proper package; absorb
   `scrapedo_finder/`; move ALL prompts (search **and** LLM cleanup) into prompt files.
6. **New structured HTML UI** (internal tool; clean, classy; served by FastAPI, no build step)
   replacing the single 2,300-line `templates/ui.html`.
7. Add-ons (approved): **cost accuracy** (scrape.do + LLM cost per run), **CSV preview before run**,
   **re-run button** for failed/partial runs. Explicitly **out of scope**: `(name,country)` dedup
   cache (deferred again), S3 storage for AI mode outputs (local paths now; schema is S3-ready),
   rewriting the AI search prompt wording (user will refine the prompt text later — we only create
   the files and the `{entities}` plumbing).

## 2. Repo restructure

Target layout (top level of `website_url_finder/`):

```
backend/
  app/
    main.py                  # FastAPI app factory + startup/shutdown wiring only
    core/
      config.py              # ALL env reading in one place (typed settings)
      supabase_client.py     # thin Supabase wrapper (httpx/postgrest or supabase-py)
      s3.py                  # existing S3 helpers (SerpWow)
      rabbitmq.py            # existing RabbitMQ setup/teardown (SerpWow only)
      logging.py             # leveled per-run logger, secret redaction (sanitize_*)
    routers/
      companies.py           # company CRUD + per-company stats
      uploads.py             # gmaps / gsearch / full / firmographics / url_discovery uploads
      ai_mode.py             # ai_bulk / ai_deep endpoints (upload, preview, status, result, rerun)
      batches.py             # Gemini batch manager endpoints (SerpWow)
      results.py             # result/report download endpoints
    services/
      serpwow/               # worker loop, pipeline processors, query building, URL scoring,
                             # gemini wrappers, XLSX builder, state persistence — moved verbatim
                             # from app.py in pure-move steps (no behavior change)
      ai_mode/
        engine.py            # two-phase pipeline (parallel scrape-all → clean-all → assemble),
                             # parameterized by ModeConfig (ai_bulk / ai_deep)
        run_store.py         # run dirs, status.json, listing, result paths
        scrapedo_client.py   # from scrapedo_finder (kept)
        llm_client.py        # from scrapedo_finder (kept)
        extraction.py        # unified cleanup: builds messages from prompts/ai_cleanup.txt
        reporting.py         # found.csv / notFound.csv / final_report.json writers
        gemini_batch.py      # moved from repo root, unchanged
      companies.py           # Supabase company + run-row lifecycle service
    models/
      entities.py            # unified Entity model + the ONE canonical CSV parser
      results.py             # EntityResult (confidence, flags, attempt_log), run/report models
    prompts/
      ai_bulk_search.txt     # AI Mode 1 search prompt ({entities} placeholder)
      ai_deep_search.txt     # AI Mode 2 detailed thorough-search prompt (placeholder wording)
      ai_cleanup.txt         # LLM cleanup system prompt — moved OUT of extraction.py code
    static/                  # UI assets served by FastAPI — see §7
      css/                   # app.css (+ Tailwind via CDN in the page)
      js/                    # one module per page/section (api.js, dashboard.js, companies.js,
                             # new_run.js, run_detail.js, runs.js, operations.js)
    templates/               # ui pages (Jinja or plain HTML) — replaces templates/ui.html
  tests/
  requirements.txt
supabase/
  migrations/001_init.sql
ai_mode_results/<company_name>/<run_id>/   # outputs, gitignored — see §6
docs/
samples/                     # sample_company.csv etc. moved here; stray root CSV/XLSX deleted
```

Notes:
- `scrapedo_finder/` is absorbed into `services/ai_mode/`. The 8 dead CLI-layer files
  (`__main__.py`, `cli.py`, `cleanup_cli.py`, `runner.py`, `cleanup_runner.py`,
  `company_cleanup_runner.py`, `reporting.py`, `csv_loader.py`) are **deleted**
  (HANDOFF §6d — user has now approved deletion via the "full restructure" decision).
  `company_csv_loader.py`, `company_extraction.py`, `company_prompting.py`,
  `company_reporting.py`, `cleanup_reporting.py`, `prompting.py` are merged into the new unified
  `extraction.py` / `reporting.py` / prompt files (the company/address split disappears).
- Restructure method: **pure-move first** (no behavior change), verified after each move by
  `python -c "import app.main"` + the existing test suite; features layered on only after moves pass.
- The two broken recovery scripts (`scripts/push_processed_rows_to_gemini_batch.py`,
  `scripts/requeue_wait_and_push_remaining_to_gemini_batch.py`) get their `app.upload_lock` →
  `get_upload_lock(upload_id)` fix during the move.
- `templates/ui.html` stays functional as a fallback during the build; deleted in the final step.

## 3. Unified input format

One parser (extracted from today's `app.py parse_csv_rows`, moved to `models/entities.py`), used by
**every** pipeline:

| Field | Required | Header aliases (normalized) |
|---|---|---|
| `company_name` | yes | company_name, company, name, entity_name, entity, organization, organisation, legal_name |
| `country` | yes | country, country_name, nation |
| `company_local_name` | no (**new**) | company_local_name, local_name, company_name_local, name_local |
| `address` | no | full_address, address, fulladdress, input_full_address |
| `firm_id` | no | firm_id, firmid, id |
| `industry` | no | industry, input_industry |

- Positional fallback (col0=company, col1=country) is **kept** for headerless files, matching today's
  gmaps/gsearch behavior.
- A CSV whose headers match neither the aliases nor positional fallback, or with zero valid rows →
  **HTTP 400** whose message lists the required and accepted columns.
- AI Mode's company/address auto-detection (`_detect_input_type`), `load_company_entities`
  (`Company Name ENG` format) and `load_address_entities` are **removed**. Old-format files now fail
  with the 400 above.
- **Prompt `{entities}` building**: each entity line includes only fields that are present. e.g.
  `1. Acme KK (local: アクメ株式会社) — Japan — 1-2-3 Shibuya, Tokyo — industry: manufacturing` when all
  optional fields exist; just `1. Acme KK — Japan` when none do. Same rule for every optional field.

## 4. Supabase (lean, two tables)

FastAPI is the **only** Supabase client (service-role key, server-side). The UI never talks to
Supabase directly. SQL migration shipped in `supabase/migrations/001_init.sql`:

```sql
create table companies (
  id uuid primary key default gen_random_uuid(),
  name text unique not null,
  created_at timestamptz not null default now()
);

create table runs (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id),
  pipeline text not null,        -- gmaps | gsearch | full | firmographics | url_discovery | ai_bulk | ai_deep
  run_ref text not null,         -- on-disk run_id / upload_id
  status text not null,          -- queued | running | completed | completed_with_errors | failed
  total_rows int,
  success_count int, failed_count int,
  websites_found int, websites_not_found int,
  token_usage jsonb,             -- {input_tokens, output_tokens, total_tokens} (per provider when mixed)
  cost jsonb,                    -- {llm_usd, scrapedo_usd, total_usd, scrapedo_requests}
  duration_seconds numeric,
  file_links jsonb,              -- {"found.csv": "<local path or S3 url>", ...}
  rerun_of uuid references runs(id),
  error text,
  started_at timestamptz, finished_at timestamptz,
  created_at timestamptz not null default now()
);
create index runs_company_idx on runs(company_id);
```

Lifecycle & rules:
- Every upload endpoint (all pipelines) requires `company_id` (form field). Missing/unknown → 400.
- Run row inserted at upload (`status=queued`), updated to `running` at start and to the terminal
  status with all stats + `file_links` at completion.
- Per-company stats = SQL aggregates over `runs` (no separate stats table).
- **Supabase is bookkeeping, never a point of failure**: if Supabase is unreachable mid-run, the run
  completes on disk; the final stats update is retried (3 attempts, logged) and failure to record is
  logged loudly but does not fail the run. Company validation at upload time DOES fail-fast (400/503)
  because no run has started yet.
- New env: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.

### Cost accuracy (add-on #2)
- `cost.llm_usd`: existing token-usage → cost computation (Gemini/OpenAI, sync and batch).
- `cost.scrapedo_usd`: scrape.do reports per-request credit cost in response headers
  (`Scrape.do-Request-Cost` — verify exact header at implementation time); sum when present, else
  estimate `request_count × SCRAPEDO_COST_PER_REQUEST_USD` (new env, default documented in
  `.env.example`). `cost.scrapedo_requests` records the request count either way.
- `cost.total_usd = llm_usd + scrapedo_usd`. Same fields also appear in `final_report.json`.

## 5. AI Mode engine — one engine, two configs

The existing two-phase pipeline (Phase 1 parallel scrape-all with resume, Phase 2 clean-all sync or
Gemini Batch via `AI_MODE_LLM_BATCH`, Phase 3 assemble) is kept and parameterized by a `ModeConfig`:

| | AI Mode 1 — Bulk (`ai_bulk`) | AI Mode 2 — Deep Search (`ai_deep`) |
|---|---|---|
| Intent | send more entities per request; get as much as we can | small batches; detailed, thorough multi-angle search |
| Batch size | env `AI_BULK_BATCH_SIZE` (default 10) | env `AI_DEEP_BATCH_SIZE` (default 3; 2 allowed) |
| Search prompt | `prompts/ai_bulk_search.txt` | `prompts/ai_deep_search.txt` |
| Cleanup prompt | shared `prompts/ai_cleanup.txt` | same |

- Prompt files are created with working placeholder wording adapted from today's templates; the user
  will refine the wording later. The deep-search placeholder instructs multi-angle investigation
  (official registries, local-language search, TLD checks) and per-entity confidence/flags/attempt
  reporting.
- The legacy single AI Mode tab/endpoints are replaced; `SCRAPEDO_BATCH_SIZE` env is superseded by
  the two per-mode envs (kept as fallback for one release, read by `ai_bulk` if set).

### Unified output schema (both modes — fixes HANDOFF §6a)

`EntityResult`:

```
company_name: str
company_local_name: str | None
country: str
website_url: str | None
confidence: int (0–100)
flags: list[{flag: str, why: str}]        # ALL flags, each with short info
attempt_log: list[{query: str, result: str, url: str | None}]   # ALL attempts
error: str | None
sno: int
```

- The cleanup prompt (`ai_cleanup.txt`) and its JSON schema are aligned to this shape — the stale
  "URL: https://… last line" hint is removed; confidence + flags + attempt log are first-class for
  both modes (previously company-mode only).
- CSV serialization: `flags` → `"name_match: exact ENG match; tld_match: .jp matches country"`;
  `attempt_log` → one attempt per line within the cell
  (`1. <query> → <result> [url]`). Columns for `found.csv` / `notFound.csv`:
  `company_name, company_local_name, country, website_url, confidence, flags, attempt_log`
  (`notFound.csv` keeps the same columns; `website_url` empty, plus `error`).
- `final_report.json` per-entity entries carry the full structured `flags` / `attempt_log` arrays.

## 6. Output layout per run

```
ai_mode_results/<company_name>/<run_id>/
├── input.csv
├── found.csv
├── notFound.csv
├── final_report.json        # ONE report: summary block + per-request details array
│                            # (today's report.json + final_report.json merged; report.json removed)
├── run.log                  # single leveled log (today's run.log + ai_mode_debug.log merged;
│                            # level via AI_MODE_LOG_LEVEL, secrets redacted)
└── raw_responses/request_NNN.json
```

- `<company_name>` is the Supabase company's name, slugified for filesystem safety (lowercase,
  spaces→`-`, unsafe chars stripped); the run row links name↔dir unambiguously via `run_ref`.
- All produced files are registered in the run's `file_links` (local absolute paths now; when S3
  arrives, same column holds S3 URLs — no schema change).
- Result-download endpoint allowlist updated to the new file set; JSON still passes through
  `sanitize_for_response`.
- SerpWow pipelines keep their existing output locations (S3/state.json) — only their **run rows +
  file links** are added to Supabase; their on-disk layout is not reorganized in this project.

## 7. UI — structured HTML served by FastAPI (no build step)

Decision revised from Next.js → **HTML-only**: internal tool, table-data dashboard, no graphs —
a framework isn't worth a second app + build pipeline. The fix for today's UI is structure, not
React.

- Served by FastAPI exactly like today (`/ui`), one server, zero build step, no Node.
- `backend/app/templates/` holds the pages; `backend/app/static/css|js/` holds assets, mounted via
  `StaticFiles`. **One JS module per page/section** (`api.js` shared fetch/polling helpers,
  `dashboard.js`, `companies.js`, `new_run.js`, `run_detail.js`, `runs.js`, `operations.js`) — no
  more single 2,300-line file.
- Tailwind via CDN + a small `app.css`. Clean, classy internal-tool aesthetic — restrained palette,
  generous whitespace, consistent cards/tables.
- Plain `fetch` + the existing 2s status polling pattern. No auth in v1.
- If a React frontend is ever genuinely needed, the FastAPI API layer is unchanged — it can be added
  later without backend rework.

Pages (each a view/tab within the app shell):
1. **Dashboard** (`/`) — company cards with aggregate stats (runs, success rate, websites found,
   tokens, total cost) + recent runs feed.
2. **Companies** (`/companies`) — create company (name), list with stats, click-through to runs.
3. **New Run** (`/runs/new`) — stepper: select company → select pipeline (Gmaps, Gsearch, Full,
   Firmographics, AI Bulk, AI Deep) → upload CSV → **preview step** (add-on #3: server parses first
   5 rows, returns detected column mapping + sample rows + row count + any warnings; user confirms
   before the run starts and tokens are spent; new endpoint `POST /uploads/preview`) → run starts →
   redirect to run detail.
4. **Run detail** (`/runs/<id>`) — live status (2s polling, existing status endpoints), phase
   (scraping/cleaning), progress, stats (counts, tokens, cost), file downloads, and a **Re-run
   button** (add-on #4): creates a new run for the same company+pipeline reusing the stored
   `input.csv`, feeding in **only rows that did not succeed** (notFound-with-error + never-scraped);
   successful rows are carried over from the previous run's results into the new run's outputs; the
   new run records `rerun_of`. Available for `failed` and `completed_with_errors` AI-mode runs (v1:
   AI modes only; SerpWow keeps its existing retry tab ported under Operations).
5. **Runs** (`/runs`) — filterable history (company, pipeline, status, date).
6. **Operations** (`/operations`) — ports of today's Batch Manager and Retry Operations tabs
   (functionality preserved, restyled).

The legacy Upload Console / Firmographics / Gmaps Lookup / Gsearch Phases tabs are subsumed by
New Run + Run detail + Operations. Anything genuinely missing is ported into Operations rather than
dropped.

## 8. Error handling, migration & testing

- **Move-then-modify discipline**: each restructure step is import-checked + test-suite-verified
  before the next; behavior changes only land after the moves are green.
- **Existing tests** (17: `test_timing_summary.py`, `test_gemini_batch.py`) keep passing throughout
  (paths updated as files move).
- **New tests** (offline, unittest, consistent with the repo): unified parser accept/reject/alias
  cases incl. `company_local_name` and positional fallback; `{entities}` block building with/without
  optional fields; ModeConfig selection (`ai_bulk` vs `ai_deep`); flags/attempt_log CSV
  serialization; companies service with mocked Supabase (insert/update/retry-on-failure); rerun row
  selection (which rows re-feed); preview endpoint; cost computation (header-sum vs estimate paths).
- **Runtime**: Python 3.12 venv (existing); `requirements.txt` gains the Supabase client lib.
  No Node/JS toolchain — the UI is static assets served by FastAPI.
- **.env.example completed**: adds `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `AI_BULK_BATCH_SIZE`, `AI_DEEP_BATCH_SIZE`, `SCRAPEDO_COST_PER_REQUEST_USD`, plus the previously
  missing `AI_MODE_LOG_LEVEL`, `API_RELOAD`, `UVICORN_LOG_LEVEL`, `GEMINI_MODEL`.
- **No RabbitMQ/Redis for AI modes** (unchanged architecture decision); SerpWow keeps RabbitMQ.

## 9. Out of scope (explicit)

- `(name,country)` dedup/result cache (suggestion #1 — declined for now; remains the top future cost
  lever, noted for later).
- AWS S3 storage for AI-mode outputs (local only this phase; `file_links` is S3-ready).
- Final wording of `ai_bulk_search.txt` / `ai_deep_search.txt` (user refines later).
- Auth on the internal UI.
- Reorganizing SerpWow's on-disk/S3 output layout.
