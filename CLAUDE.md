# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One FastAPI app + one vanilla-JS UI hosting **two independent website-discovery systems** that share an upload/runs/Supabase tracking layer:

1. **SerpWow pipelines** — RabbitMQ + worker + S3, 4 modes (`firmographics`, `gmaps`, `gsearch`, `relationship`). (The legacy `full` and `url_discovery` modes — plain search + gmaps-fallback via `execute_company_lookup` — were removed 2026-07-20; gsearch-all-phases superseded them.) The engine (FastAPI `app`, endpoints, worker, state/persist, S3 store, Gemini-batch driver, Supabase/Slack) lives in `backend/app/services/serpwow/engine.py` (formerly the ~8.3k-line `legacy_app.py` monolith; now ~4.1k after extracting focused sibling modules — see the module map in that file's docstring). Pipeline helpers are in sibling modules (`geo`, `cost`, `url_utils`, `address`, `query_builders`, `gmaps_scoring`, `serpwow_client`, `gemini_llm`, `csv_input`, `relationship_csv`, `output_export`, `reporting`, `constants`, `schemas`, `row_logging`; the S3-only pipelines share `s3_run_store` + `s3_run_driver` and add `gmaps_runner`/`gmaps_outputs` and `relationship_runner`/`relationship_outputs`); per-mode row executors are in `serpwow/modes/{gsearch,gmaps,firmographics,relationship,common}.py`; `scrapedo_maps_client.py` is the gmaps pipeline's scrape.do Google Maps client (it replaced the SerpWow `gmaps_client.py`, deleted 2026-08) and `scrapedo_search_client.py` is the firmographics pipeline's scrape.do Google Search client (it replaced `codetails.py`, deleted 2026-08-19). Cross-provider helpers shared with AI Mode live in `backend/app/services/common/` (`text`, `env`). The `relationship` mode (2026-07-08; migrated off SerpWow onto scrape.do Google AI Mode 2026-08 — see the dedicated section below) verifies an X↔Y financial relationship from OCR'd portfolio-page CSVs and emits Y's website ONLY when the relationship is confirmed (gate in `gemini_llm.apply_relationship_gate`). Its CSV carries exactly three inputs — `Company_Name_X`, `Company_Name_Y`, `Input_URL` — and one row in is one row out (no (X,Y) dedup, no location: the OCR'd page supplies none) — see `docs/HISTORY.md` 2026-07-08.
2. **AI Mode** — a scrape.do "Google AI Mode" → LLM-cleanup pipeline, 2 modes (`ai_bulk`, `ai_deep`). Lives in `backend/app/services/ai_mode/`. **Broker-driven since 2026-07**: the scrape phase is queued through RabbitMQ (own durable queue/channel on the shared broker) and executed by the worker process — built for 500k–1M-row runs (bounded memory, at-least-once redelivery, reconciler self-healing).

Python 3.12. The repo root is `website_url_finder/`; the package root is `backend/app/`. There is a `.venv/` at the repo root and `backend/requirements.txt`.

## Run & test

All commands assume the repo-root `.venv`. The **FastAPI `app` object is defined in `serpwow/engine.py`**, not `main.py` — `main.py` imports it, then adds the AI Mode + companies routers, mounts `/static`, and serves the UI at `/app`. `app.core.config` is imported first so `.env` (at repo root) loads before anything reads env.

```bash
# Web server (two equivalent ways; run.py just sets sys.path/cwd for you)
cd backend && ../.venv/bin/python -m app.main      # or: ../.venv/bin/python ../run.py
# Host/port from env: API_HOST (0.0.0.0), API_PORT (code default 11500; .env overrides),
# API_RELOAD, UVICORN_LOG_LEVEL. UI: http://localhost:<port>/app  (/ui 307-redirects to /app)

# Worker — needed for SerpWow pipelines AND AI Mode (the AI-Mode scrape phase is
# broker-driven since the 2026-07 rework; AI-Mode uploads 503 when RabbitMQ is down).
docker compose up -d rabbitmq        # broker (also exposes mgmt UI on 15672)
python worker.py                     # from repo root; consumes SerpWow + ai_mode_jobs queues
# The API is producer-only by default; it consumes in-process only if ENABLE_EMBEDDED_WORKER=true.
# Run exactly ONE worker process — the AI-Mode finish/reconciler task registry is in-process.

# Tests — all offline (no network); unittest, NOT pytest (pytest isn't installed).
cd backend && ../.venv/bin/python -m unittest discover -s tests -t .       # full suite (the -t . makes tests import as the 'tests' package so tests/__init__.py's hermeticity guard runs)
cd backend && ../.venv/bin/python -m unittest tests.test_s3_layout -v # one module
cd backend && ../.venv/bin/python -m unittest tests.test_results.TestEntityResultSerialization.test_flags_csv  # one test
```

Note: `aio-pika`/`boto3`/`supabase` are imported at module top in `engine.py`, so they must be installed **even for AI-Mode-only use**.

## Architecture (the parts that span files)

### AI Mode engine — broker-driven since 2026-07 (`ai_mode/{broker,worker,ai_mode_service}.py`)
Built for 500k–1M-row runs. **One RabbitMQ message = one scrape.do batch** (entities inline; 1M rows @ ai_bulk-10 = 100k messages), on a **separate durable queue** `ai_mode_jobs` (`AI_MODE_QUEUE`) bound to the shared exchange under `ai_mode.scrape`, consumed on AI Mode's **own channel** (aio_pika QoS is per-channel; prefetch + consumer count = `AI_MODE_WORKER_CONCURRENCY`, default 20, raisable toward scrape.do's ~200-concurrency cap) inside the **same `worker.py` process** as SerpWow.

