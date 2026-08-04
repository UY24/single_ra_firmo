# HANDOFF — `website_url_finder`

Last updated: 2026-08-03. Read this first if you're picking up this repo. Durable architecture (module map, S3 layout, pipeline internals) lives in `CLAUDE.md`; older dated sessions are archived in `docs/HISTORY.md`.

---

## Current status

- **Active branch: `migrateserp2scrape`.** Two independent bodies of uncommitted work sit in this tree — keep them separate when committing:
  1. the **2026-08-03 gmaps → scrape.do migration** (this session, see below);
  2. the older **2026-07-23 relationship** failure-diagnostics/retry/UI changes, still **UNCOMMITTED, NOT live-verified, NOT pushed** (they predate this branch, from `relationship-ai-overview` off `aiModeBroker`).
- Full suite: **633/633** passing (was 564 before this work added 69).
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

## Latest completed session — 2026-08-03 (gmaps: SerpWow Places → scrape.do Google Maps)

**Status: UNCOMMITTED, 633/633 offline tests passing, PARTIALLY live-verified (real API
calls through the real executor; NOT yet a full worker/RabbitMQ run), NOT pushed.**
First pipeline migrated off SerpWow. Built with Ponytail: one new client file, **one new env var**
(`SCRAPEDO_CONCURRENCY`), no new dependency, no new endpoint, no new UI component (the existing `costSection`
turned out to be already parameterized), and one dependency plus two debug routes *deleted*.

### What changed and why

SerpWow used **two** calls per gmaps row: `search_type=places` to harvest `data_cid`s, then
one `place_details` per CID (`1+N`, up to 21 requests) — only because `fetch_data_cids`
threw away everything but the CID. scrape.do's `/plugin/google/maps/search` returns
`website`/`title`/`address`/`phone`/`rating`/`reviews`/`type` **inline per `local_results[]`
entry**, which is the entire set this pipeline reads. So gmaps is now **one request per row**.

