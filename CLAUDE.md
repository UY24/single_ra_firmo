# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One FastAPI app + one vanilla-JS UI hosting **two independent website-discovery systems** that share an upload/runs/Supabase tracking layer:

1. **SerpWow pipelines** — RabbitMQ + worker + S3, 5 modes (`full`, `url_discovery`, `firmographics`, `gmaps`, `gsearch`). All live in one ~7,800-line monolith: `backend/app/services/serpwow/legacy_app.py`.
2. **AI Mode** — a standalone scrape.do "Google AI Mode" → LLM-cleanup pipeline, 2 modes (`ai_bulk`, `ai_deep`). Lives in `backend/app/services/ai_mode/`.

Python 3.12. The repo root is `website_url_finder/`; the package root is `backend/app/`. There is a `.venv/` at the repo root and `backend/requirements.txt`.

## Run & test

All commands assume the repo-root `.venv`. The **FastAPI `app` object is defined in `legacy_app.py`**, not `main.py` — `main.py` imports it, then adds the AI Mode + companies routers, mounts `/static`, and serves the UI at `/app`. `app.core.config` is imported first so `.env` (at repo root) loads before anything reads env.

```bash
# Web server (two equivalent ways; run.py just sets sys.path/cwd for you)
cd backend && ../.venv/bin/python -m app.main      # or: ../.venv/bin/python ../run.py
# Host/port from env: API_HOST (0.0.0.0), API_PORT (code default 11500; .env overrides),
# API_RELOAD, UVICORN_LOG_LEVEL. UI: http://localhost:<port>/app  (/ui 307-redirects to /app)

# SerpWow worker — ONLY needed for SerpWow pipelines, not for AI Mode.
docker compose up -d rabbitmq        # broker (also exposes mgmt UI on 15672)
python worker.py                     # from repo root; consumes RabbitMQ jobs
# The API is producer-only by default; it consumes in-process only if ENABLE_EMBEDDED_WORKER=true.

# Tests — all offline (no network); unittest, NOT pytest (pytest isn't installed).
cd backend && ../.venv/bin/python -m unittest discover -s tests       # full suite
cd backend && ../.venv/bin/python -m unittest tests.test_s3_layout -v # one module
cd backend && ../.venv/bin/python -m unittest tests.test_results.TestEntityResultSerialization.test_flags_csv  # one test
```

Note: `aio-pika`/`boto3`/`supabase` are imported at module top in `legacy_app.py`, so they must be installed **even for AI-Mode-only use**.

## Architecture (the parts that span files)

### AI Mode engine — `services/ai_mode/ai_mode_service.py`
`run_ai_mode_sync(run_id, resume=False)` is a synchronous, **never-raises** 3-phase pipeline (errors land in `status.json`, `status="failed"`), scheduled from the router via `asyncio.to_thread`:
1. **Scrape** (parallel, `SCRAPEDO_CONCURRENCY`, default 5) — one scrape.do request per batch of `mode.batch_size()` entities; writes `raw_responses/request_NNN.json`. **Resume-aware**: an existing parseable raw file is reused, not re-scraped.
2. **Clean** — per-batch LLM cleanup. Sync (`AI_MODE_LLM_BATCH=false`) via `make_llm_client` (Gemini *or* OpenAI-compatible), or the **Gemini Batch API** (`=true`, forces the Gemini provider) sharded/polled in `gemini_batch.py`. **The Gemini batch input always goes through the File API (JSONL upload → `input_config.file_name`)** — there is no inline route (`create_batch` is single-path, any size). Results map back **by key**, never by position. **Resume-aware like Phase 1**: each successfully-cleaned batch is persisted to `cleaned/<key>.json` (`{key,text,usage}`, both paths), and a re-run pre-loads those and only re-submits the still-uncleaned batches.
3. **Assemble** — computes cost, writes outputs, then mirrors the run dir to S3 (**also mirrored on the failure path** so crashed runs are debuggable from the bucket).

`status.json` is persisted after every batch for UI polling. Modes are defined in `mode_config.py` (`ai_bulk`=batch 10 / `ai_bulk_search.txt`; `ai_deep`=batch 3 / `ai_deep_search.txt`; both share `ai_cleanup.txt`). Provider/keys: `build_ai_mode_llm_config` reads `AI_MODE_LLM_PROVIDER` (`gemini`|`openai`, `OPENAI_BASE_URL` for gateways); validated at **upload time** (`prepare_ai_mode_run`, 400 on missing key) and again at run time (`SCRAPEDO_TOKEN` required).

**One retry action** (`POST …/{run_id}/resume` → `run_ai_mode_sync(run_id, resume=True)`, labelled "Rerun failed" in the UI): re-enters the *same* run_id and redoes only what failed — Phase 1 reuses `raw_responses/` and re-scrapes only batches that failed to scrape (no scrape.do re-spend on successes), Phase 2 reuses `cleaned/` and re-cleans only the failed batches. It keeps the prior `run.log` (no wipe), clears stale `gemini_batch_jobs`, and updates the existing Supabase row (no new row). Allowed only for `failed`/`completed_with_errors` (409 otherwise). **Genuinely not-found rows are not retried** — those go through AI Mode Deep (`ai_deep`), not a retry. (The old row-level "rerun" endpoint + `rerun.py`/`carryover.json` were removed — re-running not-found rows with the same prompt just reproduced the same answer.)

