# HANDOFF — `website_url_finder`

Last updated: 2026-07-23. Read this first if you're picking up this repo. Durable architecture (module map, S3 layout, pipeline internals) lives in `CLAUDE.md`; older dated sessions are archived in `docs/HISTORY.md`.

---

## Current status

- **Active branch: `relationship-ai-overview`** (off `aiModeBroker`). The earlier relationship AI-Overview rework is committed locally; the 2026-07-23 relationship failure-diagnostics/retry/UI changes are **UNCOMMITTED, NOT live-verified, NOT pushed**.
- Full suite: **564/564** passing.
  ```bash
  cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
  ```
  (`-t .` is **mandatory** — it makes `tests/__init__.py`'s hermeticity guard run so real cloud creds from `.env` don't leak into tests.)

## What works now

Two independent discovery systems behind one FastAPI app + vanilla-JS UI (see `CLAUDE.md` for the architecture):

- **SerpWow pipelines** — `firmographics`, `gmaps`, `gsearch`, `relationship` (the legacy `full`/`url_discovery` modes were removed 2026-07-20). gsearch/gmaps/relationship have confidence scoring + `found.csv`/`notFound.csv`/`report.json`/`run.log` output parity, S3 mirroring, Supabase counters, and run-detail UI.
- **AI Mode** — `ai_bulk`, `ai_deep` (scrape.do -> LLM cleanup), **now broker-driven** (RabbitMQ scrape phase in the worker process, reconciler self-healing, streaming assembly, Gemini Batch API support). See the AI Mode section in `CLAUDE.md`.
- Cross-cutting: unified CSV input, per-company/per-pipeline S3 layout, Supabase run tracking, best-effort Slack completion/failure pings (with split LLM vs SerpWow cost), the redesigned dark UI across all views.

## Active risks / unfinished work