- **NEW `serpwow/scrapedo_maps_client.py`** — `process_gmaps_query(q, gl, client=None)`
  returning the same envelope the scorer/reporting already expect, plus the call/credit
  accounting. **No gmaps-specific env keys**: it shares
  `SCRAPEDO_TOKEN`/`SCRAPEDO_TIMEOUT_SECONDS`/`SCRAPEDO_MAX_RETRIES` with AI Mode, and reuses
  `outcomes.categorize_http_error` plus the retry shape from
  `serpwow_client.run_serpwow_search` (429/5xx/transport only — see the retry pass below for
  the final backoff policy). Errors are **returned, not raised**. The token is redacted from
  all error text. `results` is `local_results` **verbatim** (it's persisted as the row artifact).
- **`/plugin/google/maps/place` is deliberately unused.** Empirically verified before coding
  against real CSV rows: a `local_result` with no `website` has no `website` in `/maps/place`
  either (checked `AMIGO CORPORATION`, `data_cid=15915247393667579758`), so a hydration pass
  would recover nothing. Sample rows: a Turkish SME returned 1 result **with** website; a
  Bangladeshi row returned 17 results, 11 with websites (the website-less ones were other
  companies entirely, which `_score_gmaps_candidates` skips by design).
- **Credits, not USD, and zero SerpWow residue.** 10 credits per **successful** call (failed
  attempts are free, so retries are cheap). A gmaps row's `cost_breakdown` is exactly
  `{scrapedo_requests, scrapedo_successful_requests, scrapedo_failed_requests,
  scrapedo_credits, gemini_cost_usd, total_cost_usd}` — no `serpwow_*`
  or `massive_proxy_*` keys — and `CrawlResponse.serpwow_cost_usd`/`massive_proxy_cost_usd`
  are now `Optional[float] = None`, so gmaps leaves them unset instead of reporting a
  meaningless `$0.00` (their XLSX columns come out blank for gmaps; the other three pipelines
  still pass real floats). `gemini_cost_usd`/`total_cost_usd` **stay** because
  `GMAPS_CONFIDENCE_MODE=llm` is a genuine USD cost.
  `build_summary` **routes on the presence of `scrapedo_*` keys** and skips SerpWow
  accounting for those rows. That routing is load-bearing: a gmaps row carries a
  `formatted_results` entry for its one call, so without it the billable-count fallback
  infers 1 billable search and prices every row at `SERPWOW_USD_PER_SEARCH` — regression test
  `test_formatted_results_cannot_infer_billable_serpwow_searches`. It doubles as the
  back-compat path, since a pre-migration gmaps run has no `scrapedo_*` keys, takes the
  SerpWow branch, and keeps its old cost card (`run_detail.js` likewise branches on the
  data, not the pipeline key). Slack shows "N requests · N credits".

### Retry / 502 / call-accounting pass (same session, after a 100-row live run)

A 100-row live run came back `completed_with_errors` with **12 rows failing HTTP 502**.
Reading its `state.json` showed retries were already firing (`scrapedo_requests` was 3 on
exactly those 12 rows) and credits were already 200-only (880 = 88 × 10). The real finding
was in the 502 **body**: `{"error": "no results"}`.

- **`502 "no results"` is Google having no Maps listing — a business not-found, not a
  failure.** It is now matched (`NO_RESULT_MARKERS`), returns `results=[]`,
  `no_results=True`, **no error and no retry**, and the row classifies `not_found` with
  `row_error = "No Google Maps listing exists for this company."`. Replaying those exact
  12 queries confirms it: 1 attempt instead of 3, 0 credits, no error. Previously they
  were `outcome=error`, which inflated the error count, put them under "View failed
  rows", and made them eligible for a "Rerun failed" that could never succeed.
- **Retries stay on the shared `SCRAPEDO_MAX_RETRIES`** (currently `2` → 3 calls/row).
  A gmaps-specific `GMAPS_MAX_RETRIES` was built and then **removed per user direction**:
  one scrape.do retry knob for the whole app beats a second env var. Consequence to know:
  gmaps and AI Mode now share a retry policy, so raising it moves both, and gmaps gets
  2 retries rather than the 3 originally discussed. Accepted tradeoff.
- **429 backs off much harder than 5xx.** A second run showed **27/50 rows failing on
  HTTP 429** with all attempts exhausted — a 1s/2s ramp is useless against a rate limit.
  Now 1/2/4s for 5xx vs **5/10/20s** for 429, `Retry-After` honored when sent, everything
  capped at `MAX_BACKOFF_SECONDS` (30s) so a hostile header can't park a worker.
  If 429s persist, the next lever is `WORKER_CONCURRENCY` vs the scrape.do plan's
  concurrency cap — not more retries.
- **Explicit call accounting.** The envelope and `cost_breakdown` now carry
  `scrapedo_requests` (every attempt), `scrapedo_successful_requests` (HTTP 200s, the only
  billed ones) and `scrapedo_failed_requests`, with `credits` **derived** as
  `CREDITS_PER_CALL × successful_requests` rather than incremented by hand. A run
  reconciles as **requests == succeeded + failed** (enforced by a test that sweeps
  success/500/429/no-results). `report.json`, the `run.log` cost line
  (`scrapedo_requests=136 (ok=88 failed=48) scrapedo_credits=880`), the Slack ping and the
  run-detail card ("880 credits · 48 failed") all surface the split.
- Guarded a subtlety while wiring the UI/Slack switch: `_build_cost` emits the
  `scrapedo_*` keys as **0 for every SerpWow pipeline too**, so the "is this a scrape.do
  run?" test must be **truthy**, not `is not None` — otherwise gsearch would have
  rendered as Scrape.do. Checked against requests/credits/failed so an all-failed run
  still shows the right card.

- **Two more pre-existing bugs fixed on the way** (found by tracing, not reported):
  1. **A provider failure was silently a business "not found".** `process_gmaps_query` returned
     `{"error": …}` on a missing key / HTTP failure and `run_gmaps_from_module` dropped it, so
     `classify_finalized_row` (which reads `context["formatted_results"]`, a key gmaps never
     set) fell through to `not_found`. A rate-limited or mis-keyed run reported "no website
     found" on every row, was invisible in the error breakdown, and could not be retried
     because nothing was marked `failed`. Now the error propagates and the executor writes a
     one-entry `formatted_results` tagged `error_source: scrapedo`; `_phase_stats` returns the
     phase's declared source so attribution isn't hardcoded to SerpWow. Genuine zero-result
     rows stay `not_found`.
  2. **`SCRAPEDO_TOKEN` was missing from `tests/__init__.py`'s hermeticity guard** — a real
     hole (AI Mode already read it) that a gmaps row-path test could have used to hit live
     scrape.do from a developer `.env`. Added.
- **Result ordering restored:** `position` order survives, where the old `set()` of CIDs
  randomized it and fed `_score_gmaps_candidates`'s `-0.01*idx` tiebreaker noise.
- **Deleted:** `serpwow/gmaps_client.py`, the `GET /gmaps/discover` + `GET /gmaps/details`
  debug routes (no `data_cid` step exists any more), and **`aiohttp` from `requirements.txt`**
  — that file plus `/gmaps/discover` were its only importers. `/gmaps/search` repointed at the
  new client with a `SCRAPEDO_TOKEN` guard. `POST /uploads/gmaps` now 400s on a missing token
  (checked **after** CSV validation, so a bad CSV still reports the CSV problem).
- **UI copy de-SerpWowed** (the pipeline card said "Fast SerpWow Maps discovery"): New Run's
  gmaps card now reads "Fast Scrape.do Maps discovery…", and Operations' table — which lists
  **every** non-AI-Mode pipeline, so a provider name was simply wrong — went from "SerpWow
  Uploads History"/"Showing all SerpWow uploads" to provider-neutral "Pipeline Uploads
  History"/"Showing all pipeline uploads" (4 strings). **Two `"SerpWow"` strings in
  `run_detail.js` were deliberately KEPT** after verifying they're still correct:
  `providerLabel = "SerpWow"` is only a default (gmaps/AI Mode override it with
  "Scrape.do"), and "Rows where SerpWow returned 200…" sits in `emptyResponsesSection`,
  which is unreachable for gmaps because `empty_response_breakdown()` returns `None` for
  anything but gsearch/relationship. Renaming either would have made the UI wrong for the
  three pipelines still on SerpWow.

- **Files:** `scrapedo_maps_client.py` (new), `modes/common.py`, `modes/gmaps.py`,
  `outcomes.py`, `serpwow_reporting.py`, `schemas.py`, `engine.py`, `core/notify.py`,
  `static/js/{run_detail,new_run,operations}.js`, `requirements.txt`, `.env.example`,
  `CLAUDE.md`; tests `test_scrapedo_maps_client.py` (new, 22), `test_gmaps_llm.py`,
  `test_gsearch_cost.py`, `test_timing_summary.py`, `tests/__init__.py`.
- **Verification done:** 606/606 offline tests, all 6 `.mjs` DOM contracts, `node --check` on
  every JS module. Plus **live single-row runs through the real executor** (not just mocks):
  a Turkish SME resolved `https://abcsacmetal.com.tr/` with `industry` populated from
  `types`, 1 request / 10 credits, `serpwow_cost_usd=None`; and the 12 previously-502 rows
  replayed as `no_results=True`, 1 attempt, 0 credits, no error.

### Throughput / concurrency pass (same session)

Investigated "can we use scrape.do's 100 concurrent calls". **The provider was never the
bottleneck — `state.json` was.** `update_row_state` rewrites the WHOLE state file per row
under a per-upload lock, so throughput was capped by state size no matter what
`WORKER_CONCURRENCY` was set to. Measured ceiling: **41 rows/s at 100 rows, 4.3 at 1k,
0.4 at 10k** — raising concurrency to 100 would have left ~96 sockets idle behind the lock.

- **Dropped the duplicated provider payload from persisted state** (the fix). gmaps'
  `context.gmaps.raw_response` was **36.6 of every 39.2 KB**, and gsearch inlined one per
  phase (up to 5/row). Both are already persisted as the row's `serpwow_response/`
  artifact and nothing reads them back from state; the in-process consumers (scoring, the
  Gemini selector) use the live object. Row: **39.2 KB → 2.2 KB (94% smaller)**. Ceiling at
  1k rows: **4.3 → ~76 rows/s**, comfortably above the ~28 rows/s that 100 concurrency
  needs. **Good to roughly 2.7k rows**; past that the per-upload lock binds again.
- **`_write_json` is now atomic** (temp + `os.replace`) and runs via `asyncio.to_thread`.
  It was a bare `path.write_text`, so a crash mid-write truncated `state.json` and lost
  **the whole run's** progress, not one row's — and at 1k rows it blocked the event loop
  for 115 ms per row while other rows' HTTP waited.
- **`common/provider_limits.scrapedo_slot()`** — ONE scrape.do concurrency gate
  (`SCRAPEDO_CONCURRENCY`, default 100) shared by **gmaps and AI Mode**, since the cap is
  per *account* and both run in the same worker process. Mirrors the pre-existing
  `search_fetch_semaphore` for SerpWow. It's a concurrency cap only — if scrape.do also
  enforces requests/second (suspected: 429s were seen at only 12 concurrent), a token
  bucket goes inside that one helper and no call site changes. User is confirming the
  plan's actual limits.
- **One knob to set, not three.** `AI_MODE_WORKER_CONCURRENCY` now falls back to
  `WORKER_CONCURRENCY` when unset, and `SCRAPEDO_CONCURRENCY` defaults to 100 — so a
  deployment sets only `WORKER_CONCURRENCY=100` and can delete the other two from `.env`.
  The two overrides remain for when AI Mode must differ (its messages are ~50s batches vs
  a ~3.5s gmaps row). The provider cap deliberately does NOT track the slot count: if it
  did, raising worker slots would silently raise the vendor limit. Verified `100 rows /
  batch 10 = 10k calls` goes from ~28h at concurrency 5 to ~1.4h at 100, and that 100 is
  safe — the thread pool is already sized from these env vars (a stock `to_thread` cap of
  `min(32, cpu+4)` would otherwise have throttled it), payloads are 5-21KB so 100 in
  flight is ~2MB, and `AI_MODE_JOB_TIMEOUT_SEC=600` has 12x headroom over ~51s calls.
- **Known minor inefficiency introduced:** AI Mode's retry loop lives inside its
  *synchronous* client (`time.sleep` between attempts), so the slot wrapped around
  `to_thread(scrape_batch_sync)` is held across backoff sleeps — a slot occupied while not
  calling. gmaps doesn't have this (only its HTTP call is wrapped). Negligible unless 429s
  are frequent; the fix is hoisting the retry loop out of the sync client.
- **Corrected an earlier wrong claim of mine:** raising `WORKER_CONCURRENCY` does NOT
  expose gsearch to ~500 concurrent SerpWow calls — `SEARCH_FETCH_CONCURRENCY=5` already
  gates SerpWow globally. So the two-dial model (worker slots vs per-provider caps) was
  already half-built; only the scrape.do half was missing.
- **`scrapedo_billed_empty`** — counts HTTP **200s that returned zero results**, i.e.
  credits spent for no data (the scrape.do refund claim). Deliberately distinct from the
  free 502 "no results". Flows row → `cost_breakdown` → `report.json` → `run.log`.
  **Measured on the 100-row run: 0 refundable rows** (66 found, 22 billed-with-results-but-
  no-website, 12 free 502s) — so gmaps is not leaking money. The `scrapedo_empty_requests/`
  folder in the repo root is **AI Mode**, not gmaps: that's where the real empty-response
  spend is, and it's untouched by this work.
- **Deleted the dead `redis` dependency** (imported nowhere), alongside `aiohttp` earlier.

### Live-run findings + latency pass (2026-08-04)

Ran 100 gmaps rows at `WORKER_CONCURRENCY=100`: **134s wall, 28.6s median per row**, when
100 concurrent rows at 3.5s each should be ~4s. Diagnosed from the run's own state.json.

- **scrape.do is the ceiling, and it QUEUES instead of 429ing.** Every row: exactly
  **1 attempt, zero errors, zero 429s** — so not retries, not our backoff, not rate-limit
  rejection. But a single call inflated **3.5s → 28.6s** under load, peak in-flight was
  **60 not 100**, and the delivered rate was **~0.88 calls/s**. Client concurrency above
  the provider's real capacity becomes latency, not throughput. **Consequence: the 500k
  estimate is ~158 hours (6.6 days) at the observed rate, NOT the ~4.9h I projected from
  isolated latency.** Open question for scrape.do: what the Maps endpoint's actual
  throughput is on this plan — "100 concurrent" plainly isn't 100 served at once.
  **Do not raise concurrency further**: rows already hit 43s against
  `SCRAPEDO_TIMEOUT_SECONDS=90`, so more queueing turns slow rows into failed ones.
  Cheap next experiment: same 100 rows at `WORKER_CONCURRENCY=25`. If wall time is
  unchanged, 25 is strictly better (same throughput, 4x lower latency, no timeout risk).
- **Shared pooled `httpx.AsyncClient`** (was one per row): measured **636ms → 233ms** per
  call, i.e. ~400ms of DNS/TCP/TLS setup per row and ~55 hours across 500k. Keepalive
  sized to `SCRAPEDO_CONCURRENCY`; closed in `shutdown_event`. Tests assert the pooling
  holds — every other test injects its own client, so this path had **no coverage** and
  the first version shipped with a `NameError` that the suite couldn't catch.
- **`state.json` S3 mirror throttled** to `SERPWOW_S3_STATE_FLUSH_SEC` (5s). It was re-PUT
  to the same key twice per row: ~**84MB of near-identical uploads per 100-row run**,
  which is what made my own S3 benchmark time out on uplink. Local disk still written
  every time; **terminal snapshots always mirror**. boto3 retries now `adaptive` in both
  clients (AWS's documented answer to SlowDown). Both were already recommended in the
  2026-07-23 notes below.
- **`/uploads/ai-mode/...` 404 on every page load** (user-reported) was a deliberate probe:
  a run id alone doesn't say which engine owns it. Views that know the pipeline now pass
  `?engine=ai|serpwow` (`ui.runHref`/`engineOf`), so the right endpoint is tried first.
  The probe **stays as a fallback** — bookmarked/typed URLs carry no hint and a stale hint
  must still resolve. `render()` was restructured into an ordered candidate list.
- Measured and **ruled out** as causes: S3 PUT latency (317ms, ~1% of a row), boto3
  connection pool (already 100), the publish loop (no per-row persist), the per-upload
  state lock (~5ms at this size).

### 502 retry + error-classification (2026-08-04, per user direction)

- **All 502s are retried now** (3 retries, exponential backoff). Earlier a 502 whose body
  said `"no results"` was treated as terminal, because replaying 4 such queries returned
  no-results every time. User's call to retry anyway: 502 can be genuinely transient and
  failed attempts are unbilled, so the only cost is latency. **Accepted trade: the ~12%
  of rows with no listing now burn 4 attempts + 7s of backoff each, so runs get ~30%
  slower.** If a run still reports the same `rows_no_listing` count as before, the retries
  bought nothing and reverting is worth considering.
- **A row that recovers after a 502 is NOT an error.** Each row's failed attempts are
  bucketed by the row's final state: `scrapedo_recovered_requests` (row succeeded — never
  an error), `scrapedo_error_requests` (row failed after every retry — the only real
  errors), remainder = attempts on a no-listing row. The UI's failed badge and the
  `run.log` `errors=` figure both use `scrapedo_error_requests` only.
  `run.log`: `scrapedo_requests=120 (ok=95 recovered=5 no_listing=12 errors=8) scrapedo_credits=950`.

### Live tuning result: LOWER concurrency is faster

| `WORKER_CONCURRENCY` | wall (100 rows) | per row | throughput |
|---|---|---|---|
| 100 | 134s | 28.6s | 0.75 rows/s |
| **25** | **70.5s** | **10.8s** | **1.42 rows/s** |

Over-subscribing scrape.do inflated per-call latency 2.6x and made the run 1.9x **slower**.
Confirms the queueing diagnosis. **Keep 25**; test 10 to find the knee. At 1.42 rows/s
that is ~5.1k rows/hour, so 500k ≈ 98 hours.

Also clarified for deployment: **one worker PROCESS is not one concurrent request.** The
worker is async — a single process held 60 scrape.do calls in flight. The one-worker rule
exists because two processes would race on `state.json` (the lock is in-process only), not
because of throughput. One EC2 running API + worker with high `WORKER_CONCURRENCY` is the
correct shape.

### Still to do

1. **Full-stack live smoke** — the only thing not yet exercised (needs RabbitMQ + worker, and
   writes to production S3/Supabase/Slack, so it needs explicit approval). Run
   `samples/smalltest.csv` through gmaps and confirm: `run.log` shows
   `scrapedo_requests=<rows> (ok=… failed=…) scrapedo_credits=10×ok` with **one request per
   successful row, NOT `1+N`**; the found-rate is **comparable to a SerpWow gmaps run on the
   same CSV** (the acceptance gate — a materially worse rate would mean
   `local_results[].website` is sparser than the probe rows suggested); the previously-502
   rows now land in `notFound.csv` as `not_found` with "No Google Maps listing exists for this
   company." and the run reaches `completed` rather than `completed_with_errors`; the
   run-detail card shows "Scrape.do — N credits · N failed" with no `$`; an older gmaps run
   still renders its SerpWow cost card; and a deliberately bad token yields
   `error_source=scrapedo` / `error_category=auth` rows under "View failed rows" with 0 credits.
2. **Watch the 429s at scale.** The 50-row run lost 27 rows to rate limiting. The backoff is
   now 5/10/20s, but if it persists the lever is `WORKER_CONCURRENCY` (default 4) against the
   scrape.do plan's concurrency cap — **not** more retries.
3. **Raise `WORKER_CONCURRENCY` (currently 12) toward 100 and restart the worker** —
   consumers are built at startup, so it won't affect an in-flight run. Safe now that
   both providers are semaphore-gated. Then measure a 1k run.
4. **500k target needs the state store replaced.** Agreed direction (user decision):
   gmaps becomes its own small runner reusing AI Mode's *primitives* — `s3_sync`,
   `StreamingRunReport`, the broker/own-queue pattern, `status.json` O(1) counters,
   file-presence-as-state — but NOT AI Mode's `ModeConfig`/LLM-cleanup flow, which
   assumes a prompt-driven pipeline gmaps doesn't have. Design notes: message carries a
   BATCH of ~20 rows (500k rows → 25k messages, not 500k) but files are **per row**,
   since Maps search is one company per query — that's the one real divergence from AI
   Mode's 1:1 batch:file model. Also needs a cap on `report.json`'s per-row array
   (AI Mode caps entities at 50k; SerpWow's is unbounded) and sharded row directories
   (500k files in one dir/S3 prefix is a slow LIST).
5. Decide whether gsearch / relationship / firmographics follow (they still call
   `api.serpwow.com` via `run_serpwow_search` / `codetails.fetch_serpwow`). Naming decision
   taken: rename `serpwow_*` → `scrapedo_*` per pipeline as it migrates, package rename
   (`app/services/serpwow/`, `serpwow_reporting.py`, the `serpwow_summary` `/status` field,
   the `serpwow_response/` S3 prefix) **last**, once nothing is on SerpWow.
6. The pre-existing 2026-07-23 relationship work in this tree is still uncommitted and
   unverified — see the session below. This gmaps work is independent of it, so a commit
   should separate the two.

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