**Durable per-batch state is FILE PRESENCE, not a state dict**: scraped ⇔ `raw_responses/request_NNNNNN.json` parses; terminal scrape failure ⇔ `request_NNNNNN.error.json` marker; cleaned ⇔ `cleaned/batch-NNNNNN.json` parses — all write-through mirrored to S3. `status.json` holds only O(1) counters (`batches_total/batches_done/phase/engine/requeue_attempts…`), flushed at most every `AI_MODE_STATUS_FLUSH_SEC` (2s). **Single-writer rule**: the API writes status.json only before publishing (prepare + resume prep + the publisher's pre-publish stamp); from the first publish on, only the worker writes it. Counters are a cache — the barrier always recounts the directory.

Run lifecycle:
1. **Upload** (`routers/ai_mode.py`): 503 before prepare when `broker.is_ready()` is false; otherwise prepare + Supabase row, then a background `worker.publish_run_batches(run_id)` publishes all scrape messages + one trailing `check` kick (publish failures tolerated — the reconciler republishes).
2. **Scrape** (`worker.process_scrape_job`): idempotent — parseable raw file or error marker ⇒ skip; scrape.do API failure is a **result** (error marker + ack, SerpWow-parity `error/scrapedo` taxonomy); infra crashes get one redelivery then are terminalized (poison guard). **Ack only after the raw/error file is durably on disk** (at-least-once). The last ack's recount flips `phase` scraping→cleaning and dispatches the finish task exactly once (per-run lock + `_finish_tasks` registry).
3. **Finish** (`ai_mode_service.run_ai_mode_finish` — Phases 2+3, never raises): LLM cleanup — sync (`LLM_BATCH=false`) via `make_llm_client` (Gemini *or* OpenAI-compatible) or the **Gemini Batch API** (`=true`, forces Gemini; File-API JSONL, `GEMINI_BATCH_SHARD_SIZE` 5000 / `GEMINI_BATCH_MAX_INFLIGHT` 5; shard bodies built just-in-time from disk; results map back **by key**, each persisted to `cleaned/<key>.json` as a durable checkpoint; heartbeat persists `updated_at` every poll) — then **streaming assembly** via `run_reporting.StreamingRunReport`: reads one batch's raw+cleaned files at a time (memory O(one batch), no run-wide results list), writes found/notFound CSVs incrementally, caps `final_report.json`'s `entities` array at `AI_MODE_REPORT_ENTITIES_MAX` (50k; above → `entities_omitted:true`), computes cost, mirrors to S3 (also on the failure path), updates Supabase, Slack ping.

**Reconciler** (`worker.reconcile_ai_mode_runs`, worker startup + folded into `periodic_batch_reconciler`): (a) publishing/scraping runs with missing batches and no activity for `AI_MODE_BATCH_STALE_TIMEOUT_SEC` (900s) get missing batches republished (safe — workers skip existing files) up to `AI_MODE_BATCH_MAX_REQUEUE` (1) times, then terminalized via error markers so the barrier ALWAYS resolves (gated on a drained queue); (b) `cleaning` runs with no live finish task are re-dispatched (resumable via `cleaned/`); (c) **phantom fix**: legacy (`engine != "broker"`) runs stuck `queued/running` past `AI_MODE_LEGACY_STALE_SEC` (3600s) are flipped to `failed` so the UI's "Rerun failed" button appears.

`status.json` is polled by the UI as before (fields additive: `engine`, `phase`, `requeue_attempts`…). Modes in `mode_config.py` (`ai_bulk`=batch 10 / `ai_bulk_search.txt`; `ai_deep`=batch 3 / `ai_deep_search.txt`; both share `ai_cleanup.txt`). Provider/keys: `build_ai_mode_llm_config` is **Gemini-only** — `GEMINI_API_KEY` + `GEMINI_MODEL` (or `llm_batch.batch_model()` in batch mode); validated at **upload time** (`prepare_ai_mode_run`, 400 on missing key) and again in the worker (`SCRAPEDO_TOKEN` required) — so the worker's env needs the scrape.do/LLM/S3/Supabase/Slack vars too.

**One retry action** (`POST …/{run_id}/resume`, labelled "Rerun failed"): allowed only for `failed`/`completed_with_errors` (409 otherwise), 503 when the broker is down. It deletes `*.error.json` markers (best-effort deleting their S3 mirrors too), clears `gemini_batch_jobs`/`requeue_attempts`, then republishes **only** batches without a parseable raw file (`publish_run_batches(only_missing=True)`) — no scrape.do re-spend on successes; the finish task reuses `cleaned/` so only failed LLM work is redone. Updates the existing Supabase row (no new row). If the local run dir is gone (ephemeral host), it first **rehydrates from S3** (`s3_sync.rehydrate_run_from_s3`). This is also the migration path for legacy pre-broker failed runs (same file layout). **Genuinely not-found rows are not retried** — those go through AI Mode Deep (`ai_deep`). **Offline tests** drive this whole engine without RabbitMQ via `tests/ai_mode_drive.py` (captures publishes, feeds them through `process_scrape_job`, awaits the finish task).

### SerpWow engine — `services/serpwow/engine.py` (formerly `legacy_app.py`)
Owns the FastAPI `app`, all upload/status/output/batch endpoints, the RabbitMQ producer (`publish_job` on upload) + consumer (`process_upload_job` per row), per-row pipeline executors, S3 persistence, Gemini-batch post-processing, hand-rolled XLSX export, and Supabase tracking. Pipeline type lives in `state["pipeline"]` and flows into every job. The upload `state` dict (built in `_create_upload_with_rows`) is the source of truth; status is **derived** by `summarize_upload_state` (`queued`→`processing`→`completed`/`completed_with_errors`), never hand-set on rows. When RabbitMQ is down, the app still boots but upload endpoints return **503**.

### gsearch pipeline (SerpWow Google AI Overview) — confidence + AI-Mode-parity outputs
gsearch hits SerpWow `/live/search` (`engine=google`, `include_ai_overview=true`) across phase queries (`build_selected_phase_queries`, phase1–5/fallback; phase5 pivots on people/trade-names), gathers candidate URLs (disallow-filtered via `is_disallowed_official_url`), then runs an **LLM confidence/selection step** — `choose_final_website_with_gemini` (ported from `single_ra`; now in `serpwow/gemini_llm.py`, re-exported from `engine.py`). It picks `official_website` **from the candidate list only** (never invents) and scores `confidence_score` 0–100, re-validated against the candidate set + `_official_website_looks_plausible` (out-of-set/implausible → null + 0). The result lands in `context.final_url_selection_ai.raw` (per-row mode) or `context.gemini_batch_ai.raw` (batch mode) — which is why the `output_confidence_score`/`output_confidence` XLSX columns finally populate.
- **Two confidence modes via `LLM_BATCH`** (the one global toggle, default false): per-row (Gemini call inside the worker, `await asyncio.to_thread`) or **chunked** Gemini-batch at finalization (File-API JSONL, one Gemini job per `GEMINI_BATCH_SHARD_SIZE` rows, up to `GEMINI_BATCH_MAX_INFLIGHT` concurrent jobs). A single helper `_batch_postprocess_enabled_for(pipeline)` gates batching: `gsearch` and `firmographics`→`LLM_BATCH`, else off (`relationship` is absent from this helper — since its 2026-08 migration it owns its own Gemini Batch driver in `relationship_runner.py` and never routes through this shared row-batch engine; used at all 5 batch gate sites — the 3 `maybe_*` guards + 2 seeders + the row-pending gate). `ENABLE_FINAL_URL_GEMINI` (default true) can disable the confidence call entirely. Chunk state is persisted in `state["gemini_batch"]["chunks"]`; aggregate `gemini_batch.status` is `succeeded`/`completed_with_errors`/`failed`/`skipped`. The **always-on terminalization reconciler** (`reconcile_stuck_gsearch_rows`, folded into `periodic_batch_reconciler` in the worker process) guarantees the Phase 1→2 barrier by force-failing stuck rows after `GSEARCH_ROW_STALE_TIMEOUT_SEC` / `GSEARCH_ROW_MAX_REQUEUE` attempts, using durable timestamps — deployment-agnostic (no shared memory). The old poll-gated `maybe_requeue_stuck_queued_rows` status-endpoint trigger is **removed**.
- **Outputs** (parity with AI Mode): at terminal status, `_finalize_serpwow_outputs` calls `reporting.write_outputs(upload_dir, state)` → `found.csv`/`notFound.csv` (shared `EntityResult` columns) + `report.json` (`{summary, rows}`, summary has `cost{llm_usd, serpwow_searches, serpwow_usd, total_usd}` + `token_usage`) + `run.log`, written next to `state.json`/`output.json` and best-effort mirrored to S3. `reporting.py` (renamed from `gsearch_reporting.py` — it's pipeline-agnostic now, shared with gmaps; see below) is standalone (imports only `models/results.py`) to avoid a circular import. Batch mode re-finalizes via the same `persist_upload_state` hook after all chunks are terminal.
- **Files endpoint**: `GET /uploads/{upload_id}/result?file=<name>` (allowlist `_GSEARCH_RESULT_FILES`) serves found/notFound/report/run.log/output/state from disk or S3; `_upload_file_links` advertises the gsearch (and gmaps) files; `run_detail.js` shows them in a "Files" card on the legacy detail view.
- Supabase terminal update (`_update_supabase_run`) for gsearch (and gmaps) now also writes `websites_found`/`websites_not_found`/`cost`/`token_usage` (was only success/failed counts).

### gmaps pipeline (scrape.do Google Maps) — S3-only, heuristic confidence, built for 500k rows
**No `state.json` and no local disk since 2026-08.** S3 object presence IS the row state, exactly like relationship: `raw/<shard>/row_NNNNNN.json` = the provider's payload verbatim, `rows/<shard>/row_NNNNNN.json` = the row's computed result (the executor's `context` + cost keys), `errors/<shard>/…` = died after every retry. A row is DONE when its **rows/** object exists (raw/ is written first, so a crash between the two just re-scrapes it). `status.json` holds O(1) counters; `_gmaps_runs/<run_id>.json` maps run_id → prefix + Supabase row id. Why it moved: `update_row_state` rewrote the WHOLE state file under a per-upload lock per row, so write cost grew with the SQUARE of the row count — ~8GB of rewrites at the old ~2.7k ceiling, ~275TB at 500k. Orchestration is `gmaps_runner` (ONE RabbitMQ message per RUN on `gmaps_runs`, ack-on-receipt, bounded task window sized by `SCRAPEDO_CONCURRENCY`, `redrive_stale_runs` for durability) over the shared `s3_run_driver`/`s3_run_store`; **two phases only** — scrape, then report — because heuristic confidence needs no LLM pass.

gmaps hits **scrape.do's Google Maps API** (`scrapedo_maps_client.process_gmaps_query` → `GET https://api.scrape.do/plugin/google/maps/search`, needs `SCRAPEDO_TOKEN`) and picks a best candidate via `_select_best_gmaps_website` (unchanged selection logic). It has **output parity with gsearch**, built on the same shared `reporting.py` module — no separate reporting path per pipeline.
- **Migrated off SerpWow 2026-08** (first pipeline to move; `relationship` followed onto scrape.do Google AI Mode, then `firmographics` onto scrape.do Google Search 2026-08-19 — gsearch is the last one on `api.serpwow.com`). SerpWow needed **two** calls per row — `search_type=places` to collect `data_cid`s, then one `place_details` per CID (`1+N`, up to 21 requests/row) — because its `fetch_data_cids` discarded everything but the CID. scrape.do's `local_results[]` carries `website`/`title`/`address`/`phone`/`rating`/`reviews`/`type` **inline**, which is the entire set the pipeline reads, so it's **one request per row** and `/plugin/google/maps/place` is deliberately unused (verified: a website-less `local_result` is website-less in `/maps/place` too, so hydration recovers nothing). `results` holds `local_results` **verbatim** — it is persisted as the row's raw artifact. Result order now survives (`position`), where the old `set()` of CIDs destroyed it and fed `_score_gmaps_candidates`'s `-0.01*idx` tiebreaker noise. The deleted `gmaps_client.py` was `aiohttp`'s only importer, so that dependency is gone too.
- **Billing is credits, not USD**: 10 credits per **successful** call (`CREDITS_PER_CALL`; failed attempts are free, so the 3-attempt retry is cheap). A gmaps row's `cost_breakdown` is `{scrapedo_requests, scrapedo_credits, gemini_cost_usd, total_cost_usd}` — **no `serpwow_*` or `massive_proxy_*` keys at all**, and `CrawlResponse.serpwow_cost_usd`/`massive_proxy_cost_usd` are left `None` ("not applicable" rather than a misleading $0.00; they're `Optional` in the shared schema, and the XLSX columns come out blank for gmaps). `build_summary` **routes on the presence of the `scrapedo_*` keys**: such a row skips SerpWow accounting entirely. That routing is load-bearing, not cosmetic — gmaps rows carry a `formatted_results` entry for their single call, so without it the billable-count fallback would infer 1 billable search and price every row at `SERPWOW_USD_PER_SEARCH` (regression test in `test_gsearch_cost.py`). It's also the back-compat path: a pre-migration gmaps run has no `scrapedo_*` keys, lands in the SerpWow branch, and keeps rendering `serpwow_searches`/`serpwow_usd`. `run.log`'s `# cost:` line appends `scrapedo_credits=` only when non-zero, `run_detail.js` branches on `cost.scrapedo_credits` (data, not pipeline key), and the Slack ping shows "N requests · N credits".
- **`502` is always retried; only a row that dies after every retry is an error.** scrape.do overloads 502 for both "transiently broken" and "Google has no listing", and we can't tell which until the retries are spent — so all 502s get the full backoff (failed attempts are unbilled, so the only cost is latency). Each row's failed attempts are then bucketed by what the ROW became: `scrapedo_recovered_requests` (it succeeded in the end — **never** reported as an error), `scrapedo_error_requests` (failed after every retry — the only real errors), and the remainder (attempts on a no-listing row, expected and free). `run.log` reads `scrapedo_requests=N (ok=… recovered=… no_listing=… errors=…)`, and the UI's failed badge shows `scrapedo_error_requests` only, so a run where every 502 recovered displays zero errors.
- **`502 "no results"` after all retries is a NOT-FOUND, not a failure.** scrape.do overloads HTTP 502: usually transient (`"request failed"`), but the body is `{"error": "no results"}` when **Google has no Maps listing** for the query — 12/100 rows of a real run. Matched via `NO_RESULT_MARKERS`, it returns `results=[]`, `no_results=True`, **no error, no retry** (retrying can never succeed), and the row lands as `not_found` with `context["row_error"] = "No Google Maps listing exists for this company."`. Before this, those rows were `outcome=error`, which inflated the error count, listed them under "View failed rows", and made them eligible for a "Rerun failed" that could only fail again.
- **Retries + call accounting.** There are **no gmaps-specific env keys** — it shares `SCRAPEDO_TOKEN`/`SCRAPEDO_TIMEOUT_SECONDS`/`SCRAPEDO_MAX_RETRIES` with AI Mode as the app's single scrape.do config (so changing a retry/timeout value moves both). `SCRAPEDO_MAX_RETRIES` counts retries after the first attempt: default 2 → 3 calls/row. Backoff is exponential with a **429-specific base**: 1/2/4s for 5xx vs 5/10/20s for rate limits (a 1s/2s ramp exhausted every attempt on 27/50 rows of a live run), `Retry-After` honored, all capped at `MAX_BACKOFF_SECONDS` (30s). Because failures are free, a retry costs only latency. The envelope reports `request_count` (every attempt), `successful_requests` (HTTP 200s — the only billed ones), `failed_requests`, and derives `credits = CREDITS_PER_CALL × successful_requests` rather than counting by hand, so a run always reconciles as **requests == succeeded + failed**.
- **Provider failures are errors, not not-founds.** The client returns error envelopes instead of raising; `run_gmaps_from_module` propagates them, and the executor writes a one-entry `context["formatted_results"]` (the structure `outcomes._phase_stats` reads) tagged `error_source: scrapedo`. So a 429/auth failure classifies as `outcome=error`/`source=scrapedo` — visible in the error breakdown and reachable by "Rerun failed" — instead of silently reporting "no website found" for every row. `_phase_stats` now returns the phase's declared `error_source` so the attribution isn't hardcoded to SerpWow.
- **Heuristic confidence (default, no LLM cost).** `_score_gmaps_candidates` scores each Maps candidate against the input company name/address (`name_match`/`address_match`/`address_conflict`/`organizational_mismatch`); `_gmaps_confidence_for_entry` maps those signals to a 0–100 confidence; `_gmaps_confidence_block` returns `{raw, mode}`, stored in `context["gmaps_confidence"]` (all in `serpwow/gmaps_scoring.py`, re-exported from `engine.py`). `reporting._confidence_raw` reads `context["gmaps_confidence"]["raw"]` for gmaps rows (vs. `final_url_selection_ai`/`gemini_batch_ai` for gsearch), so the shared reporting module produces populated `output_confidence_score`/`output_confidence` for gmaps too.
- **Confidence is heuristic, and that is the only mode.** `GMAPS_CONFIDENCE_MODE` and `GMAPS_LLM_BATCH` were deleted in 2026-08 with the move to the S3-only runner: the batch one routed through the shared gsearch chunk engine, whose chunk state lives in `state.json` — the very thing that capped this pipeline at ~2.7k rows. `_gmaps_confidence_block` runs inside the row task, costs nothing, and is what every verified run actually used.
- **Outputs, Supabase, `/status`, UI**: `gmaps_outputs.write_outputs` streams `rows/` + `input.csv` into `found.csv`/`notFound.csv`/`retry.csv`/`report.json` (**summary only**)/`run.log`, all in S3 under `<company-slug>/gmaps/<run_id>/`. Per-row conversion is the SHARED `reporting.row_to_entity_result`, unchanged — but the CSV **columns** are the uploaded `input.csv`'s own header verbatim, then `RESULT_COLUMNS` (see "Output CSV columns" below), so they differ from gsearch's fixed set. Supabase/Slack fire from `s3_run_driver.notify_terminal`; `/status` is `_gmaps_status` (counters mid-run, `report.json` once `phase == "completed"`), the same JSON shape as before so `run_detail.js` needed no change.
- **No batch machinery at all.** No Gemini batch, no reconciler, no deferred completion, no `batch_pending` — `_batch_postprocess_enabled_for` now knows only gsearch. gmaps is two phases: scrape, then report.

### relationship pipeline (scrape.do Google AI Mode) — S3-only, built for 500k rows
Migrated off SerpWow 2026-08, the second pipeline to move. **One** AI Mode call per row
(`scrapedo_ai_client.search_ai_mode` → `/plugin/google/search/ai-mode`) driven by a single
editable prompt at `prompts/relationship_search.txt`, whose only placeholders are
`{x_name}`, `{y_name}`, `{x_domain}` and `{input_url}` (a test enforces that set — the
file goes through `.format()`, so an unsupported one KeyErrors on every row).
The LLM half is unchanged: `build_relationship_prompt` / `apply_relationship_gate` /
`update_relationship_block` keep their rules, and **every `https://` URL anywhere in the
answer** — prose, `snippet_links[].link`, `references[].link` — is the candidate set the
gate validates against, minus disallowed hosts and Company X's own domain.

**No `state.json` and no local disk.** S3 object presence IS the state
(`raw/<shard>/row_NNNNNN.json` = done, `errors/<shard>/row_NNNNNN.json` = dead,
`cleaned/…` = has a verdict; `NNNNNN` counts from **1**, so input.csv's first data row is
`row_000001` while the index stays 0-based in code — `s3_run_store._idx_from_key`
undoes it). **A scraped row is ONE object holding scrape.do's response body and nothing
else** — the exact bytes off the wire, not a wrapper and not re-serialised JSON, because
it is read by hand to judge the search prompt. There is deliberately no sidecar: the query
and locale come back inside the response's own `search_parameters`, the row index IS the
filename, the CSV fields are in input.csv (which the verdict phase now streams via
`_iter_scraped_rows`, so dropping the sidecar also removed a GET per row from that phase),
credits are `CREDITS_PER_CALL` per billed 200, and the run's attempt total is a counter in
`status.json` — the one figure not recoverable from the objects, which is why
`write_outputs` takes `scrapedo_requests` from the counter. `store.read_row` supplies the
implied counts so all row logic still sees one envelope with the body under `response`.
`modes.relationship.ai_mode_arrays` is the single reader of `text_blocks`/`references`
(with a fallback for pre-split objects), and **`evidence_text` is what the verdict LLM
reads: EVERY string in the stored response, in order, one per line** (2026-08-05). Plain
text, not JSON — the braces were ~25% of the evidence tokens. Nothing is selected by key;
it walks whatever is there, because every rendering of "just the parts we need" lost
something real — first the EVIDENCE bullets (they live under `list`, which carries no
`snippet`), then `snippet_links[].link`, the resolved target of an inline link and, on a
real 100-row run where `references[]` came back empty for all 100 rows, the ONLY link
source scrape.do gave us. `_NON_EVIDENCE_KEYS` is an **exclude** list, the inverse of the
include-list that kept losing content, so a key the provider adds tomorrow survives by
default: it drops only `search_parameters` (our own ~1.2KB prompt echoed back, and where
the `https://example.com` format example and X's portfolio URL live — both would otherwise
be pickable as Y's website) and `type`/`level`/`index`/`reference_indexes` (structural
metadata whose values are block-type names and numbers). The candidate set comes from that
same text, so there is one source of truth for both.
AI Mode also structures the same question differently from call to call — sometimes
headings, sometimes one `ordered_list`, sometimes a single paragraph — which is why nothing
may key off block types. This layout makes the EC2 instance disposable and a re-drive resumes for free. `status.json`
holds O(1) counters only (~2KB at any run size) and is a cache — the phase barrier
re-LISTs. Resume is ONE paginated LIST building an in-memory index set, not 500k HEADs.
Writes go through `s3_run_store.put_object`, which **raises** — unlike
`s3_sync.mirror_file_to_s3`, because with S3 as the only copy a swallowed PUT loses work.

**One RabbitMQ message per RUN** (`relationship_runs`, own queue and channel), body
`{"run_id": …}`; concurrency comes from a bounded task window inside the consumer, sized
from **`SCRAPEDO_CONCURRENCY`** (the per-account vendor cap it shares with gmaps and AI
Mode) — this pipeline has no concurrency knob of its own, because a second setting could
only disagree with the semaphore every call already passes through. `WORKER_CONCURRENCY`
is irrelevant here: it sets RabbitMQ prefetch, and there is one message per run. The consumer
**acks on receipt** — the repo's only inversion of ack-after-persist — because RabbitMQ's
30-minute `consumer_timeout` would tear down a multi-hour run. Durability comes from
`redrive_stale_runs` instead, which also covers a worker that was down at publish time or
an instance that was replaced. One message means no barrier machinery: phase 2 is simply
the next statement after phase 1.

**In-flight Gemini shards are remembered.** A Batch job runs on Google's side and is billed
whether or not we are still polling, so each shard's job name is written to
`<run>/batches/<first>-<last>.json` before the first poll and cleared only once its
verdicts are durable in `cleaned/`. `_reattach_batches` runs at the top of the verdict
phase and re-polls any surviving record, so a timeout, worker restart or crash re-attaches
to the job already paid for instead of submitting an identical one. The wait is bounded by
**`RELATIONSHIP_BATCH_TIMEOUT_SEC`** (48h = Gemini's job expiry) — deliberately its own key,
not the shared `GEMINI_BATCH_TIMEOUT_SEC`, which bounds gsearch's per-row batches and is
1800 in real deployments.

Billing is credits only, `10 × HTTP-200`, same keys as gmaps so `run_detail.js` renders it
unchanged. `report.json` is **summary only** — no per-row array, which at 500k would be
gigabytes.

### Concurrency model — worker slots vs provider caps (2026-08)
Two **kinds** of dial, deliberately not one — but in practice you set only the first:
- **`WORKER_CONCURRENCY`** = worker **slots** (RabbitMQ prefetch + consumer count) = messages in flight. Shared by all SerpWow-engine pipelines (one queue), and **`AI_MODE_WORKER_CONCURRENCY` falls back to it when unset**, so one number configures both fleets. Set the AI Mode one only to make it differ (its messages are ~50s batches vs a ~3.5s gmaps row). Needs a worker restart — consumers are built at startup.
- **Per-provider caps** = concurrent calls to a vendor, enforced by a semaphore so raising the slot count can never breach a vendor limit: **SerpWow** → `serpwow_client.search_fetch_semaphore` (`SEARCH_FETCH_CONCURRENCY`, set in `startup_event`); **scrape.do** → `common/provider_limits.scrapedo_slot()` (`SCRAPEDO_CONCURRENCY`).

**Per-row latency fixes (2026-08-04, from a 100-row live run):** a shared pooled `httpx.AsyncClient` in `scrapedo_maps_client` — a fresh client per row cost ~400ms of DNS/TCP/TLS setup per call (636ms → 233ms measured) and forfeited keep-alive, i.e. ~55 hours across 500k rows; keepalive is sized to `SCRAPEDO_CONCURRENCY` so every slot holds a warm connection, and it's closed in `shutdown_event`. The `state.json` S3 mirror is **throttled** to `SERPWOW_S3_STATE_FLUSH_SEC` (5s) because it was re-PUT to the SAME key twice per row and grows with the run (~84MB of near-identical copies per 100-row run, saturating uplink and hot-spotting one key — the documented `SlowDown` cause); local disk is still written every time and **terminal snapshots always mirror**, so a cold-start resume never reads a stale run. boto3 retries are `adaptive` (AWS's client-side rate limiter, their documented answer to `SlowDown`) in both S3 clients.

**What that run proved about the provider — and what re-measurement changed (2026-08-25):** on 2026-08-04, `WORKER_CONCURRENCY=100` put 100 rows at 134s / ~0.75 rows/s while 25 did 70.5s / 1.42 — scrape.do *queues* rather than 429ing, so client concurrency above its real capacity became latency. **That number is obsolete.** Three completed 100-row runs at concurrency 100 (`8ffe96d9` 44s, `1a46cba0` 36s, `64b81c68` **34s / 2.94 rows/s**) are 4x the old 100 figure and 2x the best 25 ever measured; the A/B predated the pooled `httpx.AsyncClient`, which is the likeliest cause. 500k ≈ 47 hours at today's rate. Every run still shows **1 attempt per row, zero 429s**. What has NOT been re-measured is 25 — all three fast runs are at 100 — so "100 beats 25 today" is unproven; only "100 got much faster" is. `SCRAPEDO_TIMEOUT_SECONDS=90` is still the ceiling that turns a slow row into a failed one.

`scrapedo_slot()` is **one gate shared by gmaps AND AI Mode** — scrape.do's cap is per *account* and both run in the same worker process, so two separate caps could sum past it. It stays even when you intend to run one pipeline at a time, because that isn't fully manual: AI Mode's periodic reconciler re-dispatches finish tasks and republishes missing batches on its own, and the resume endpoint can start work you didn't. The cap turns "only run one at a time" from a rule someone has to remember into an enforced invariant. Note the provider cap deliberately does **not** track `WORKER_CONCURRENCY` — if it did, raising slots would silently raise the vendor limit and defeat the point. gmaps wraps its HTTP call; AI Mode wraps its `to_thread(scrape_batch_sync)`. It's a concurrency cap only: if scrape.do also enforces requests/second, a token bucket goes **inside** that helper and no call site changes.

Why per-row cost matters here — and this now applies to **gsearch alone**, the last state-driven pipeline: `update_row_state` rewrites the **entire** `state.json` per row under a per-upload lock, so throughput is capped by state size regardless of concurrency. Rows therefore must not carry provider payloads — gmaps and gsearch persist their context **without** `raw_response` (it's already in `serpwow_response/`), which took a row from 39.2 KB to 2.2 KB and the ceiling at 1k rows from 4.3 to ~76 rows/s. That's enough for 100 concurrency up to roughly **2.7k rows**; beyond that the per-upload lock is the limit again and the fix is per-row files + O(1) counters (AI Mode's model). `_write_json` is atomic (temp + `os.replace`) and runs off the event loop.

### firmographics pipeline (scrape.do Google Search) — S3-only, built for 500k rows
**No `state.json` and no local disk since 2026-08-20** — the third pipeline to move, and the
migration removed a hard ~2.7k-row ceiling: `update_row_state` rewrote the WHOLE state file
under a per-upload lock on every row, so bytes written grew with the SQUARE of the row count
(~7.6GB at 2.7k rows, ~262TB at 500k). Measured after the move: **2.01 S3 writes and ~881
bytes per row, flat at any run size.**

S3 object presence IS the row state, three objects with three meanings:
`raw/<shard>/row_NNNNNN.json` = the provider's SERP verbatim; `rows/…` = OUR result + its
cost, and the **phase-1 DONE marker** (written after `raw/`, so a crash between the two just
re-scrapes); `cleaned/…` = the six fields when a Gemini **batch** produced them (absent in
inline mode); `pending_llm/…` = a tiny marker meaning "this row's LLM work was deferred".
That last one exists so **phase 2's pending set is ONE list, not a GET per row** — it used to
open every `rows/` object to ask "was this deferred?", which is 500k round-trips inside a
phase that does nothing at all in inline mode (`test_firmographics_runner` pins it).

Orchestration is `firmographics_runner` (ONE RabbitMQ message per RUN on
`firmographics_runs`, ack-on-receipt, bounded task window from `SCRAPEDO_CONCURRENCY`,
`redrive_stale_runs` for durability) over the shared `s3_run_driver`/`s3_run_store` — the
generic half is 100% shared with gmaps and relationship, so the runner is ~380 lines.
**Three phases**: scrape → LLM → outputs, and the LLM phase runs unconditionally rather than
being branched around by the mode flag, because which mode a run used has to be recoverable
from the objects, not from current env.

- **Outputs** (`firmographics_outputs.write_outputs`, memory O(one row), spooled temp files):
  **`enriched.csv` / `notEnriched.csv`** — not found/notFound, because this pipeline is HANDED
  the website and a found/not-found split would name a discovery result it never computed —
  plus `retry.csv`, `report.json` (**summary only**) and `run.log`, all under
  `<company-slug>/firmographics/<run_id>/`. Columns are the uploaded header **verbatim**, then
  `row_index, outcome, address, phone, email, industry, products, services, summary,
  enrichment_note, gemini_cost_usd, total_cost_usd, processing_seconds,
  raw_response_s3_key` — per-ROW facts only. The old 37-column export also repeated NINE
  run-level values on every row (`upload_id`/`file_status`/`created_at`/`total_rows`/…),
  which `report.json` states once; its six `input_*` columns are superseded by the uploaded
  header (every column the user sent, under their own names); and its confidence /
  `descirption_ai` / `massive_proxy` columns were always blank for this pipeline.
  `raw_response_s3_key` is emitted only when a `raw/` object can exist — blank for a row
  with no website (no call was made) and for one that died before returning a body, because
  pointing at a missing key is worse than an empty cell. `enrichment_note` is empty on an
  enriched row by design; it is the reason column for `notEnriched.csv`. `retry.csv`'s membership rule is the SHARED `reporting.retry_row`, so the
  three pipelines' rerun lists speak one vocabulary; a row with no `website_url` is excluded
  (not rerunnable — the INPUT is what is missing).
- **`/uploads/{id}/output` and its CSV form 404 for this pipeline now** — there is no
  state.json to build them from. `run_detail.js` knows via `NO_STATE_PIPELINES` and
  advertises the result files instead.
- Upload validates every row and retains **none** (`csv_input.count_firmographics_csv_rows`),
  writes `input.csv` to S3 and publishes one message; 400s early on a missing
  `SCRAPEDO_TOKEN`/`GEMINI_API_KEY`/`S3_BUCKET` rather than burning a run.
- `row_fields` resolves headers through `csv_input.firmographics_columns` — the SAME alias
  table the upload validates against, so a header accepted at upload can never be one the
  worker fails to find.

#### Provider + cost (unchanged by the S3 move) — credits only, two priced endpoints
Migrated off SerpWow **2026-08-19**, the last of the three. Still state-driven (per-row
RabbitMQ messages, `state.json`) — only the provider changed, so the ~2.7k-row state
ceiling from the concurrency section still applies to it.

**Two endpoints, priced differently, because Google does not always have the AI Overview
ready when it serves the SERP** (`scrapedo_search_client.py`):
1. `GET /plugin/google/search?q=<question>&hl=en&gl=<cc>` — **10 credits** per HTTP 200.
   Carries `ai_overview` inline when `ai_overview.state == "complete"`, plus
   `knowledge_graph`/`local_results`/`organic_results` that the SerpWow call discarded.
2. `GET /plugin/google/search/ai-overview?session_key=…` — **5 credits** per HTTP 200,
   fired ONLY when step 1 answered `state == "deferred"`. The key is **single-use with a
   60s expiry**, so this call is deliberately **never retried**: a second attempt gets
   `404 session not found` and could only add latency to a row that already has its SERP.
   `state` can also be absent — Google produced no overview at all, which is a billed
   search with nothing to extract (`billed_no_overview`), not a failure.

The inline `ai_overview` on a deferred row is a **stub** (`{state, session_key}`, no
content) and is discarded before the follow-up: leaving it in place made
`billed_no_overview` false and would have fed the stub to the LLM as if it were an
overview. The `deferred` flag is recorded independently of the final state, or a follow-up
that succeeds rewrites `state` to `"complete"` and hides the 5 credits it cost.

The query is still `codetails.build_query`'s wording, now `modes.common.build_firmographics_query`,
so migrated runs stay comparable with the SerpWow ones. It is built from the **domain**, so
`https://grupoltn.com/acerolatina` asks about `grupoltn.com` and a sub-brand path enriches
its parent group — a known limitation, unchanged by the migration.

- **Billing is credits, no provider USD.** A row's `cost_breakdown` carries
  `scrapedo_requests`/`scrapedo_successful_requests`/`scrapedo_failed_requests`/`scrapedo_credits`
  plus a **per-endpoint split** — `scrapedo_search_requests|successful`,
  `scrapedo_ai_overview_requests|successful`, `scrapedo_ai_overview_deferred` — because 10
  and 5 credits are not interchangeable and the totals alone cannot explain a bill.
  `CrawlResponse.serpwow_cost_usd`/`massive_proxy_cost_usd` are left **`None`** ("not
  applicable", the gmaps convention), and `total_cost_usd == gemini_cost_usd`: **the only
  USD this pipeline spends is the LLM normalisation call.** `build_summary` routes on the
  presence of the `scrapedo_*` keys, so it skips SerpWow accounting with no pipeline check.
- **`COST_SUMMARY_PIPELINES`** (`constants.py`) = `REPORTING_PIPELINES | {firmographics}`.
  firmographics needs the billing card, Supabase cost fields and Slack credits, but NOT
  found.csv/notFound.csv — it is handed the website, so a found/not-found split would be
  a discovery result it never computed. Used at the `/status`, Supabase and Slack sites;
  the file-writing site still gates on `REPORTING_PIPELINES`.
- **`official_website` is an INPUT here, so it cannot mean "found"** (fixed 2026-08-19).
  `classify_finalized_row` has a firmographics branch: provider error ⇒ `error`, no AI
  overview or no extracted fields ⇒ `not_found`, fields extracted ⇒ `found`. Before this
  EVERY row was `found` — including rows whose provider call failed outright, which
  reported success at zero cost. The no-explicit-outcome fallback is the shared
  `reporting.row_produced_a_result(result, pipeline)`, called from BOTH
  `_derive_outcome` and `engine.summarize_upload_state._outcome_of` so report.json and
  `/status` cannot disagree. firmographics also joined the 3-way `row_status` remap, so a
  website with no overview is `completed`/`not_found` instead of a `failed` row that
  "Rerun failed" would re-buy at 10 credits.
- **Gemini batch mode**: gated by the one global `LLM_BATCH` via
  `common/llm_batch.batch_enabled` — see the batch-config section above. In batch mode the executor **skips its inline Gemini call**
  (doing both would bill every row twice) and marks
  `context.mapping_ai.deferred_to_batch`. Three branches in the shared batch engine:
  `_build_batch_prompt_for_row` (the firmographics normalisation prompt),
  `_build_batch_items_for_state` (**skips rows with no AI overview** — a paid request over a
  missing overview can only come back empty) and `_apply_batch_parsed_to_row` (outcome from
  the extracted fields, never from the echoed `official_website` — D43 again). Both
  transports send a **byte-identical** prompt via `gemini_llm.build_ai_overview_prompt`, so
  the answer does not depend on the mode. Tests: `test_firmographics_batch.py`.
- **Header chips**: `confidence_mode` is `None`, which used to hide that a paid model ran
  at all. `build_summary` now also emits **`llm_mode`** (`"inline"`|`"batch"`, so the UI states which one ran) and
  **`phase_seconds_avg`** `{provider, llm}`; `run_detail.js` renders `LLM: Inline` +
  `Model: <name>` (an `else if (g?.llm_mode)` branch, for a pipeline whose LLM does
  something other than confidence) and `scrape.do/row` + `LLM/row`. The times are per-row
  **averages, labelled `/row`** — rows run concurrently here, so a sum would exceed the
  run's own wall clock and read as if the run took hours. The key is emitted only when rows
  carried the split, so no other pipeline's summary grows it. The split itself is measured
  in the executor (`context.timing.search_seconds`/`llm_seconds`), and the worker now
  **merges** `total_seconds` into that dict instead of replacing it.
- **UI**: `run_detail.js`'s `emptyResponsesSection` gained a firmographics branch (keyed on
  `empty_response_breakdown.no_ai_overview`, i.e. the DATA, not the pipeline) showing
  billed search calls, search credits, AI-Overview follow-ups and their credits, deferred
  rows, billed-but-no-overview, failed-after-retries and unbilled attempts. The cost card
  takes the existing Scrape.do branch (credits, `providerCostKey: null`).
  `confidence_mode` is **`None`** for firmographics — it is given the website, so there is
  nothing to be confident about and the UI skips the chip; `model` is still set, so the
  card keeps its LLM line.
- Concurrency/retries: no keys of its own. Shares `SCRAPEDO_TOKEN`/`SCRAPEDO_TIMEOUT_SECONDS`/
  `SCRAPEDO_MAX_RETRIES` and the per-account `provider_limits.scrapedo_slot()` gate with
  gmaps and AI Mode, and holds its own pooled `httpx.AsyncClient` (closed in
  `shutdown_event`). `SEARCH_FETCH_CONCURRENCY` no longer applies to it.
- **No SerpWow left on this path** (2026-08-19): `codetails.py` deleted,
  `standardize_serpwow_ai_overview_with_gemini` → `standardize_ai_overview_with_gemini`
  (firmographics is its only caller; its prompt now says "Google AI Overview"), and the
  per-row raw object moved from `serpwow_response/…_serpwow.json` to
  **`search_response/…_search.json`** via `engine._raw_artifact_names(pipeline)` — a
  firmographics row holds a scrape.do SERP, so the old name mislabelled every one of them.
  gsearch keeps `serpwow_response/` so its pre- and post-change runs stay in one folder.
  `upload_serpwow_json_to_s3`/`_upload_serpwow_json_sync` are now
  `upload_raw_response_to_s3`/`_upload_raw_response_sync` (shared by both pipelines, so
  naming them after one provider was the misnomer). Still SerpWow-named on purpose: the
  `services/serpwow/` **package path**, the shared `reporting` module, and the
  `CrawlResponse.serpwow_cost_usd` field — cross-pipeline schema, and the package rename is
  the agreed last step. **`s3_html_key` + `s3_serpwow_json_key` are gone** (2026-08-19):
  they were two CSV columns holding the SAME value under two wrong names — nothing ever
  contained HTML, and the SerpWow one carried a scrape.do payload for every migrated
  pipeline — now one **`raw_response_s3_key`**, read via `engine._raw_response_key(row)`
  which falls back to both legacy names so pre-rename runs stay readable.
- Tests: `test_firmographics_scrapedo.py` (client + credit math + executor, all via
  `httpx.MockTransport`); `test_gsearch_s3_slug.RawArtifactNamingTests` pins the two
  artifact-folder names.

### Gemini batch config — ONE resolver for all four pipelines (`common/llm_batch.py`, 2026-08-19)
All four batching pipelines (`ai_bulk`/`ai_deep`, `gsearch`, `firmographics`, `relationship`)
already shared ONE driver — `ai_mode/gemini_batch.py`. Only the config around it had drifted,
so `common/llm_batch.py` now owns every batch setting and each call site reads it there.

- **`LLM_BATCH` is the ONLY toggle** (2026-08-20). `AI_MODE_LLM_BATCH` / `GSEARCH_LLM_BATCH` /
  `FIRMOGRAPHICS_LLM_BATCH` are **deleted**, not deprecated: `_TOGGLEABLE` is now a plain set
  of the four pipelines the global moves, and a leftover override line in someone's `.env` is
  inert (`test_deleted_per_pipeline_toggles_have_no_effect`). They were dropped because the
  granularity was unused while the tri-state cost a second place to look when a run came out
  in the wrong mode — the global only *appeared* to work because all three shipped blank.
  (The consequence that `LLM_BATCH=true` forced AI Mode's provider is **moot as of
  2026-08-20**: Gemini is the only provider — see below.) `_flag`'s blank-counts-as-unset
  tri-state stays — `.env.example`
  ships `NAME=` and `env.get_bool_env` falls back only on an ABSENT variable.
- **`batch_enabled(pipeline)` and `uses_shared_row_batch(pipeline)` are different questions.**
  relationship batches (its Gemini call IS the verdict — no inline path exists, so it has no
  toggle) but through its OWN driver in `relationship_runner`; `engine`'s gate
  (`_batch_postprocess_enabled_for`) must therefore say **no** for it, or a second duplicate
  job gets seeded for the same run. Conflating the two broke exactly that, caught by
  `test_gsearch_batch_gate`. gmaps has no LLM and answers False to both.
- **One key per mechanical knob**, and the old duplicates are **deleted**:
  `GSEARCH_GEMINI_CHUNK_SIZE` → `GEMINI_BATCH_SHARD_SIZE` (5000),
  `GSEARCH_GEMINI_MAX_INFLIGHT` → `GEMINI_BATCH_MAX_INFLIGHT` (5),
  `RELATIONSHIP_BATCH_TIMEOUT_SEC` + `AI_MODE_BATCH_TIMEOUT_SEC` → `GEMINI_BATCH_TIMEOUT_SEC`
  (**172800 = 48h, Gemini's job expiry** — a Batch job runs and bills on Google's side whether
  or not we poll, so the old 1800 abandoned work already paid for), `AI_MODE_BATCH_POLL_SEC` →
  `GEMINI_BATCH_POLL_SEC`. Closes HANDOFF blocker #1.
- **Retry is MANUAL, button-only** (user's call, 2026-08-25) — `redrivable()` is unchanged,
  so `completed` stays terminal to the re-drive scan and no run ever re-spends Gemini tokens
  unattended. What that requires in exchange: a row whose scrape succeeded but whose Gemini
  shard died is named `llm_incomplete` everywhere — its own `empty_response_breakdown` bucket
  and billing chip (NOT folded into `no_ai_overview`, which it is not), its own
  `enrichment_note` ("LLM never completed — rerun to retry (no scrape.do re-spend)."), its own
  `reporting.retry_row` reason so it reaches `retry.csv`, and a **Rerun button on the run
  detail page** for all three S3-only pipelines instead of hand-typing the id into Operations.
  The discriminator is object presence: `cleaned/` **missing** = the shard died, retryable;
  `cleaned/` present with a null result = Gemini answered and had nothing, final. A callout
  renders `task_errors`, so `completed_with_errors` always has a visible cause.
- **A shard Google never answered leaves its rows RETRYABLE** (2026-08-25). `is_terminal()`
  is true for SUCCEEDED/FAILED/CANCELLED/EXPIRED alike — it means "stop polling", not "there
  are results". `is_success()` now short-circuits on `gemini_batch.FAILED_STATES` instead of
  falling through to `done_flag and not error`, which an expired job satisfies. Both S3-only
  runners then call `_abort_if_shard_died`: drop the `batches/` record (else the next drive
  re-polls a corpse rather than buying a fresh shard) and **raise before writing anything**,
  so the rows keep their `pending_llm/` marker and only they are resubmitted. Recovery is
  "Rerun failed" → scrape phase skips every row with an object (**0 scrape.do credits**) →
  LLM phase resubmits the pending rows. No extra artifact is needed for this: `raw/`,
  `rows/`, `pending_llm/` and `batches/` already hold everything a retry reads. `engine`'s
  private `_failed_states` copy is deleted — one rule, three callers.
- **`GEMINI_BATCH_TIMEOUT_SEC` bounds ONE SHARD's poll, not the run and not the LLM phase.**
  The deadline is built inside `_poll_to_terminal`, which runs in phase 2 — so a scrape phase
  of any length (hours, days) cannot consume it, and 20 sequential waves of shards can far
  exceed 48h in total because each wave starts its own clock. Asked twice, so it is pinned by
  `test_batch_timeout_scope.py` (including the companion that a shard which genuinely overruns
  still raises, or the first test passes vacuously). Never hoist that deadline to run start.
- **Model: `GEMINI_BATCH_MODEL` → `GEMINI_MODEL` → `gemini-2.5-flash-lite`.** The second step
  is load-bearing: `relationship_runner` read only `GEMINI_BATCH_MODEL`, so setting just
  `GEMINI_MODEL` moved three pipelines and left relationship on the hardcoded default.
  `test_llm_batch_config` guards the invariant by **scanning every pipeline module for a direct
  env read** of any batch key — verified to fail when the bug is reintroduced.
- **Worker executor is sized** (`start_worker_consumers`): `max(32, max_inflight + 16)` via
  `loop.set_default_executor`. HANDOFF blocker #4, and it is **relationship-only** —
  `engine`'s chunk driver `await`s `asyncio.sleep` between polls and AI Mode polls all shards
  from one thread, while `relationship_runner._poll_to_terminal` blocks a pool thread per
  in-flight shard for hours. Headroom matters because that executor is shared with every S3
  write and CSV parse in the worker.
- **Gemini is the only LLM provider, and there is no `provider` field anywhere** (2026-08-20).
  Deleted: `AI_MODE_LLM_PROVIDER`, the whole `OPENAI_*` family (key/model/base_url/both
  pricing rates), `llm_client.OpenAICompatibleClient` + `parse_usage`, the dead
  `settings.load_llm_config` and its `LLM_PROVIDER`/`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`
  keys, `cost.calculate_llm_cost_usd`'s `provider` argument, **`LLMConfig.provider`** and the
  `llm_provider` key it wrote into `status.json` (nothing read it — not `run_detail.js`, not
  the DOM contracts). `DEFAULT_LLM_BASE_URLS` collapsed to the constant
  `settings.GEMINI_BASE_URL`, which is now `LLMConfig.base_url`'s default.
  **`LLMConfig` is `{api_key, model, base_url=GEMINI_BASE_URL, max_retries, timeout_seconds}`
  and the ONLY thing env chooses is the model** — `llm_batch.batch_model()` in batch mode
  (`GEMINI_BATCH_MODEL` → `GEMINI_MODEL` → default), `GEMINI_MODEL` inline. `final_report.json`'s
  `llm` block is now `{base_url, model}`. `make_llm_client` stays despite having no branch
  left: it adapts `LLMConfig` to the client kwargs and is the seam every offline AI-Mode test
  patches. `LLM_MAX_RETRIES` / `LLM_TIMEOUT_SECONDS` are transport settings and stay.

### Shared layers
- **Canonical CSV input** (`models/entities.py`, `parse_entities_csv`): requires a company-name column (aliases `company_name|company|name|entity_name|entity|organization|organisation|legal_name`) **and** a country column (`country|country_name|nation`); optional `company_local_name|address|firm_id|industry`. Headerless 2+-col files parse positionally. The **old `Company Name ENG`/`Country Code`/`ISIC` format is rejected with 400** (no auto-detect). `InvalidCSVError` → 400.
- **Output CSV columns — the input file plus what we worked out** (2026-08-11). `found.csv`/`notFound.csv` (and relationship's two, and `retry.csv`) carry **every column of the uploaded CSV, verbatim and in original order**, then `reporting.RESULT_COLUMNS` = `website_url, confidence, flags, attempt_log` (+ `error` on the not-found file). The rule is one pair of helpers in `common/text.py` — `passthrough_fieldnames` (collision-safe: an input column named `website_url` becomes `website_url__orig`, the computed one keeps the plain name; `error` is reserved for BOTH files so their headers stay parallel; an optional `source_overrides` undoes `s3_run_store.iter_input_rows`' injected `row_index`) and `passthrough_row`. It lives there, NOT in `reporting`, because `ai_mode/run_reporting.py` is standalone-by-contract and `reporting` imports `serpwow_client`→`httpx`. **AI Mode's writer owns a forward-only cursor** over its `input.csv` (`StreamingRunReport(run_dir, company_column)`), pulling one input row per `EntityResult` — alignment is an invariant of the writer, not of the caller — and it **replays `parse_entities_csv`'s skip-a-blank-company-name rule**, which spends no `sno`, or every row after the first blank one gets the previous row's cells. Never joins on `EntityResult.sno` (it takes the LLM's echoed value when present). Falls back to the fixed `CSV_COLUMNS` for headerless/positional input, an unreadable input.csv, or the classic `write_outputs()` entry point. **gsearch keeps the fixed `CSV_COLUMNS`** — `csv_input.parse_csv_rows` keeps 6 mapped keys, the upload bytes are dropped at the end of the request, and nothing carries the original record into `state.json`, so passthrough there needs an `input.csv` write at upload first.
- **Firmographics input CSV** (`csv_input.parse_firmographics_csv_rows`): requires **`website_url`** (aliases `official_website|website|url|domain`); optional `company_name` (same alias list as `parse_entities_csv`, so a `found.csv` round-trips with its names). **No column ⇒ empty cell** — the domain fallback was deleted 2026-08-18 from both the parser and the executor, since `acme.com` rendered as if it were a real company name and reached the LLM prompt as one, `country`, `firm_id`, `industry`, `full_address`. Header resolution is ONE table, `csv_input.firmographics_columns()`, shared with the New Run preview. The parsed row's key and the state field are still `official_website` — only the user-facing column was renamed (2026-08-18). **Preview**: `POST /uploads/firmographics/preview`, its own endpoint like relationship's, because the shared `/uploads/preview` runs `parse_entities_csv` (company_name + country required) and reported firmographics CSVs as headerless/positional.
- **Per-row output export — CSV is the advertised format** (2026-08-18). `GET /uploads/{id}/output?format=csv|xlsx|json`. XLSX and CSV are built from ONE table (`output_export.build_upload_output_table`), so the 37 columns cannot drift between them; `build_upload_output_csv_bytes` writes **`utf-8-sig` (BOM) + CRLF** because Excel decodes a BOM-less CSV as the OS legacy codepage and turns `Leśna` into `LeÅ›na` — the BOM is the whole fix, and `csv`/`pandas` skip it. The CSV also passes `ensure_ascii=False` for the `output_json` column and **does not** apply the XLSX writer's 32767-char cell cap (a CSV has no such limit; silently losing the tail of `output_json` is worse than a long field), while still stripping C0 control chars. `format=xlsx` still works for old links, it just isn't linked from the UI (`run_detail.js` Files card, `operations.js` history column). Tests: `test_output_csv.py`.
- **Per-entity result schema** (`models/results.py`): `EntityResult{website_url, confidence(0–100), flags[{flag,why}], attempt_log[{query,result,url}], error}`. `flags_csv()`/`attempt_log_csv()` join with `"\n"`, so each flag/attempt is its own line **inside one quoted CSV cell** (a real embedded newline — Excel renders multi-line; it does not break the row).
- **S3 layout** — bucket `S3_BUCKET` (currently `website-url-finder`, region `S3_REGION`=`ap-south-1`), keyed **`<company>/<pipeline>/<run_id|upload_id>/…`**. AI Mode uses the shared helper `core/s3.py`. It **write-throughs each file as it's produced** (`s3_sync.mirror_file_to_s3` — `input.csv`/`status.json` at start, each `raw_responses/` scrape and `cleaned/` batch as it lands; mirrors SerpWow's per-row uploads so a hard kill is still resumable), and still runs an end-of-run `s3_sync.mirror_run_to_s3` (`upload_directory`) as a backstop for the aggregates (`final_report.json`/`found`/`notFound`). All S3 writes are **best-effort, never fail the run**. Resume can `rehydrate_run_from_s3` if the local dir is gone. SerpWow keeps its *own* S3 client inside `engine.py` (`_upload_s3_prefix`, `write_upload_artifact`). The new bucket has no shared top-level prefix, so cold-start scanners paginate the whole bucket. **The company-folder segment is now unified across SerpWow + AI Mode**: SerpWow uses `_company_slug` (lowercase, non-alphanumeric→`-`, matches AI Mode's `slugify_company` → `isi-market-test`); `_safe_name` (underscores, keeps case) is retained **only for the per-row filename** segment, not the company folder. (Old `ISI_Market_Test/`-slug runs predate this and weren't migrated; `_find_upload_dir`/`_find_s3_upload_key_sync` still find them by `upload_id` suffix.) **Per-row raw SerpWow responses live under a `serpwow_response/` subfolder** of the run dir, named `{row_index:06d}_{_safe_name(row_company)}_serpwow.json` (6-digit index + the ROW company, so each is identifiable even though the run folder uses the UPLOAD company); the run aggregates (`found.csv`/`notFound.csv`/`report.json`/`run.log`/`output.json`/`state.json`) sit at the run root.
- **Supabase tracking** (`core/supabase_client.py`, `services/companies.py`, `supabase/migrations/`): `companies` + `runs` tables. `SUPABASE_URL` must be the **bare** `https://<ref>.supabase.co` (not the `:5432` postgres string); `SUPABASE_SERVICE_ROLE_KEY` is the long JWT. Unconfigured → company/AI-upload endpoints return **503**; SerpWow run writes are best-effort and never fail a run. `get_supabase()` is a **thread-safe lazy singleton** (lock + double-checked locking; flips its `_attempted` flag only after the client is built) — the sync dashboard endpoints run on parallel threadpool threads, and an earlier race returned a half-built `None` and 503'd intermittently until reload.
- **Slack notifications** (`core/notify.py`): best-effort run **completion/failure** pings to `SLACK_WEBHOOK_URL` (Slack Block Kit; no-op when unset, swallows all errors, never raises — same shape as `s3_sync.py`). Both pipelines: AI Mode calls `notify_run_complete`/`notify_run_failed` in `run_ai_mode_finish`/`_fail_run`; SerpWow calls `_notify_slack_terminal` from `persist_upload_state`, guarded by a once-per-terminal-snapshot `slack_notified_marker` (so it doesn't fire per row, and fires even when Supabase is off). `notify_run_complete` takes `search_label` so each engine names its search unit (AI Mode → "Scrape.do searches"); fields are dropped when not passed, so each run type shows only its own detail.
- **UI** (`static/js/`, `templates/index.html`): vanilla ES-module SPA with a hash router (`main.js`). Each view module exports `render(root, params)` returning an optional cleanup fn. `api.js` has `api()` (fetch + one retry), `pollStatus()` (2s until terminal), and `el(tag, attrs, …children)`.

## Disk layout of run outputs
- AI Mode: `ai_mode_results/<company_slug>/<run_id>/` — `input.csv`, `status.json`, `final_report.json`, `found.csv`, `notFound.csv`, `run.log`, `raw_responses/request_NNNNNN.json` (6-digit), `cleaned/batch-NNNNNN.json` (one per successfully-cleaned batch; enables the "Rerun failed" resume). Legacy read-only dir: `ai_mode_result/` (singular).
- SerpWow: local `/tmp/single_ra_isi/<company>/<upload_id>/` (`state.json`, `output.json`, batch files; **gsearch also writes `found.csv`/`notFound.csv`/`report.json`/`run.log`** at terminal status). Both a legacy flat (`<upload_id>/`) and nested (`<company>/<upload_id>/`) on-disk layout must be tolerated.

## Conventions & gotchas
- **Always use ponytail for coding.** The `ponytail` plugin/skill is the required mode for all coding in this repo (writing, refactoring, reviewing, choosing dependencies): laziest solution that actually works — YAGNI, reuse existing code, stdlib/native before new deps, shortest correct diff — without ever shortcutting understanding the problem first.
- **Never add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits.** Tell any committing subagent the same.
- **Never `git push` and never query the production Supabase DB without explicit user approval.**
- `docs/` is **gitignored** — specs/plans/handoff there are on-disk only; code under `backend/` commits normally.
- `el()` sets every attr via `setAttribute`, so `el("a", {href: undefined})` literally sets `href="undefined"`. Use the spread pattern: `...(cond ? {href: x} : {})`.
- SerpWow batch post-processing is gated by `_batch_postprocess_enabled_for(pipeline)` → `llm_batch.uses_shared_row_batch`: `gsearch` and `firmographics` when the one global `LLM_BATCH` is on, all others off — including `relationship`, which since its 2026-08 migration runs its own Gemini Batch verdict phase in `relationship_runner.py` and never routes through this helper, and `gmaps`, which has no LLM at all. The row-pending mark (`process_upload_job`) also routes through this helper, so only those two keep no-URL rows pending for the batch. `_build_batch_prompt_for_row` reads `context["candidates"]` (which gsearch and gmaps both set) additively through its dedup.
- **Two different provider billing models.** scrape.do's **AI Mode** endpoint (AI Mode pipeline) is treated as a flat fee — runs report a `scrapedo_searches` count, no USD. scrape.do's **Google Maps** endpoint (gmaps) and its **AI Mode search** endpoint (relationship, since its 2026-08 migration) both bill **10 credits per successful (HTTP-200) call**, reported as `scrapedo_requests`/`scrapedo_credits`, also no USD. SerpWow (gsearch only) is per-request billed: `serpwow_usd = serpwow_billable_searches × SERPWOW_USD_PER_SEARCH`; `cost.total_usd = llm_usd + serpwow_usd`. `report.json.summary.cost` carries all of them — `{llm_usd, serpwow_searches, serpwow_billable_searches, serpwow_usd, scrapedo_requests, scrapedo_credits, total_usd}` — so a pipeline that has migrated and one that hasn't both render from their own keys.
- The full env reference lives in `.env.example` (sections for SerpWow — incl. `ENABLE_FINAL_URL_GEMINI` and the one global `LLM_BATCH` — AI Mode batch/cleanup, scraping, cost, the AI-Mode broker engine (§3f: `AI_MODE_QUEUE`/`AI_MODE_WORKER_CONCURRENCY`/`AI_MODE_JOB_TIMEOUT_SEC`/`AI_MODE_STATUS_FLUSH_SEC`/`AI_MODE_BATCH_STALE_TIMEOUT_SEC`/`AI_MODE_BATCH_MAX_REQUEUE`/`AI_MODE_RECONCILE_SCAN_LIMIT`/`AI_MODE_LEGACY_STALE_SEC`/`AI_MODE_REPORT_ENTITIES_MAX`), Supabase, S3, and Slack — `SLACK_WEBHOOK_URL`). `SCRAPEDO_CONCURRENCY` is **live and load-bearing**: it is the per-ACCOUNT scrape.do semaphore (`common/provider_limits.scrapedo_slot()`) shared by gmaps and AI Mode, and the relationship pipeline additionally *derives* its in-flight row window from it (`relationship_runner._concurrency()`). What is gone is the old *thread-pool* meaning it had under AI Mode's in-process sync engine.
