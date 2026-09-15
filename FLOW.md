# FLOW — how execution actually travels

Scope: the **gmaps** pipeline (scrape.do Google Maps), end to end, plus the credit/billing
numbers that ride along it. Written 2026-08-11 alongside the billing-display fix; the
sections marked **▲ CHANGED** are what that fix touched.

Architecture prose lives in `CLAUDE.md`; this file is the call graph. Read it when you
need to know *who calls what, in what order* — not what the design is.

---

## 0. The one-line map

```
POST /uploads/gmaps ──► S3 (input.csv, status.json, pointer) ──► RabbitMQ (1 msg / run)
                                                                        │
                                              worker.py ── consume_gmaps_runs
                                                                        │
                                                              gmaps_runner.drive_run
                                                                        │
                                                       s3_run_driver.drive  (single-flight)
                                                                        │
                                                          gmaps_runner._phases
                                                          ├─ run_scrape_phase  (phase 1)
                                                          └─ write_outputs     (phase 2)
                                                                        │
                                              Supabase + Slack ◄── driver.notify_terminal

GET /uploads/{id}/status ──► engine._gmaps_status ──► run_detail.js  (the UI)
```

Two phases only. There is no LLM pass, no Gemini batch, no barrier machinery — heuristic
confidence is computed inside the row task.

---

## 1. Upload (API process)

`backend/app/services/serpwow/engine.py`

1. `create_gmaps_upload()` — `@app.post("/uploads/gmaps")` (engine.py:4206)
   1. `parse_entities_csv(raw, sample_limit=0)` — validates **every** row, retains none.
      A 500k-row upload costs the API process a fixed amount of memory.
   2. Guards, in this order and deliberately so: bad CSV → 400 about the CSV;
      missing `SCRAPEDO_TOKEN` → 400; missing `S3_BUCKET` → 400.
   3. `companies.create_run(...)` → Supabase row (`status="queued"`), id kept as `run_db_id`.
   4. `s3_run_store.put_bytes(input_key(prefix), raw)` — input.csv is the row list; it is
      never materialised in the API.
   5. `s3_run_store.write_run_pointer(run_id, prefix, company, run_db_id, "gmaps")`
      → `_gmaps_runs/<run_id>.json`. The only run_id → prefix map that exists.
   6. `Counters(prefix, rows_total, phase="queued").flush(True)` → `status.json`.
      **This is the API's only status.json write.** From the first scrape on, the worker
      is the single writer.
   7. `publish_gmaps_run(run_id)` (engine.py:3344) — ONE persistent AMQP message,
      `{"run_id": …}`, routing key `gmaps.run`. Publish failure is swallowed on purpose:
      the run already exists in S3, and `redrive_stale_runs` will start it.

Nothing about the row work happens here.

---

## 2. Drive (worker process — `python worker.py`, exactly one)