- **`aiModeBroker` is NOT live-verified** — offline tests only (532/532). Live smoke needed before relying on it (see "Latest completed session" below for the checklist). **AI Mode now REQUIRES RabbitMQ + the worker process** — uploads 503 without the broker; the worker env needs `SCRAPEDO_TOKEN` + LLM keys + S3/Supabase/Slack vars (shared repo-root `.env` covers same-host setups). Run exactly ONE worker process.
- A high-effort adversarial code review (2026-07-14) found 10 defects; **all fixed** in the 4th commit (dead queue-depth probe — aio_pika has no `Queue.declare(passive=)`, fixed here AND in SerpWow's pre-existing `_get_rabbitmq_queue_depth`; missing S3 seed mirror; barrier double-counting; corrupt-raw-file resume; env-recomputed batch_size; double-resume double-billing; non-atomic status.json/raw writes; redelivered-flag poison guard → durable `delivery_failures` budget; partial CSVs served as complete; CSV parse on the event loop).
- Known follow-ups from the rework (accepted): sync-LLM cleanup in the finish task is still serial (the plan's `AI_MODE_LLM_CONCURRENCY` pool was deferred — at 500k+ the Gemini Batch path is the intended one); the reconciler has no S3 cold-start scan (a wiped host relies on the resume endpoint's rehydrate — per the manual-rerun preference); multi-host workers would need S3-based file-presence checks; the queue-depth gate counts READY messages only (staleness checks on file activity cover in-flight work); `final_report.json`'s `requests` array is uncapped (~30MB at 100k batches — accepted watchpoint).
- **`errorTaxonomy` branch (2026-07-10) is NOT merged — user's call.** Introduces the `found`/`not_found`/`error` outcome taxonomy (error source+category, `not_found` becomes `completed`). Reviewed, 416/416 offline, but **not live-verified**. Details in `docs/HISTORY.md` (2026-07-10).
- **Other historical branches** (`gmapsfix`, `gsearchFix`, `relationshipMode`) — `relationshipMode` was merged into `revampCode` via PRs #5/#6; the gmaps/gsearch branches carry earlier features that are captured in `CLAUDE.md`. See `docs/HISTORY.md` if you need their state.

## Run / test commands

```bash
# Web server (FastAPI app is defined in serpwow/engine.py; main.py wires the rest)
cd backend && ../.venv/bin/python -m app.main       # or: ../.venv/bin/python ../run.py
# UI at http://localhost:<API_PORT>/app  (default port 11500; .env overrides)

# Worker — needed for SerpWow pipelines AND AI Mode (broker-driven since 2026-07-14)
docker compose up -d rabbitmq                        # broker + mgmt UI on 15672
python worker.py                                     # from repo root; ONE process only

# Tests (offline, unittest — NOT pytest; -t . is mandatory)
cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
```

## Immediate next steps

1. Live-smoke the current relationship changes with a small CSV, including a literal `ERROR:` or `FETCH_ERROR:` Company Y value; confirm it is accepted and processed verbatim (apart from existing outer-whitespace trimming).
2. Commit the current relationship changes if approved, then decide merge/push (pushing requires explicit user approval).
3. **Live-smoke `aiModeBroker`** (checklist in the older session notes below).

## Conventions (do not break)

- **Always use ponytail for coding** — the `ponytail` plugin/skill (installed: `ponytail@ponytail`) is the required mode for any coding work here (writing, refactoring, reviewing, choosing deps). Laziest solution that actually works: question whether it needs to exist (YAGNI), reuse what's here, stdlib/native before new deps, shortest correct diff — but never shortcut understanding the problem first.
- **Never** add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits — tell any committing subagent the same.
- **Never** `git push` or query the production Supabase DB without explicit user approval.
- `docs/` is **gitignored** — specs/plans/handoff-history there are on-disk only; code under `backend/` commits normally.

---

## Open findings — 2026-07-23 (S3 throttling + concurrency; DISCUSSION ONLY, no code changed)

Investigated during a live 1000-row relationship run. **No code was changed** — these are
recommendations/answers for a future decision. A scratch plan sits at
`~/.claude/plans/transient-nibbling-pearl.md` (transient).

- **S3 `SlowDown` in worker logs** (`ClientError (SlowDown): Please reduce your request rate`): S3's HTTP-503 throttle. It is **already retried (5× exponential backoff) and best-effort — non-fatal**, so the run is unharmed (`_write_s3_background`, `engine.py:855`; local disk is source of truth, S3 is the resume/backstop mirror). Store is **real AWS S3** (region `ap-south-1`, `AKIA…` IAM key, **no custom `endpoint_url`** — not R2/MinIO), so AWS auto-scales the prefix to 3,500 PUT/s; the 503s are the transient cold-prefix ramp **amplified by `state.json` being re-PUT to the SAME key on every row** (`engine.py:2867`; note `output.json` is already deferred to terminal-only, comment at `engine.py:2869`). **Recommended, NOT applied:** (a) **primary/minimal** — switch boto3 `retries` `"standard"`→`"adaptive"` in `core/s3.py:42` + `engine.py:740` (AWS's own client-side rate-limiter answer to SlowDown); (b) **optional** — throttle the per-row `state.json` S3 mirror behind a new `SERPWOW_S3_STATE_FLUSH_SEC` (~5s, mirrors AI-Mode's `AI_MODE_STATUS_FLUSH_SEC`; local write stays per-row, accepted tradeoff = S3 copy lags ≤5s on cold-start resume, safe since rows are idempotent). Also flagged: the AWS **secret key lives in `.env`** — keep it gitignored, never commit it.
- **Worker concurrency = `WORKER_CONCURRENCY` (env, default 4)** — sets BOTH the RabbitMQ channel prefetch and the consumer-loop count (`engine.py:3253` / `3856`), **identical for gsearch and relationship** (same `process_upload_job` path). So only 4 rows process at once; each relationship row fans **3** AI-Overview searches in parallel → ~`3 × WORKER_CONCURRENCY` in-flight SerpWow calls. Observed ~400 rows/hr at default 4. Raising it speeds runs **but requires a worker restart** (consumers are created at startup; won't affect an in-flight run) and is bounded by SerpWow's account rate limit (and would make the S3 SlowDown above worse). Not changed.
- **`websites_found` = 0 during a running relationship batch is EXPECTED, not a bug**: with `RELATIONSHIP_LLM_BATCH=true` ("Batch: On" in the UI) the gate/URL-selection runs in the Gemini **batch phase after all rows finish Phase-1 scraping**, so found/confirmed populate only at the end.

---

## Latest completed session — 2026-07-23 (relationship failure diagnostics, retries, outputs, and failed-row UI)

**Status: UNCOMMITTED on `relationship-ai-overview`, 564/564 offline tests passing,
NOT live-verified, NOT pushed.** Implemented with Ponytail: existing pipeline and
failure-analysis endpoint reused; no new dependency, endpoint, modal, or database migration.

### Investigated run

- Run `ac75e704-f609-4b00-ac33-4b33e066ccff` had exactly **2 failed rows** in its state: row index **63** (`Kitche`) and **67** (`CASCADE COFFEE`). Both were **SerpWow timeouts**, not Gemini failures. Their blank exception messages caused the aggregate classifier to label them `internal`; that classification bug is fixed.
- The Gemini batch itself succeeded with no recorded Gemini chunk failures. Row 100 finished SerpWow after the Gemini input snapshot and retained `Pending Gemini batch post-processing decision.` after the batch became terminal — a real snapshot race, now covered by a regression test.
- Values such as `FETCH_ERROR: 403...` / `ERROR: 503...` seen on other rows were the uploaded `Company_Name_Y` text, not SerpWow failures from this run. A prefix-rejection guard was briefly implemented, then **removed per user direction**: these values are valid inputs for this workflow and now proceed exactly like any other Company Y value (the parser's pre-existing outer-whitespace trim still applies).

### Changes

- **Bounded SerpWow retries:** `run_serpwow_search` now makes at most **3 total attempts**, with 1s/2s backoff, for `httpx.TransportError`, HTTP 429, and HTTP 5xx. Non-transient HTTP 4xx responses such as 403 return immediately. No retry library/config layer was added.
- **Useful timeout errors:** an exception whose sanitized message is empty now falls back to its class name (for example `ReadTimeout`). `_phase_stats` counts an explicit `error_category` even when the message is blank, preserving `timeout` instead of falling back to `internal`.
- **Terminal Gemini invariant:** after a terminal batch, a completed no-URL row absent from the input snapshot and still carrying the exact pending sentinel becomes `failed`, `outcome=error`, `error_source=gemini`, `error_category=internal`, with `Gemini batch missed row after its input snapshot.` It is therefore visible and eligible for failed-row retry instead of remaining falsely pending.
- **Relationship output cleanup:** removed `verified_pair` and all `X ↔ Y` presentation from relationship context, CSV/report/run-log output, descriptions, and production code. `error_source` now occupies the former relationship-CSV column position; it is blank for normal business outcomes and identifies technical providers such as `serpwow`/`gemini` on failures.
- **Failure inspection:** `build_failure_analysis` samples now include `error_source` and `error_category`. Terminal Run Detail pages with errors show a lazy `View failed rows (N)` control that reuses `GET /uploads/{id}/failure-analysis?sample_limit=100` and renders CSV row, company, source, category, and error in an accessible inline table.
- **Company Y remains authoritative input:** there is no `ERROR:`/`FETCH_ERROR:` filtering or name correction in the CSV parser. Separately, `resolved_company_y_name` remains Gemini's evidence-based interpretation from the relationship prompt; it does not overwrite the original `Company_Name_Y` column and is blank when Gemini omits it or the LLM is skipped.

### Verification / files

- Full offline suite: `cd backend && ../.venv/bin/python -m unittest discover -s tests -t .` → **564/564 passing**.
- Run Detail DOM contract passes; `git diff --check` passes; `rg -n 'verified_pair|↔' backend/app` returns no production matches.
- Production files changed: `engine.py`, `modes/relationship.py`, `outcomes.py`, `query_builders.py`, `serpwow_client.py`, `serpwow_reporting.py`, `static/js/new_run.js`, and `static/js/run_detail.js`; related regression tests changed alongside them.

---

## Previous completed session — 2026-07-20 (relationship mode → AI-Overview prose search + confirmed/notconfirmed outputs, branch `relationship-ai-overview` off `aiModeBroker`)

**Status: COMMITTED on branch `relationship-ai-overview` (off `aiModeBroker`), tree clean,
NOT merged/pushed. 557/557 offline, NOT live-verified.** Commits: `7722d9b` (prose-search
rework), `e890605` (wall-clock timing), `afaeafc` (CSV UTF-8 BOM). Reworked the relationship
pipeline around SerpWow AI-Overview searches per user direction.

- **Searches are now 3 parallel PROSE AI-Overview questions** (`query_builders.build_relationship_phase_queries`, still `(x_name, y_name, x_domain)`): phase1 relationship+URL (the user's proven "what is the financial relationship between X (domain) and Y? type out Y's website…" shape), phase2 financial-evidence, phase3 website-resolver — each asks for a **typed-out plain-text https:// URL, not a hyperlink** (extracted from the AI-overview text; `ai_overview_sources[].source_url` is the structured backstop). X identified by `name (domain)`, Y verbatim (OCR noise kept). Replaces the keyword queries. Parallel now; chaining (phase1→phase2 like gsearch phase5) deferred.
- **No unique-(X,Y) dedup** — `relationship_csv.parse_relationship_csv` now makes each CSV row its own "pair" (`source_row_indices == [idx]`); one row in → one row out. Downstream pair/fan-out machinery untouched (now trivially 1:1). CSV requires exactly `Input_URL`, `Company_Name_X`, `Company_Name_Y`.
- **Outputs renamed + split by RELATIONSHIP STATUS** (not URL presence): `confirmed_relation.csv` (`confirmed`) and `notconfirmed_relation.csv` (everything else = `not_confirmed` + `unclear` + any error, via the `!= "confirmed"` catch-all). `found.csv`/`notFound.csv`/`skipped.csv` gone for relationship (gsearch/gmaps still use found/notFound). `report.json` summary + `/status` + run-detail page still show BOTH count sets — `confirmed`/`not_confirmed`/`unclear` **and** `websites_found`/`websites_not_found` — just no found/notFound files.
- **Dedup-era UI/fields removed**: preview no longer returns `unique_pairs`/`duplicates`/`csv_rows` (adds `relationship: true`); `build_summary` drops `unique_pairs`/`searchable_rows`; run-detail drops the "Unique pairs" tile and blank-row "Skipped"; New Run launch gate uses `total_rows > 0` for all pipelines.
- Files touched: `query_builders.py`, `relationship_csv.py`, `serpwow_reporting.py`, `engine.py` (`_upload_file_links`/`_reporting_result_names`/`_GSEARCH_RESULT_FILES`/preview endpoint), `static/js/{new_run,run_detail}.js`, and the relationship test suite (queries/csv/endpoint/gates/reporting/worker + 2 `.mjs` DOM contracts).
- **"Processing time" fix (all SerpWow pipelines, not just relationship):** the run's `processing_seconds_total` was the **sum of per-row work times**, which overcounts massively under parallel workers (a 3-min run showed ~55m). Now it's **wall-clock** — `created_at` → the latest terminal timestamp (max row `status_updated_at` + `gemini_batch.completed_at`; stable, doesn't drift on later re-persists), live-elapsed to now while non-terminal (`_run_elapsed_seconds` in `engine.py`). `processing_seconds_avg` still shows the mean per-row work time. Fixed in both `summarize_upload_state` and `build_upload_output_payload`; regression test in `tests/test_timing_summary.py`.
- **CSV mojibake fix (`Äî`/`Üí` in Excel):** all CSV writers now use `encoding="utf-8-sig"` (UTF-8 **with BOM**) instead of `"utf-8"` — `serpwow_reporting.py` (×2 writers) + `ai_mode/run_reporting.py` (×2). Root cause: a UTF-8 CSV with no BOM was read by Excel/Numbers as Mac Roman, garbling every non-ASCII char (em dash `—`→`Äî`, arrow `→`→`Üí`, `↔`, curly quotes). BOM makes the viewer auto-detect UTF-8; data unchanged, and our reader already tolerates the BOM (`utf-8-sig`). Tests that read these CSVs updated to `utf-8-sig`.
- **Live smoke still needed** (needs SerpWow + worker): run `smallrel20.csv`; confirm the AI Overview actually triggers via SerpWow for the prose phrasing (the main risk — it fired in the user's browser but SerpWow may differ), that confirmed rows carry a typed-out URL, and that `confirmed_relation.csv`/`notconfirmed_relation.csv` + the 5 counts populate.

---

## Older session — 2026-07-20 (remove `full` + `url_discovery` pipelines, merged to `aiModeBroker` via PR #12)

**Status: code complete, 556/556 offline, NOT live-verified, NOT pushed.** Removed the two
legacy SerpWow pipelines completely; gsearch-all-phases supersedes them for URL discovery.
`firmographics`/`gmaps`/`gsearch`/`relationship` + AI-mode are the surviving pipelines.

What changed:
- **Deleted** `services/serpwow/modes/full.py` (the shared `execute_company_lookup` executor — no
  other module imported anything from it) and the two full-only ops scripts
  (`scripts/push_processed_rows_to_gemini_batch.py`, `scripts/requeue_wait_and_push_remaining_to_gemini_batch.py`).
- **Constants** (`serpwow/constants.py`): removed `PIPELINE_FULL` + `PIPELINE_URL_DISCOVERY`.
- **Routes** (`engine.py`): removed `GET/POST /crawl`, `POST /crawl/url-discovery`,
  `POST /uploads` (the bare full creator) and `POST /uploads/url-discovery`. Kept
  `/crawl/firmographics`, `/uploads/firmographics|gmaps|gsearch|relationship|ai-mode`.
- **`PIPELINE_FULL` purged, no replacement default** (per user: pipeline is always set explicitly
  at upload, so the old `... or PIPELINE_FULL` sentinel never fired on a real run). All ~24 sites
  became `... or ""`; the **worker dispatch `else` now `raise ValueError(f"unknown pipeline …")`**
  (fail-loud) instead of routing to the removed executor. Removed the `full`→`ENABLE_GEMINI_BATCH_POSTPROCESS`
  branch in `_batch_postprocess_enabled_for` (that env flag is now dead — dropped from `.env.example`).
  Trimmed full/url_discovery from the 3 pipeline allow-list sets and `notify._PIPELINE_LABELS`.
- **Frontend**: dropped the "Upload Console" card in `new_run.js`, the two entries in `ui.js`
  (`PIPELINES`/`PIPELINE_LABELS`) and in `operations.js` `HISTORY_PIPELINES`.
- **Tests**: no dedicated test files existed; updated shared tests that used `"full"` as an example
  to a surviving pipeline, deleted `test_full_uses_enable_flag`, updated 2 `.mjs` DOM contracts.
- **Docs**: `CLAUDE.md`, `readme.md`, this file. (Left the immutable applied SQL migration comment and
  the historical `impplan.md` as-is.)

Accepted consequence: legacy `full`/`url_discovery` runs already in S3/Supabase are **not migrated** —
they render with the raw pipeline key as label and their old S3 folder path; a replayed legacy queue
message fails loudly. Live smoke still needed: New Run has no "Upload Console" card, and a `gsearch` +
`firmographics` run still complete end-to-end.

---

## Recent session — 2026-07-20 (relationship search-query regression fix + Runs-page pipeline dropdown, branch `aiModeBroker`)

Two independent fixes.

### 1. Relationship mode found 0 websites on every row — search-query regression (committed `4bd3b03`)

**Root cause:** commit `6c4df1c` (2026-07-15, "Improve relationship search evidence and reporting") rewrote `build_relationship_phase_queries` (`serpwow/query_builders.py`) from keyword/boolean Google queries into three ~300-char **prose "AI-Overview question"** strings — but the transport (`serpwow_client.run_serpwow_search`) still sends the query as `q=` to `engine=google` **organic** search. Google returns ~0 `organic_results` for a prose paragraph, so the candidate-URL pool in `modes/relationship.py` collapsed to ~0 on every row. With no candidates, the LLM gate (picks `official_website` **from candidates only**) can never emit → `websites_found` always 0. The gate/counting/CSV logic was all correct; only the query shape was wrong.

**Fix:** `build_relationship_phase_queries(x_name, y_name, x_domain)` now emits keyword/boolean queries — phase1 `"{y}" official website` (direct site-finding, the recall win the pre-`6c4df1c` set lacked), phase2 `"{x}" "{y}" investment OR portfolio OR funding OR acquisition OR investor OR backed`, phase3 `"{x}" "{y}" site:{x_domain}` (dropped when the Input_URL yields no host). Requires **both x and y** (blank X can't confirm a relationship — the executor short-circuits it via `REL_ERROR_NO_X` before the LLM, so a Y-only search is wasted). `include_ai_overview=true` still fires, so overview evidence keeps flowing as a bonus; precision is left to the gate. Call site in `modes/relationship.py` now passes the already-computed `x_domain`. Tests updated: `tests/test_relationship_queries.py`, `tests/test_relationship_worker.py`. **NOT live-verified** — needs a small relationship CSV smoke (confirm `websites_found > 0` and per-phase `candidate_count > 0` in `run.log`).

Deliberately **out of scope** (noted, not done): the LLM JSON parsers (`ai_mode/gemini_batch.parse_json_from_text`, `serpwow/gemini_llm._parse_json_from_text`) only strip code fences then `json.loads`, silently becoming not-confirmed on any wrapping — a latent second 0-found path if `GEMINI_BATCH_MODEL` moves to a gemini-3.x thinking model that wraps JSON.

### 2. Runs page pipeline-filter dropdown missing `relationship` (UNCOMMITTED, JS only)

`static/js/runs.js` kept its **own** copy of `PIPELINES`/`PIPELINE_LABELS` that omitted `relationship`, so the pipeline filter dropdown had no "Financial Relationship" option (couldn't filter those runs; a `?pipeline=relationship` hash silently reset to "All") and the runs table showed the raw `relationship` key. Root cause was duplicated label maps drifting from `run_detail.js`/`new_run.js`. **Fix:** moved `PIPELINES` + `PIPELINE_LABELS` + `pipelineLabel()` into `static/js/ui.js` as the single source of truth; `runs.js` and `run_detail.js` now import them (each dropped its local duplicate). `new_run.js` keeps its own card labels (different UX vocabulary — "AI Mode 1 - Bulk" etc.). Verified via `node --check`; browser smoke still pending (needs Supabase + broker to render the Runs list).

---

## Older session — 2026-07-14 (AI Mode → RabbitMQ broker rework, branch `aiModeBroker`)

**Status: code complete (3 commits), 532/532 offline, NOT live-verified, NOT pushed.** Full architecture now documented in `CLAUDE.md` (AI Mode engine section). Motivation: 500k–1M-row recurring runs — the old in-process `run_ai_mode_sync` held every scrape payload + all results in RAM (OOM at scale), had no redelivery (a crash meant a manual whole-run re-drive), and a hard kill left runs showing `running` forever with the "Rerun failed" button unreachable.

### What changed (by commit)

1. **PR1 — streaming/memory** (`perf(ai_mode): stream Phase-3 assembly…`): `StreamingRunReport` (incremental CSVs + outcome counters + `AI_MODE_REPORT_ENTITIES_MAX` cap), `classify_one_result` extraction, `scrape_batch_sync` extraction (idempotent one-batch scraper, returns metadata only), per-batch disk reads in Phases 2/3, throttled status flushes. Memory O(one batch).
2. **PR2 — broker engine** (`feat(ai_mode): RabbitMQ broker engine…`): `ai_mode/broker.py` (own channel/QoS, durable `ai_mode_jobs` queue), `ai_mode/worker.py` (producer `publish_run_batches` + consumers with SerpWow's ack-after-persist/poison policy + file-presence idempotency + recount barrier that dispatches `run_ai_mode_finish` exactly once), `run_ai_mode_finish` extraction (Phases 2+3, never-raises, rebuilds Phase-1 records from disk), router 503 gate + background publisher.
3. **PR3 — reconciler + resume + legacy removal**: `reconcile_ai_mode_runs` (startup + periodic; republish-missing with `AI_MODE_BATCH_MAX_REQUEUE` cap then error-marker terminalization; finish re-dispatch; **phantom-running flip** for legacy runs so "Rerun failed" appears), Gemini-poll heartbeat, resume endpoint rework (clears `*.error.json` + S3 mirrors via new `s3_sync.delete_mirrored_file`, clears stale bookkeeping, republishes only missing batches), **deleted `run_ai_mode_sync`** + the interim `AI_MODE_ENGINE` flag (broker is the only path), `tests/ai_mode_drive.py` offline harness, `.env.example` §3f + `CLAUDE.md` updates.

### Live smoke checklist (before merge)

1. `docker compose up -d rabbitmq`; start API + `python worker.py` (restart both). Small `ai_deep` upload (10 rows → 4 batches): `ai_mode_jobs` visible in mgmt UI with consumers; raw files land; phase publishing→scraping→cleaning; outputs/Slack/Supabase as before.
2. `kill -9` the worker mid-scrape → restart → redeliveries skip existing raw files, run completes (no scrape.do re-spend — check run.log).
3. Kill mid-Gemini-batch (`AI_MODE_LLM_BATCH=true`) → restart → reconciler re-dispatches finish, `cleaned/` reused.
4. Stop the worker entirely → after `AI_MODE_BATCH_STALE_TIMEOUT_SEC` the reconciler republishes/terminalizes; "Rerun failed" completes the run.
5. Broker stopped → AI-Mode upload 503s; old completed runs still render.
6. At 100k+: worker RSS flat; status.json stays ~2KB; scrape.do 429s at high `AI_MODE_WORKER_CONCURRENCY`; found+notFound row counts == total_rows; `final_report.json` has `entities_omitted:true`.

---

## Older session — 2026-07-13 (Midnight Ledger UI/UX + Slack cost precision, branch `uiuximp`)

**Status: COMPLETE and reviewed.** The UI has been fully redesigned in the approved "A - Midnight Ledger" direction: an elegant, lower-density dark interface with clearer hierarchy, fewer boxed metrics, outcome-first run details, responsive navigation, accessible controls, and consistent shared primitives. Final branch review found no remaining Critical/Important issues. Suite **487/487**; notification-focused suite **28/28**.

### UI/UX redesign

- **Global shell/theme:** Midnight navy surfaces, warm off-white text, teal/cyan primary accent, amber warnings, red errors, restrained borders/shadows, consistent typography/spacing, visible `:focus-visible`, reusable button/card/table/status/empty-state primitives. Desktop sidebar becomes an accessible mobile drawer (toggle, backdrop, Escape close, focus restoration).
- **Whole-site migration:** Dashboard, Companies, New Run, Runs, Run Detail, Operations, and Tools share one visual language. Stale light-theme utility classes and emoji-as-interface-icon patterns removed. Tables scroll within their containers instead of widening the page.
- **Run Detail (priority area):** Outcome before telemetry. Reporting pipelines show Websites found / Not found / Errored; generic legacy pipelines show Succeeded / Failed. Technical metrics (time, average/row, tokens, cost, batch status) are secondary. Human-readable pipeline names, run ID, created/updated timestamps. Files use one accessible View/Download surface with a keyboard-safe modal (focus trap, Escape/backdrop close, abort/race cleanup, mobile-contained actions).
- **Responsive verification:** Headless-Chrome matrix, **8 views x 4 widths = 32 checks** at 375/768/1024/1440 px — zero page-overflow/focus failures.
- **Accessibility:** Operations storage-copy controls are native keyboard-operable buttons with labels; modal actions exempt from the mobile full-width rule; polling/request cleanup prevents detached-DOM updates; New Run preview/modal state is inert and race-safe.

### Operations and batch lifecycle hardening

- `/batch/jobs` correlates top-level and chunk jobs across `full`, `gsearch`, `gmaps`, `relationship`; Operations sends `upload_id` plus an optional expected generation.
- Cancel/delete updates local state, invalidates the remote-list cache, preserves sibling chunk state, rejects stale Operations actions after an explicit retry.
- Deletion uses durable local/S3 tombstones plus batch generations so a stale worker snapshot cannot resurrect deleted jobs or overwrite a newer retry generation.
- `cancel_requested` remains nonterminal for reporting/finalization. Reporting-file scans gated to terminal, batch-settled status instead of adding S3 latency to every 2s active-run poll.

### Slack split-cost precision

SerpWow terminal notifications now receive separate LLM and SerpWow costs plus total. `app.core.notify._fmt_usd` prevents sub-cent values from collapsing to `$0.00`: normal `$1.23`; sub-cent up to six decimals with trailing zeros trimmed (`$0.00105`); signed zero `$0.00`; sub-micro explicit thresholds. Tip commits: `e8ba02a` (adaptive precision), `504cd07` (signed zero), `7a96b35` (sub-micro thresholds).

---

## Older history

All dated sessions before 2026-07-13 (error-taxonomy, relationship pipeline, engine decomposition, gmaps/gsearch reworks, S3 layout, AI Mode Gemini-batch, the original rework, environment notes, outstanding-decisions log) are archived newest-first in **`docs/HISTORY.md`**.
