# Plan: Decouple AI Mode into Scrape stage + Gemini-Batch LLM-Cleanup stage

## Context

AI Mode (`ai_mode_service.run_ai_mode_sync`) today runs a single in-process loop where each
batch of ~10 entities is scraped (scrape.do) and then **immediately** cleaned by a synchronous
LLM call. That works for quick OpenAI/opencode-zen testing but doesn't fit two goals:

1. **Cost/throughput at scale** — move LLM cleanup to Gemini's asynchronous **Batch API**
   (~half the cost, one job over the whole set). The Batch API is fundamentally
   "assemble all requests → submit one job → poll → map results back", so the interleaved
   per-batch model cannot be kept.
2. **A worker/queue future for millions of rows** — scrape and LLM-cleanup become two
   independently-triggerable stages connected by a durable on-disk handoff, so a future
   worker/queue can consume the cleanup step and failures can be re-run in isolation.

**Decisions (agreed with user):**
- Global env flag **`AI_MODE_LLM_BATCH`** (default `false`) selects behavior.
  - `false` → **current end-to-end synchronous path, unchanged** (used for OpenAI/opencode-zen).
  - `true` → AI Mode does **scrape-only** (stops at status `scraped`); a **new "LLM Cleaner" tab**
    drives the Gemini Batch cleanup on demand.
- Batch cleanup is **Gemini-only** (always `GEMINI_API_KEY` + native `batchGenerateContent`,
  independent of `AI_MODE_LLM_PROVIDER`). **No OpenAI batch.**
- Stage 2 is **block-and-poll in-process for now**, but persists the batch `job_name` so it is
  re-runnable/recoverable; the in-process trigger is the seam a future worker/queue replaces.
- **Stage 1 scrape runs in parallel** (bounded concurrency) — the main throughput win.

Intended outcome: large CSVs can be scraped once (in parallel) and cleaned cheaply via Gemini
batch with a single click, with the cleanup step isolated, idempotent, and re-runnable — while
the existing sync path keeps working.

---

## Architecture

```
Stage 1  (AI Mode tab, when AI_MODE_LLM_BATCH=true)
  upload CSV → scrape.do for every batch IN PARALLEL (bounded concurrency)
  → persist raw responses + scrape_manifest.json → status = "scraped"   (NO LLM call)

Stage 2  (new "LLM Cleaner" tab → single Start click)
  read manifest + raw responses → build ONE Gemini batch job (one request per scrape-batch,
  keyed "batch-NNN") → submit → poll to terminal → map by key → parse → write
  found.csv / notFound.csv / final_report.json → status = "completed"   (re-runnable)
```

Parallel scrape (Stage 1) is the main throughput win — scraping is the slow, one-at-a-time part
today. The Gemini batch (Stage 2) is then a single cheap click over the already-scraped data.

Durable handoff between stages = `raw_scrapedo_response/request_NNN.json` + `scrape_manifest.json`
(Stage-2 inputs, never mutated by Stage 2). This is exactly what a future queue job consumes.

---

## File-by-file changes

### 1. `ai_mode_service.py` — Stage-1 = parallel scrape-only under the flag

- Add `_batch_mode_enabled()` → `_bool_env("AI_MODE_LLM_BATCH", False)` (helper already exists).
- When batch mode is on, `run_ai_mode_sync` becomes **scrape-only + parallel**:
  - Build `groups = list(chunked(entities, batch_size))` as today.
  - **Fan out the per-batch scrape.do calls** over a `concurrent.futures.ThreadPoolExecutor`
    (`max_workers = SCRAPEDO_CONCURRENCY`, default 5). Each worker builds the query
    (`build_company_search_query`/`build_search_query`), calls
    `scrapedo_client.search_google_ai_mode`, writes `request_NNN.json`, times it, and returns a
    manifest entry (success, or `scrape_status="error"` + error). `ScrapeDoClient` is thread-safe
    (opens a fresh `httpx.Client` per call), so threads are safe.
  - Collect via `as_completed`; guard shared counters/status with a `threading.Lock`; bump
    `batches_done` + persist status as each batch finishes (live UI progress). Sort the final
    `scrape_manifest.json` `batches[]` by `batch_index`.
  - **Do not** call `make_llm_client`/`complete_json` at all (a missing OpenAI key never blocks
    scrape-only). After the pool drains: write `scrape_manifest.json` + scrape-only `report.json`
    + `run.log`, set `status="scraped"`. Do **not** write found/notFound/final_report.
- **Non-batch (sync) mode is 100% unchanged** — still the sequential interleaved loop. Parallel
  scrape is scoped to batch-mode Stage 1 only, to keep the regression surface minimal.
- Add `"scrape_manifest.json"` to `ALLOWED_RESULT_FILES`.

