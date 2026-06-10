# Plan: AI Mode — two-phase scrape → LLM cleanup (sync OR Gemini Batch)

> Supersedes the earlier "separate LLM Cleaner tab" design. The cleanup now stays **inside AI Mode**
> (no new tab/endpoints); the run is split into scrape-all → clean-all, with an env toggle for the
> cleanup engine.

## Context

AI Mode (`ai_mode_service.run_ai_mode_sync`) today runs **one interleaved loop**: scrape a batch of
~5 entities (scrape.do) → immediately clean it with a synchronous LLM call → next batch. At scale
(33k+ rows → 6,600+ scrape calls; the funnel reaches the millions) the synchronous interleaved LLM
step is the bottleneck (~150 s/batch) and a crash loses progress.

**Decision (no RabbitMQ / Redis):** AI Mode's throughput is gated by scrape.do's ~100-concurrent
**vendor cap**, which a single process saturates — a queue's distribution benefit doesn't apply, and
an on-disk record gives the durability/retry a queue would. SerpWow keeps RabbitMQ, untouched.

**This change keeps the current flow and UI; it only splits the phases:**
- **Same AI Mode tab, same upload endpoints, same outputs.** No new tab, no new endpoints, no upload UI.
- The flow becomes **two phases in one run**: Phase 1 scrape *all* batches, then Phase 2 clean *all*.
- **`AI_MODE_LLM_BATCH` env toggle** selects the cleanup engine:
  - **false (normal)** → today's synchronous path; provider via `AI_MODE_LLM_PROVIDER` (gemini *or* openai).
  - **true (batch)** → **Gemini only**, via the Gemini **Batch API**
    (https://ai.google.dev/gemini-api/docs/batch-api).

## Architecture

```
Phase 1 — Scrape (all first)
  parallel scrape.do over batches (ThreadPoolExecutor ≤ SCRAPEDO_CONCURRENCY)
  → write raw_scrapedo_response/request_NNN.json   (skip if already present = cheap resume)

Phase 2 — LLM cleanup (all after) — by AI_MODE_LLM_BATCH:
  false → sync: loop scraped batches, llm.complete_json per batch (provider = AI_MODE_LLM_PROVIDER)
  true  → Gemini Batch:
            build one Gemini request per scraped batch (GLOBAL key "batch-000001"…)
            shard into chunks of GEMINI_BATCH_SHARD_SIZE → one File API job per shard
            submit with ≤ GEMINI_BATCH_MAX_INFLIGHT jobs in flight; persist job_names list
            poll all jobs to terminal (≤ GEMINI_BATCH_TIMEOUT_SEC); collect results BY KEY

Phase 3 — Assemble (once all cleanup done; unchanged from today)
  map results → entities, address backfill, contiguous sno
  → final_report.json + found.csv + notFound.csv + report.json + run.log
  → status completed | completed_with_errors | failed
```

### Gemini Batch facts (confirmed from the docs)
- **Inline** batch input caps at **20 MB total** → ~6,600 requests (~25 MB) won't fit inline.
- **File API JSONL** caps at **2 GB/file** (~500k requests); `{"key","request"}` per line.
- **Results map back by `key`** (order/shard independent).
- **SLA 24 h target / 48 h hard expiry**; **priced at 50%** of interactive.
- We **shard** (even though one file could fit) for a small failure blast-radius, per-shard retry, and
  to stay well under the 48 h expiry. **Global keys** keep cross-shard result mapping unambiguous.

## File-by-file

### 1. `gemini_batch.py` — DONE (self-contained, stdlib `urllib`, no `app.py` import)
Public functions already implemented:
- `messages_to_gemini_request(messages, *, temperature=0) -> dict` — mirrors
  `scrapedo_finder.llm_client.GeminiClient.complete_json` (JSON mode + systemInstruction handling).
- `build_jsonl(items)` where `items=[(key, request_dict)]` → JSONL text.
- `upload_jsonl_file(jsonl_text, display_name) -> file_name` (resumable File API).
- `create_batch(model, items, *, display_name) -> create_obj` (inline if <18 MB else File API).
- `batch_name_from_create(create_obj) -> str`; `get_batch(batch_name) -> dict`.
- `state_name(obj)`, `is_terminal(state, done)`, `is_success(state, done, obj)`.
- `collect_results(batch_obj) -> [{key, text, usage, error}]` (inline OR downloaded result file).
- `parse_json_from_text(text)`, `calculate_gemini_batch_cost_usd(usage)`.
- HTTP isolated in `_http_post_json` / `_http_get_json` / `_http_get_bytes` (monkeypatch points).

### 2. `ai_mode_service.py` — restructure `run_ai_mode_sync` + config (the core change)
- **`build_ai_mode_llm_config`:** when `AI_MODE_LLM_BATCH=true`, force `provider="gemini"` (so
  `GEMINI_API_KEY` is validated at upload → fail fast) and model = `GEMINI_BATCH_MODEL` or `GEMINI_MODEL`.
- **Imports:** `from concurrent.futures import ThreadPoolExecutor, as_completed`; add `parse_gemini_usage`
  to the `scrapedo_finder.llm_client` import; `import gemini_batch`.
- **Replace the interleaved per-batch loop** with three phases. Preserve ALL existing variable names
  (`results`, `per_request_records`, `usage_total`, `entities_without_scrape_data`, `llm_errors`,
  `scrapedo_seconds_total`, `llm_seconds_total`, `sno`) so the **existing final-report block is reused
  verbatim**.
  - **Phase 1 — parallel scrape:** `ThreadPoolExecutor(max_workers=SCRAPEDO_CONCURRENCY)`; each worker
    builds the query (`build_company_search_query`/`build_search_query`), geo params
    (`_geo_params_for_group`), calls `scrapedo_client.search_google_ai_mode`, writes `request_NNN.json`,
    returns `{request_index, group, group_names, payload|None, error, scrapedo_seconds, rel_raw_path,
    geo_debug}`. **Resume:** if `request_NNN.json` exists and parses, reuse it (no re-scrape). Main
    thread aggregates via `as_completed`, bumps scrape counters + `entities_without_scrape_data`,
    `_persist_status` incrementally (set `status["phase"]="scraping"`).
  - **Phase 2 — cleanup** (`status["phase"]="cleaning"`):
    - **sync** (flag false): loop the successfully-scraped batches in `request_index` order; reuse the
      EXISTING messages/parse logic (`build_company_messages`/`build_messages` →
      `llm.complete_json` → `parse_company_results`/`parse_results`); per-batch `llm_seconds`.
    - **batch** (flag true): for each scraped-OK batch build `(key, request)` where
      `key=f"batch-{request_index:06d}"` and `request=gemini_batch.messages_to_gemini_request(messages)`.
      Shard the list by `GEMINI_BATCH_SHARD_SIZE`; submit shards with ≤ `GEMINI_BATCH_MAX_INFLIGHT`
      jobs in flight, persisting `status["gemini_batch_jobs"]` (list of names) **before** polling
      (resume seam — a re-run re-polls existing non-terminal jobs instead of resubmitting). Poll each
      job to terminal (`GEMINI_BATCH_POLL_SEC`, `GEMINI_BATCH_TIMEOUT_SEC`); `gemini_batch.collect_results`
      → index by key → `gemini_batch.parse_json_from_text` → `parse_*_results`; accumulate usage via
      `parse_gemini_usage`. A shard that fails after retry → its entities become per-entity error
      results (`llm_errors`) → notFound; do not block the run. In batch mode per-batch `llm_seconds=0`
      and `llm_seconds_total` = whole-cleanup duration.
    - LLM-failure for a batch (either engine) → emit per-entity error results exactly as today
      (`CompanyCleanResult(... error=...)` / `EntityCleanResult(... error=...)`).
  - **Phase 3 — assemble** in `request_index` order: address-mode backfill (`result.location=entity.address`,
    `result.country=entity.country`), contiguous `sno`, build `per_request_records` with the SAME field
    names/shape (`request_index, entity_count, entity_names, status, error, scrapedo_seconds,
    llm_seconds, combined_seconds, raw_json_file, scrapedo_params`). Then the existing final-report
    block (build report, `write_outputs`/`write_company_outputs`, `report.json`, `run.log`, status
    reconcile to `completed`/`completed_with_errors`) is unchanged.
- Keep `status["status"]="running"` across both phases (add only the harmless `status["phase"]` field —
  the UI ignores unknown keys). Never raise (outer try/except already sets `status=failed`).

### 3. `.env.example` — add new keys, remove dead `REDIS_*`
Add (with brief comments): `AI_MODE_LLM_BATCH=false`, `SCRAPEDO_CONCURRENCY=5`,
`GEMINI_BATCH_SHARD_SIZE=5000`, `GEMINI_BATCH_MAX_INFLIGHT=5`, `GEMINI_BATCH_MODEL=` (optional →
`GEMINI_MODEL`), `GEMINI_BATCH_POLL_SEC=15`, `GEMINI_BATCH_TIMEOUT_SEC=172800` (48 h). Reuse existing
`GEMINI_API_KEY`, `GEMINI_MODEL`, `AI_MODE_LLM_PROVIDER`, `OPENAI_*`, `GEMINI_BATCH_*_USD_PER_1M_TOKENS`.
Remove the unused `REDIS_*` keys (read by nothing — `docs/02` §B).

**No changes to `app.py` (same 4 AI-mode endpoints) or `templates/ui.html` (same tab).**

## Idempotency / recovery (single machine)
- Scrape keyed by deterministic `batch_index` → re-run/crash-resume skips already-scraped batches
  (no duplicate scrape.do cost).
- Batch cleanup: `gemini_batch_jobs` persisted before polling → re-run re-polls existing non-terminal
  jobs instead of resubmitting (no duplicate Gemini jobs / double cost). Results map by key; missing
  keys → notFound. Derived outputs overwritten, never appended.

## Defaults
`SCRAPEDO_CONCURRENCY=5` (raise toward ~100), `GEMINI_BATCH_SHARD_SIZE=5000` (6,600→2 jobs),
`GEMINI_BATCH_MAX_INFLIGHT=5`, `GEMINI_BATCH_TIMEOUT_SEC=172800` (48 h).

## Verification
- Static: `python3 -c "import ast; ast.parse(open('ai_mode_service.py').read())"` (+ `gemini_batch.py`);
  `.venv/bin/python -c "import app"`.
- Offline batch test (monkeypatch `gemini_batch._http_*`): hand-built run dir + `AI_MODE_LLM_BATCH=true`
  → asserts `final_report.json`/found/notFound, status completed, keys mapped, missing key → notFound.
- Regression: `AI_MODE_LLM_BATCH=false` → unchanged sync behavior (now two-phase).
- E2E: real `SCRAPEDO_TOKEN`+`GEMINI_API_KEY`, upload `sample_company.csv`, poll to completed.
- `.venv/bin/python -m pytest tests/`.

## Critical files
- `gemini_batch.py` (done) · `ai_mode_service.py` (run_ai_mode_sync + build_ai_mode_llm_config) ·
  `.env.example`. No `app.py` / `ui.html` changes.