### SerpWow monolith — `services/serpwow/legacy_app.py`
Owns the FastAPI `app`, all upload/status/output/batch endpoints, the RabbitMQ producer (`publish_job` on upload) + consumer (`process_upload_job` per row), per-row pipeline executors, S3 persistence, Gemini-batch post-processing, hand-rolled XLSX export, and Supabase tracking. Pipeline type lives in `state["pipeline"]` and flows into every job. The upload `state` dict (built in `_create_upload_with_rows`) is the source of truth; status is **derived** by `summarize_upload_state` (`queued`→`processing`→`completed`/`completed_with_errors`), never hand-set on rows. When RabbitMQ is down, the app still boots but upload endpoints return **503**.

### Shared layers
- **Canonical CSV input** (`models/entities.py`, `parse_entities_csv`): requires a company-name column (aliases `company_name|company|name|entity_name|entity|organization|organisation|legal_name`) **and** a country column (`country|country_name|nation`); optional `company_local_name|address|firm_id|industry`. Headerless 2+-col files parse positionally. The **old `Company Name ENG`/`Country Code`/`ISIC` format is rejected with 400** (no auto-detect). `InvalidCSVError` → 400.
- **Per-entity result schema** (`models/results.py`): `EntityResult{website_url, confidence(0–100), flags[{flag,why}], attempt_log[{query,result,url}], error}`. `flags_csv()`/`attempt_log_csv()` join with `"\n"`, so each flag/attempt is its own line **inside one quoted CSV cell** (a real embedded newline — Excel renders multi-line; it does not break the row).
- **S3 layout** — bucket `S3_BUCKET` (currently `website-url-finder`, region `S3_REGION`=`ap-south-1`), keyed **`<company>/<pipeline>/<run_id|upload_id>/…`**. AI Mode uses the shared helper `core/s3.py` (`upload_directory`) and mirrors the whole run dir at the end of a run via `s3_sync.mirror_run_to_s3` — **best-effort, never fails the run**. SerpWow keeps its *own* S3 client inside `legacy_app.py` (`_upload_s3_prefix`, `write_upload_artifact`). The new bucket has no shared top-level prefix, so cold-start scanners paginate the whole bucket.
- **Supabase tracking** (`core/supabase_client.py`, `services/companies.py`, `supabase/migrations/`): `companies` + `runs` tables. `SUPABASE_URL` must be the **bare** `https://<ref>.supabase.co` (not the `:5432` postgres string); `SUPABASE_SERVICE_ROLE_KEY` is the long JWT. Unconfigured → company/AI-upload endpoints return **503**; SerpWow run writes are best-effort and never fail a run.
- **UI** (`static/js/`, `templates/index.html`): vanilla ES-module SPA with a hash router (`main.js`). Each view module exports `render(root, params)` returning an optional cleanup fn. `api.js` has `api()` (fetch + one retry), `pollStatus()` (2s until terminal), and `el(tag, attrs, …children)`.

## Disk layout of run outputs
- AI Mode: `ai_mode_results/<company_slug>/<run_id>/` — `input.csv`, `status.json`, `final_report.json`, `found.csv`, `notFound.csv`, `run.log`, `raw_responses/request_NNN.json`, `cleaned/batch-NNNNNN.json` (one per successfully-cleaned batch; enables the "Rerun failed" resume). Legacy read-only dir: `ai_mode_result/` (singular).
- SerpWow: local `/tmp/single_ra_isi/<company>/<upload_id>/` (`state.json`, `output.json`, batch files). Both a legacy flat (`<upload_id>/`) and nested (`<company>/<upload_id>/`) on-disk layout must be tolerated.

## Conventions & gotchas
- **Never add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits.** Tell any committing subagent the same.
- **Never `git push` and never query the production Supabase DB without explicit user approval.**
- `docs/` is **gitignored** — specs/plans/handoff there are on-disk only; code under `backend/` commits normally.
- `el()` sets every attr via `setAttribute`, so `el("a", {href: undefined})` literally sets `href="undefined"`. Use the spread pattern: `...(cond ? {href: x} : {})`.
- SerpWow's batch-pending row marking is **not pipeline-scoped**: with `ENABLE_GEMINI_BATCH_POSTPROCESS=true`, no-URL rows in *any* pipeline get marked pending, but the batch only runs for `full`. Enable the flag for `full` only.
- scrape.do is treated as a flat fee — runs report a `scrapedo_searches` count, not a USD figure.
- The full env reference lives in `.env.example` (sections for SerpWow, AI Mode batch/cleanup, scraping, cost, Supabase, S3).