**`scrape_manifest.json` schema** (`ai_mode_result/<run_id>/`):
```json
{
  "run_id": "...", "input_type": "company|address", "batch_size": 10,
  "total_entities": 50, "scrape_completed_at": "ISO", "scrapedo_seconds_total": 66.8,
  "batches": [
    {"batch_index": 1, "entity_count": 10, "entity_names": ["..."],
     "scrape_status": "success", "error": null, "scrapedo_seconds": 12.4,
     "raw_json_file": "raw_scrapedo_response/request_001.json"},
    {"batch_index": 4, "entity_count": 10, "entity_names": ["..."],
     "scrape_status": "error", "error": "scrape.do timeout", "scrapedo_seconds": 90.0,
     "raw_json_file": null}
  ]
}
```
`batch_index` is 1-based, matches `request_NNN.json`. `entity_names` = `entity_name` (address) /
`company_name_eng` (company) — the exact keys `parse_results`/`parse_company_results` join on.

**status.json state machine** (batch mode): `queued → running → scraped → cleaning → completed|failed`.
Additive fields (existing list/UI code stays valid):
```json
"scrape_only": true,
"cleanup": {
  "status": "not_started|cleaning|succeeded|failed",
  "job_names": [],                 // list-ready for future sharding; one entry for now
  "batch_model": "gemini-2.5-flash-lite",
  "started_at": null, "completed_at": null, "batch_duration_seconds": 0.0,
  "batches_submitted": 0, "batches_excluded_scrape_failed": 0,
  "batch_cost_usd": 0.0, "error": null
}
```
`input_type` stays as today (drives company vs address everywhere). Top-level `llm_seconds_total`
stays in schema but is `0` in batch mode (per-batch LLM timing is gone); UI shows
`cleanup.batch_duration_seconds` instead.

### 2. `gemini_batch.py` — NEW, self-contained reusable Gemini-batch helpers

Leaf module at repo root, **no imports from `app.py`** (avoids circular import; leaves the SerpWow
single-RA path 100% untouched — chosen for zero regression risk). Self-contained copies/ports of
the helpers proven on the SerpWow side (`app.py:4400–4700, 1479, 1508`), raw `urllib`, no SDK:
- `gemini_batch_create_sync(model, requests_payload, label) -> dict` (POST
  `models/{model}:batchGenerateContent`, body `{batch:{display_name, input_config:{requests:{requests:[...]}}}}`, returns `{name}`)
- `gemini_batch_get_sync(batch_name) -> dict`
- `gemini_batch_state_name`, `gemini_batch_is_terminal`, `gemini_batch_is_success`
  (handle old `state.name` + new LRO `done/metadata.state` shapes)