`gmaps_runner.consume_gmaps_runs(channel)`
→ `s3_run_driver.consume_runs(queue="gmaps_runs", routing_key="gmaps.run", drive_fn=drive_run)`
→ **acks on receipt** (a multi-hour run would trip RabbitMQ's 30-min `consumer_timeout`),
then `gmaps_runner.drive_run(run_id)` → `s3_run_driver.drive(...)`:

1. `_driving` single-flight guard (in-process set) — the consumer and the stale-run scan
   can both pick the same run; two live drivers *would* re-spend credits.
2. `store.read_run_pointer` → prefix. `store.read_status` → counters, **carried forward**
   (`created_at` never re-stamped, so total wall clock survives a restart).
3. `update_supabase(pointer, status="running")`.
4. `phases(prefix, counters, pointer)` = `gmaps_runner._phases` — section 3 below.
5. On success: `notify_terminal(...)` → Supabase terminal row + Slack.
   On exception: `drive_attempts += 1`, `phase="failed"`, Supabase `failed`. Never raises.

`redrive_stale_runs()` (folded into the worker's periodic reconciler) re-drives runs whose
phase is not in `TERMINAL_PHASES`, bounded by `GMAPS_MAX_DRIVE_ATTEMPTS`. Re-driving is
free: every row that already has an object is skipped.

---

## 3. Phase 1 — scrape

`gmaps_runner._phases` → `run_scrape_phase` → `s3_run_driver.run_row_phase(done_prefix="rows")`

`run_row_phase` is the generic engine:

- `store.list_done_rows(prefix, "rows")` — ONE paginated LIST, an in-memory index set.
  Resume costs one LIST, not 500k HEADs.
- `_iter_pending` streams `input.csv` and yields only rows with no `rows/` object.
- A bounded task window of `concurrency()` = `SCRAPEDO_CONCURRENCY` live tasks,
  refilled on `asyncio.wait(FIRST_COMPLETED)`.
- `store.stop_requested(prefix)` is checked at most once per second (wall-clock gated).
- `drain()` retrieves every finished task's exception → `task_errors` counter.

Per row, `gmaps_runner._scrape_one(prefix, row, counters)`:

```
_scrape_one
├─ row_fields(row)                                   modes/gmaps.py
├─ (no company name) → write rows/ + rows_scraped, return.  Costs nothing.
├─ execute_gmaps_lookup(...)                          modes/gmaps.py:65
│   └─ run_gmaps_from_module(...)                     modes/common.py:107
│       └─ scrapedo_maps_client.process_gmaps_query() ← the ONLY HTTP call
│           ├─ pooled httpx.AsyncClient (keep-alive)
│           ├─ async with provider_limits.scrapedo_slot()   ← per-ACCOUNT cap
│           ├─ retries: SCRAPEDO_MAX_RETRIES (default 2 → 3 attempts)
│           │   backoff 1/2/4s (5xx) vs 5/10/20s (429), Retry-After wins, cap 30s
│           └─ returns _envelope(...) — NEVER raises
│       └─ _select_best_gmaps_website / extract_gmaps_website / disallow filter
│   └─ _score_gmaps_candidates + _gmaps_confidence_block   gmaps_scoring.py
│   └─ builds context{gmaps, formatted_results[1], cost_breakdown, gmaps_confidence}
├─ counters.bump(requests=…, credits=…)               ← from cost_breakdown
├─ put_bytes(raw/<shard>/row_NNNNNN.json)             ← what came back, written FIRST
├─ put_object(rows/<shard>/row_NNNNNN.json)           ← the row is DONE at this write
│     (PUT failure ⇒ rows_failed, never swallowed: S3 is the only copy)
├─ counters.bump(rows_scraped | rows_failed)
├─ counters.bump(rows_no_listing=1)   if scrapedo_no_results        ▲ CHANGED
├─ counters.bump(rows_billed_empty=1) if scrapedo_billed_empty
└─ _log_row_stage("gmaps.scrape", …)                  row_logging.py (prints)
```

### What each S3 folder holds (one object per row, per folder)

| folder | content | role |
|---|---|---|
| `raw/<shard>/row_NNNNNN.json` | the call **and what came back**: `query`, `gl`, `request_count`, `successful_requests`, `credits`, and `results[]` — scrape.do's `local_results` verbatim (title, address, phone, rating, website, …) | the artifact a human reads to judge a row |
| `rows/<shard>/row_NNNNNN.json` | **our verdict**: `official_website`, `summary`, `fields`, `context{cost_breakdown, gmaps_confidence{score, reason, name_match, address_match}, formatted_results}` | the **DONE marker** — a row is finished when this exists |
| `errors/<shard>/row_NNNNNN.json` | the provider's own message + category | died after every retry |

`rows/` is **not** "cleaned" data — gmaps has no LLM pass. Relationship's third folder
*is* `cleaned/` (Gemini verdicts), which is a different stage, not a different name for
the same thing. `rows/` keeps its name because renaming the DONE marker would hide every
finished row from the resume scan and re-drive them at 10 credits each.

### Where the money is decided — `scrapedo_maps_client.process_gmaps_query`

One row = one *billed* call at most. Every outcome, and what it costs:

| Outcome | envelope | billed? |
|---|---|---|
| HTTP 200 with `local_results` | `successful_requests=1`, `credits=10` | **yes** |
| HTTP 200, empty `local_results` | same + `billed_empty=True` | **yes** — money for nothing, the refund case |
| HTTP 200 whose body carries `error` | same + `error` | **yes** |
| 502 `{"error":"no results"}` after every retry | `no_results=True`, no error | no |
| 429/5xx/transport after every retry | `error`, `error_category` | no |

`credits` is **derived** — `CREDITS_PER_CALL × successful_requests` — never counted by
hand, so a run always reconciles as `requests == successful + failed`.

`modes/gmaps.py` then splits the *failed attempts* by what the ROW became:
`scrapedo_recovered_requests` (row succeeded anyway), `scrapedo_error_requests` (row died
after every retry — the only real errors), remainder (attempts on a no-listing row).

---

## 4. Phase 2 — outputs

`gmaps_outputs.write_outputs(prefix, counters)` (called via `asyncio.to_thread`)

- `counters.set_phase("reporting")`, force flush.
- Streams `store.iter_input_rows(prefix)`; per row reads `rows/…` (falling back to
  `errors/…`), rebuilds a state-shaped row via `_state_row`, and sums the per-row
  `cost_breakdown` into run totals: `requests / successes / failed / credits /
  billed_empty / no_results / recovered / error_requests`.
- `reporting.row_to_entity_result` → `found.csv` / `notFound.csv`, written as
  **the input file plus what we worked out** ▲ CHANGED: `common.text.passthrough_row` emits
  the original cells under the input's own header (via `s3_passthrough`, which is
  collision-safe and undoes `iter_input_rows`' injected `row_index`), then
  `reporting.result_cells` appends `website_url, confidence, flags, attempt_log`
  (+ `error` on notFound). gsearch keeps the old fixed `CSV_COLUMNS` — it never persists
  the upload, so it has no input columns to pass through.
- `reporting.retry_row(...)` → **`retry.csv`** ▲ CHANGED — the rerun / refund list.
  Written in the same pass, from the same `original` input row, so it costs no extra read.
  A row is in it only when it has nothing to show for itself:
  `billed_empty` (charged, no data — the refund claim) → `no_listing` (unbilled) →
  `error` (dead after every retry, or never processed). Columns are the **input header
  verbatim** + `retry_reason`, so the file uploads straight back to `/uploads/gmaps`.
  Always written, header-only when there is nothing to retry.
- `requests = max(row-sum, counters["requests"])`: attempts are unrecoverable from the
  objects if a row's result PUT failed, so the live counter wins when higher.
- Emits `summary` → `report.json` (**summary only**; a per-row array would be gigabytes at
  500k) with:
  - `empty_response_breakdown = {no_listing, billed_empty}`
  - `cost = reporting._build_cost(...)` — the full scrape.do key set.
- `run.log` header via `_cost_log_line`:
  `scrapedo_requests=139 (ok=87 recovered=0 no_listing=52 errors=0) scrapedo_credits=870 rows_no_listing=13`
  (`no_listing=52` counts **attempts**; `rows_no_listing=13` counts **rows**.)
- `counters.set_phase("completed")`, force flush.

---

## 5. Read path — status → UI

```
GET /uploads/{ref}/status
└─ engine._gmaps_status(run_id)
   └─ engine._s3_run_status(segment="gmaps", fallback=_gmaps_fallback_summary)
      ├─ phase == "completed" → serve report.json's summary verbatim
      └─ otherwise           → _gmaps_fallback_summary(counters, …)   ▲ CHANGED
         (built from status.json's O(1) counters — same JSON shape either way)
└─ response.serpwow_summary → static/js/run_detail.js
   └─ renderLegacyStatus(root, ref, s)
      ├─ outcomeSummary / executionStrip
      ├─ costSection(g, {providerLabel:"Scrape.do", searchKey:"scrapedo_credits",
      │                  failedSearchCount: cost.scrapedo_error_requests})
      └─ emptyResponsesSection(g)                                     ▲ CHANGED
```

`run_detail.js` branches on the **data**, never the pipeline key — a pre-migration gmaps
run still carries `serpwow_*` keys and must keep rendering its old cost card.

---

### 4b. relationship's equivalent

`relationship_outputs._write_outputs` runs the same three-file shape one phase later
(scrape → Gemini Batch verdicts → outputs): `confirmed_relation.csv` /
`notconfirmed_relation.csv` split by **relationship status**, plus the same
**`retry.csv`** ▲ CHANGED, built from the same shared helper. Its membership rule differs
in one place — there is no `no_listing` (that is a Maps-only 502), and `billed_empty` is
*derived* per row as "HTTP 200 with neither `text_blocks` nor `references`", matching how
the summary already counts it. Rows with citations but no prose (`no_ai_text`) are
**not** in the list: the gate had candidates and produced a verdict.

---

### 4c. AI Mode's equivalent — the one with a cursor

AI Mode's assembly never sees the input CSV: `run_reporting.StreamingRunReport` is fed
`EntityResult`s, batch by batch. So **the writer opens its own forward-only cursor** over
`run_dir/input.csv` and pulls exactly one row per result:

```
ai_mode_service.run_ai_mode_finish
├─ parsed_input = parse_entities_csv(input.csv)        ← already happened; now KEPT
├─ StreamingRunReport(run_dir, parsed_input.columns_detected["company_name"])
│    └─ _open_input(): DictReader over input.csv, filtered by the SAME rule
│       parse_entities_csv uses — skip a row whose company cell is empty
│       (it costs no sno, so the Nth result is NOT the Nth data row)
└─ for rec in ordered:            ← ascending request_index, one result per entity
     report.add_batch(...) → add_results() → row.update(next(cursor))
```

Alignment is positional-by-consumption and is an invariant of the **writer**, not the
caller — which is why the failed-batch `continue` cannot desync it (that path emits one
`_error_results` row per entity through the same `add_results`). `close()` warns if the
cursor has rows left over. `company_column=None` (headerless/positional input, unreadable
input.csv, or the classic `write_outputs()` entry point) keeps the old fixed columns.

Never joins on `EntityResult.sno` — that field takes the LLM's echoed value when present.

---

## 6. Exactly what this change modified

| File | Function | Change |
|---|---|---|
| `static/js/run_detail.js:403` | `emptyResponsesSection(g)` | Took `g` (the whole summary) instead of just `empty_response_breakdown`, and added a **gmaps branch**, keyed on `eb.no_listing != null`: "Scrape.do billing (10 credits per HTTP 200)" with four chips split by what was PAID — *Billed calls · Billed but no result (empty **or** error body → refund claim) · Failed after retries (every attempt 502/429/timeout, free) · Unbilled attempts*. gsearch (`all_phases`/`some_phases`) and relationship (`no_ai_text`) branches are untouched. |
| `reporting.py` | `_build_cost` | New `scrapedo_billed_errors` (rows that errored *with* a billed 200). `Billed but no result = billed_empty + billed_errors`; `Failed after retries = no_listing + (errored − billed_errors)` — the subtraction is what keeps a paid error row out of both chips. Defaults to 0, so other pipelines are unchanged. |
| `static/js/run_detail.js:821` | `renderLegacyStatus` | Call site now passes `g`. |
| `s3_run_store.py:458` | `Counters._FIELDS` | Added `rows_no_listing`. |
| `s3_run_store.py:473` | `Counters.bump` | Raises `KeyError` on an unknown counter instead of silently dropping it. |
| `engine.py:5279` | `_gmaps_fallback_summary` | Mid-run `cost` now carries `scrapedo_successful_requests` (= `credits // CREDITS_PER_CALL`) and `scrapedo_failed_requests`, so the billing card is populated before `report.json` exists. |
| `reporting.py:26` | `retry_column` / `retry_row` | **New.** The shared rerun/refund-row builder: membership rule, reason vocabulary, and input-column passthrough, in one place for both pipelines. |
| `gmaps_outputs.py` | `_write_outputs` | Third spooled temp file → `retry.csv`. `error=` is passed only when `_derive_outcome(row) == "error"`, so a no-listing row and a "Row has no company name" row are not mislabelled as failures. |
| `relationship_outputs.py` | `_write_outputs` | Same, with `billed_empty` derived from the stored response (`not blocks and not refs`, only when the row has no error). |
| `engine.py:220 / 5134 / 5151` | `_GSEARCH_RESULT_FILES`, `_RELATIONSHIP_FILES`, `_GMAPS_FILES` | `retry.csv` added to the `/result` allowlist and to both pipelines' advertised file sets. |
| `run_detail.js:865` | `renderLegacyStatus` | Files card offers `retry.csv` for gmaps + relationship only (gsearch shares that branch and writes none). |
| `common/text.py` | `passthrough_fieldnames` / `passthrough_row` | **New.** The input-columns-first rule, shared by all three pipelines. Here and not in `reporting` because `ai_mode/run_reporting.py` is standalone-by-contract and `reporting` drags in `httpx`. `source_overrides` exists because the S3 readers inject `row_index` and AI Mode's doesn't. |
| `reporting.py` | `CSV_COLUMNS` split | `RESULT_COLUMNS` (the computed four) + the three echoed fields = today's `CSV_COLUMNS`, byte-identical, so the gsearch writer is untouched. Adds `result_cells` and `s3_passthrough`. |
| `relationship_outputs.py:58` | `_passthrough_fieldnames` | Now a one-line wrapper over the shared helper; its collision test passed unchanged. |
| `gmaps_outputs.py` | `_write_outputs` | found/notFound built from `input header + RESULT_COLUMNS` instead of `CSV_COLUMNS`. |
| `ai_mode/run_reporting.py` | `StreamingRunReport` | New optional `company_column`; owns the input.csv cursor (`_open_input` / `_close_input`), `extrasaction="ignore"` on both writers, desync canary in `close()`. |
| `ai_mode_service.py:867,1154` | `run_ai_mode_finish` | Keeps the `ParsedCSV` it already built and passes the resolved company column to the report. 3 lines. |

Tests added: `tests/test_gmaps_runner.py` (no-listing counter reaches status.json;
mid-run summary splits billed/unbilled; `retry.csv` membership + the refundable
billed-empty row), `tests/test_s3_run_store.py` (`bump` rejects an unknown counter),
`tests/test_relationship_outputs.py` (`RetryCsvTests`),
`tests/run_detail_dom_contract.mjs` (`gmapsBillingBreakdown`, seeded with the real run
8ffe96d9 numbers; relationship Files card now expects `retry.csv`).

**Not touched, on purpose:** `scrapedo_maps_client` (the credit arithmetic was already
correct — verified against the provider dashboard, section 7),
`reporting._build_cost`, and the Slack payload.

---

## 7. The reconciliation this fix is about (run `8ffe96d90db341ca85147ead3459c05c`)

100 rows, prefix `test-isi-market/gmaps/8ffe96d9…`:

```
139 attempts = 87 billed 200s  +  52 free failures
 87 × 10 credits = 870         ← exactly what scrape.do's dashboard charged
 52 free failures = 13 no-listing rows × 4 attempts (1 + SCRAPEDO_MAX_RETRIES… see below)
100 rows = 68 found + 32 not found + 0 errored
```

So "1000 credits if everything succeeded" minus 13 rows Google has no Maps listing for =
870. Nothing was mis-billed and nothing was under-counted; the run just had no surface
that said so. (The 4-attempts-per-dead-row implies `SCRAPEDO_MAX_RETRIES=3` in that
deployment's `.env`, not the code default of 2.)

Checked row by row: all 13 read `successful_requests: 0, credits: 0, no_results: true` —
four 502s each, **never an HTTP 200**, so there is nothing to claim a refund for. The
billed-for-nothing case (`billed_empty`) was 0 on this run. Their rerun list is
`docs/retry_8ffe96d9_gmaps.csv`, backfilled with the same helper the pipeline now uses.