- `extract_batch_inlined_responses`, `extract_batch_response_key`
- `extract_text_from_generate_response`, `parse_json_from_text`
- `calculate_gemini_batch_cost_usd(usage)` (+ small local float-env helper)
- **NEW** `messages_to_gemini_request(messages, *, temperature=0) -> dict` — lifts the
  messages→`{contents, systemInstruction, generationConfig}` conversion from
  `GeminiClient.complete_json` (`scrapedo_finder/llm_client.py:117-133`); returns the inner
  `request` dict (incl. `systemInstruction`, which the SerpWow single-prompt path doesn't use).
- Route the two HTTP calls through tiny `_http_post_json`/`_http_get_json` so tests can monkeypatch.

(Follow-up cleanup, NOT in this plan: dedupe `app.py`'s `_gemini_batch_*` to import from here via
aliases. Deferred to avoid touching the single-RA path now.)

### 3. `ai_mode_cleanup_service.py` — NEW, Stage-2 orchestrator

Pure-sync, never raises (mirrors `ai_mode_service`). Reuses private helpers from `ai_mode_service`
(`_run_dir`, `_read_status`, `_persist_status`, `_available_files`, `AI_MODE_RESULT_DIR`,
`load_address_entities`, `load_company_entities`, `build_ai_mode_settings`) for a single
status.json writer. Public API:
```python
def list_cleanable_runs() -> list[dict]      # runs with status in {scraped, cleaning, failed} (has manifest), newest first
def start_cleanup(run_id, *, force=False) -> dict   # validate → cleaning → submit+block-poll → write outputs; idempotent
def get_cleanup_status(run_id) -> dict        # reuse _read_status + refresh available_files
```
`start_cleanup` flow:
1. Read status. Guard: `scraped`/`failed` → run; `cleaning` → return current (no resubmit);
   `completed` → no-op unless `force=True`. Missing `scrape_manifest.json` → `ValueError` (→400).
2. `GEMINI_API_KEY` missing → `cleanup.status="failed"`, clear error, no job created, return.
3. Load manifest; `is_company = input_type=="company"`; `batch_size` from status. Re-load
   `input.csv` and `chunked(entities, batch_size)`. **Assert** re-chunked names match manifest
   `entity_names` per batch; mismatch → fail loudly (protects address/country backfill).
4. For each `scrape_status=="success"` batch: read `raw_json_file` → `payload["text_blocks"]`
   (references intentionally omitted) → `build_company_messages(group, tb, None)` /
   `build_messages(group_names, tb, None)` → `messages_to_gemini_request(...)` → wrap as
   `{"request": <..>, "metadata": {"key": f"batch-{idx:03d}"}}`. Record `key→batch_index→group`.
   `scrape_status=="error"` batches are **excluded**; their entities become error results
   (`entities_without_scrape_data`) landing in `notFound.csv`.
5. Set `status="cleaning"`, `cleanup.status="cleaning"`, `batches_submitted=N`, persist.
6. `gemini_batch_create_sync(batch_model, payload, run_id)` → `name`. **Persist
   `cleanup.job_names=[name]` immediately** (recovery seam). If a `job_name` already exists from a
   prior failed run, skip create and resume polling it (mirrors `app.py:4744-4771`).
7. Block-poll (`time.sleep`, `time.monotonic` deadline) using `GEMINI_BATCH_POLL_SEC` /
   `GEMINI_BATCH_TIMEOUT_SEC`, tolerating transient poll errors, terminal via
   `gemini_batch_is_terminal` (same logic as `app.py:4793-4839`).
8. On success: `extract_batch_inlined_responses` → map each by `extract_batch_response_key`
   (**key-only, no positional fallback**) → group. Per batch: `extract_text_from_generate_response`
   → `parse_json_from_text` → `parse_company_results`/`parse_results`; accumulate
   `usageMetadata` into a job-total `TokenUsage`. Address mode: backfill `location`/`country` by
   `zip(group, batch_results)`. Assign contiguous `sno` in `batch_index` order. Submitted batches
   with no matching inline response → entities get `error="missing from LLM batch output"`
   (`llm_errors`), land in `notFound.csv`.
9. Build report via `build_company_report_dict`/`build_report_dict` (token_usage,
   `time_taken_seconds=batch_duration`), enrich with `run_id`, `input_type`, `cleanup` block;
   `write_company_outputs`/`write_outputs` → `final_report.json`/`found.csv`/`notFound.csv`. Write
   Stage-2 `report.json` (job_names, batch_cost_usd, counts, per-batch mapped/missing diagnostics)
   and `run.log`. `batch_cost_usd = calculate_gemini_batch_cost_usd(totals)`.
10. `cleanup.status="succeeded"`, `status="completed"`, fill `websites_found/not_found`,
    `token_usage`, `batch_duration_seconds`, `completed_at`. Persist.
11. Terminal-failure/timeout/exception → `cleanup.status="failed"`, `status="failed"`, `error`,
    **retain `job_names`**, persist, return (never raise). Re-runs **overwrite** derived outputs
    (never append); raw responses + manifest untouched ⇒ deterministic.

### 4. `app.py` — new endpoints (lazy `import ai_mode_cleanup_service`, mirror existing AI Mode handlers)

- `GET  /uploads/ai-mode/cleanup` → `{count, runs}` (list cleanable). **Declare this literal route
  before any `/{run_id}` route** to avoid path shadowing — verify during impl.
- `POST /uploads/ai-mode/{run_id}/cleanup` (optional `?force=true`) → cheap validate, then
  `asyncio.create_task(asyncio.to_thread(start_cleanup, run_id, force))`; keep task in new
  module-level `ai_mode_cleanup_tasks: set`. Returns `{run_id, cleanup_status_url}`.
- `GET  /uploads/ai-mode/{run_id}/cleanup/status` → status dict (404 on KeyError).
- Result downloads reuse the existing `/uploads/ai-mode/{run_id}/result` + `get_ai_mode_result_path`
  (allowlist already extended with `scrape_manifest.json`).

### 5. `templates/ui.html` — new "LLM Cleaner" tab (mirror the AI Mode tab)

- Tab button `tabAiCleaner`; view `aiCleanerView`; extend `setActiveTab` + `isConsole` exclusion.
- "Cleanable Runs" table: Run ID, Status, Mode, Rows, Batches scraped, Cleanup state, Job, Cost,
  Updated, Actions. Actions: **Start Cleanup** (enabled when status ∈ {scraped, failed}) + download
  links (final_report/found/notFound) when `completed`.
- `refreshAiCleanerRuns()` → `GET /uploads/ai-mode/cleanup`; delegated Start click →
  `POST .../cleanup` then 2s poll of `.../cleanup/status` until `completed`/`failed`; disable button
  while `cleaning`. Reuse `escapeHtml`/`shortTs`; add `aiCleanerStatusClass()`. Wire into initial
  load + periodic `uploadsTimer`.

### 6. Env / `.env.example`

- **New:** `AI_MODE_LLM_BATCH=false` (the global flag); `SCRAPEDO_CONCURRENCY=5`
  (parallel scrape workers in Stage-1 batch mode).
- **Reuse:** `GEMINI_API_KEY`, `GEMINI_BATCH_MODEL` (→`GEMINI_MODEL`), `GEMINI_BATCH_POLL_SEC`,
  `GEMINI_BATCH_TIMEOUT_SEC`, `GEMINI_BATCH_*_USD_PER_1M_TOKENS`, `SCRAPEDO_BATCH_SIZE`.
- Add a short `.env.example` note explaining the two-stage flag + that batch cleanup is Gemini-only.

---

## Edge cases & resolved decisions

- **Scrape-failed batches:** excluded from job; entities → `notFound.csv`, `entities_without_scrape_data`.
- **Whole-job failure/timeout:** `failed`, `job_names` retained; re-POST resumes same job.
- **Missing inline responses:** key-only map; unmatched batch's entities → `llm_errors` / notFound.
- **Re-run:** `failed`→auto-resume; `completed`→no-op unless `?force=true`; outputs overwritten.
- **Concurrent Start:** in-memory guard — if `cleanup.status=="cleaning"`, return current (no resubmit).
- **Manifest:** re-load+re-chunk is the source of truth; `entity_names` only a consistency assert.

## Future worker/queue seam (millions of rows)

Keep `submit_cleanup(run_id)` (create+persist `job_names`+set `cleaning`) and `finalize_cleanup(run_id)`
(poll-once / on-terminal write outputs) as **separate idempotent functions**; `start_cleanup` just
calls them sequentially now. A worker later calls them on different triggers; because `job_names` is
persisted before polling and outputs are overwrite-idempotent, any worker resumes from `status.json`
alone. For per-run request caps, shard into multiple jobs and store all in `cleanup.job_names[]`
(field already list-shaped). Deferred, not in this plan.

**Deferred enhancements (discussed, intentionally NOT in this cut):**
- **JSONL File-API batch input** — Gemini inline batch requests cap out at high volume; switch
  Stage-2 submit to upload a JSONL via the File API when batch sizes get large. Inline is fine for now.
- **Dedupe/cache scrape results** — skip re-scraping repeated name+country across runs (cost saver
  at millions of rows).
- **Submit/finalize as separate endpoints** — go fully queue-driven (POST enqueues only; a cron/
  worker finalizes) instead of in-process block-poll. The internal `submit_cleanup`/`finalize_cleanup`
  split above already prepares for this.

---

## Verification

**Offline (no network):** monkeypatch `gemini_batch._http_post_json/_http_get_json`. Canned create
→ `{"name":"batches/test"}`; canned get → terminal `BATCH_STATE_SUCCEEDED` with
`response.inlinedResponses.inlinedResponses=[{"key":"batch-001","response":{candidates...,usageMetadata...}}]`.
Hand-build a run dir (`input.csv`, `status.json` status=`scraped`, `scrape_manifest.json`, one
`request_001.json` copied from the real `scrapeDo/reports/.../request_002.json`) → call
`start_cleanup`. Assert `final_report.json` written, found/notFound counts, `status="completed"`,
`cleanup.job_names` persisted, token_usage + batch_cost_usd populated, `sno` contiguous. Edge tests:
scrape-error batch → notFound; terminal-FAILED → failed + job retained; missing `batch-002` key →
llm_errors; no `GEMINI_API_KEY` → immediate failed, no create; re-run-after-failed reuses job.

**End-to-end (3-row sample):** `AI_MODE_LLM_BATCH=true`, real `SCRAPEDO_TOKEN`+`GEMINI_API_KEY`.
Upload `sample_company.csv` via AI Mode tab → expect `status="scraped"` + `scrape_manifest.json`,
no `final_report.json`. LLM Cleaner tab → run listed → Start → poll to `completed` → download
found/notFound, verify website columns + address/country backfill (address mode) /
confidence+flags (company mode). **Regression:** with `AI_MODE_LLM_BATCH=false`, upload runs the
unchanged sync path → `completed` directly; LLM Cleaner tab empty; SerpWow single-RA batch manager
still works (confirms `gemini_batch.py` is additive and `app.py` untouched).

---

## Critical files
- `ai_mode_service.py` (Stage-1 parallel scrape-only branch + manifest + state machine)
- `ai_mode_cleanup_service.py` (NEW — Stage-2 orchestrator)
- `gemini_batch.py` (NEW — self-contained reusable helpers)
- `app.py` (3 new endpoints + task set; no other behavior change)
- `templates/ui.html` (new LLM Cleaner tab)
- reuse: `scrapedo_finder/extraction.py`, `company_extraction.py`, `cleanup_reporting.py`,
  `company_reporting.py`, `llm_client.py`, `prompting.py`, `settings.py`
