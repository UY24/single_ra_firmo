# HANDOFF — `website_url_finder`

Last updated: 2026-08-04. Read this first if you're picking up this repo. Durable architecture (module map, S3 layout, pipeline internals) lives in `CLAUDE.md`; older dated sessions are archived in `docs/HISTORY.md`.

---

## Current status

- **Active branch: `relationship-scrapedo`.** Both the **2026-08-03 gmaps → scrape.do
  migration** and the older **2026-07-23 relationship** failure-diagnostics/retry/UI
  changes are now **COMMITTED** (`5158114`; the relationship changes landed via PR #13,
  `3a3ed0c`) — the "uncommitted, two bodies of work" note that used to live here is stale.
  NOT pushed. See the 2026-08-04 session below for the follow-on: `relationship` itself
  has since migrated off SerpWow onto scrape.do Google AI Mode.
- Full suite: **701/701** passing, plus 6/6 `.mjs` DOM contracts.
  ```bash
  cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
  ```
  (`-t .` is **mandatory** — it makes `tests/__init__.py`'s hermeticity guard run so real cloud creds from `.env` don't leak into tests.)

## What works now

Two independent discovery systems behind one FastAPI app + vanilla-JS UI (see `CLAUDE.md` for the architecture):

- **SerpWow-engine pipelines** — `firmographics`, `gmaps`, `gsearch`, `relationship` (the legacy `full`/`url_discovery` modes were removed 2026-07-20). They share one engine/queue/reporting layer, but two have since moved off SerpWow onto scrape.do: **`gmaps`** (2026-08-03, Google Maps API) and **`relationship`** (2026-08-04, Google AI Mode; S3-only run store, one RabbitMQ message per run — see the latest session). gsearch/firmographics still call `api.serpwow.com`. gsearch/gmaps have confidence scoring + `found.csv`/`notFound.csv`/`report.json`/`run.log` output parity, S3 mirroring, Supabase counters, and run-detail UI; relationship has its own summary-only `report.json` and confirmed/notconfirmed CSVs.
- **AI Mode** — `ai_bulk`, `ai_deep` (scrape.do -> LLM cleanup), **now broker-driven** (RabbitMQ scrape phase in the worker process, reconciler self-healing, streaming assembly, Gemini Batch API support). See the AI Mode section in `CLAUDE.md`.
- Cross-cutting: unified CSV input, per-company/per-pipeline S3 layout, Supabase run tracking, best-effort Slack completion/failure pings (with split LLM vs SerpWow cost), the redesigned dark UI across all views.

## Active risks / unfinished work

- **gmaps is production-ready only up to ~2.7k rows.** `update_row_state` rewrites the whole `state.json` per row, and `report.json` embeds every row. The 500k target needs the state store replaced — see "Still to do" in the latest session.
- **scrape.do's Maps endpoint is the throughput ceiling, ~1.4 rows/s** (measured), and it *queues* instead of 429ing, so raising concurrency makes runs SLOWER. `WORKER_CONCURRENCY=25` beat 100 by 1.9x. Do not tune this upward without re-measuring.
- ~~Nothing in the latest session is committed~~ **stale**: the gmaps migration and the 2026-07-23 relationship changes are both committed (`5158114`, PR #13). See the 2026-08-04 session for what came after.
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
cd backend && ../.venv/bin/python -m unittest discover -s tests -t .   # 701 tests

# UI contract tests are SEPARATE and not part of the unittest run — run them too after
# touching any static/js file, or JS regressions ship silently.
cd backend && for f in tests/*.mjs; do node "$f" || echo "FAIL $f"; done   # 6 contracts
```

## Immediate next steps

See the 2026-08-04 relationship-migration session below for its own live-verification
checklist. Carried over from the 2026-08-03/04 gmaps session:

1. **Validate the 502-retry trade** and **test `WORKER_CONCURRENCY=10`** — both are single
   100-row runs and both change tuning decisions.
2. **Verify Slack + Supabase on a terminal gmaps run** — the only pipeline parts never
   confirmed live.

Still open from earlier sessions: live-smoke the 2026-07-23 relationship changes (including
a literal `ERROR:`/`FETCH_ERROR:` Company Y value, which must be processed verbatim), and
live-smoke `aiModeBroker` (checklist in its session notes below) — the latter matters more
now, because the 500k plan builds gmaps on AI Mode's primitives.

## Conventions (do not break)

- **Always use ponytail for coding** — the `ponytail` plugin/skill (installed: `ponytail@ponytail`) is the required mode for any coding work here (writing, refactoring, reviewing, choosing deps). Laziest solution that actually works: question whether it needs to exist (YAGNI), reuse what's here, stdlib/native before new deps, shortest correct diff — but never shortcut understanding the problem first.
- **Never** add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits — tell any committing subagent the same.
- **Never** `git push` or query the production Supabase DB without explicit user approval.
- `docs/` is **gitignored** — specs/plans/handoff-history there are on-disk only; code under `backend/` commits normally.

---

## Latest completed session — 2026-08-04 (relationship: SerpWow → scrape.do Google AI Mode migration)

**Status: 701/701 offline tests + 6/6 `.mjs` DOM contracts passing on `relationship-scrapedo`.
NOT yet live-verified with a real run — see the checklist below before trusting this at
scale.**

The second pipeline off SerpWow (gmaps was first, previous session). `relationship`
verifies an X↔Y financial relationship and emits Y's website only when confirmed; it now
does that with **one** scrape.do Google AI Mode call per row instead of three SerpWow
phase-query calls. The LLM gate/verdict logic (`build_relationship_prompt` /
`apply_relationship_gate` / `update_relationship_block`) is untouched — only the evidence
source changed, from three SerpWow AI-Overview responses to one AI Mode response's
`references[]` + typed-out URLs in `text_blocks`.

### Why S3-only, no `state.json`, no local disk

Every other SerpWow-engine pipeline keeps a per-run `state.json` on local disk (mirrored to
S3 as a backstop). Relationship skips that entirely: S3 object presence **is** the state
(a row is done when `raw/<idx//1000>/row_NNNNNN.json` exists, dead when its `.error.json`
sibling exists, verdicted when `cleaned/…` exists). The reasoning: the target is 500k rows
on a disposable EC2 instance, and disposable means there is **nothing to rehydrate** — if
the box dies or gets replaced, a fresh one just re-LISTs S3 and keeps going. A local
`state.json` would need its own resume/rehydrate machinery (which AI Mode has, because it
does keep local files); skipping local disk entirely means that machinery doesn't need to
exist for this pipeline at all. `status.json` is kept, but only as an O(1) counter cache —
the real barrier check is a fresh paginated LIST building an in-memory index set, not a
counter read. `relationship_store.put_object` **raises** on failure (unlike
`s3_sync.mirror_file_to_s3`, which swallows), because with S3 as the only copy, a silently
swallowed PUT would lose the row's work permanently — there is no local copy to fall back to.

### Why the consumer acks on receipt (the repo's only ack-before-persist inversion)

Every other queue consumer in this repo acks only after the work is durably persisted
(at-least-once, safe to redeliver). Relationship inverts this: the RabbitMQ message is one
per **RUN** (not per row), and a run can take hours. RabbitMQ's `consumer_timeout` (30
minutes) would kill and redeliver a message that's still legitimately in flight past that
mark, tearing down a multi-hour run for no reason. Acking on receipt sidesteps that, at the
cost of losing at-least-once delivery for the message itself — durability is recovered
instead through `redrive_stale_runs`, a periodic scan (`RELATIONSHIP_REDRIVE_SCAN_SEC`,
300s) that re-drives any run whose S3 state hasn't advanced in `RELATIONSHIP_STALE_SEC`
(900s), which also covers a worker that was down at publish time or got replaced mid-run.

### App-wide change: `SCRAPEDO_MAX_RETRIES` 2 → 3

Moved to give the relationship pipeline's AI Mode calls a 4th attempt. This is **not**
relationship-scoped — it's the single scrape.do retry knob shared by gmaps and AI Mode's
scraper too (see `.env.example` / `CLAUDE.md`), so all three now get 4 total calls per row
instead of 3.

### Two numbers that are NOT proven yet

- **`SCRAPEDO_CONCURRENCY=100`** — this is a target, not a measurement. The gmaps
  session (above) measured scrape.do's **Google Maps** endpoint running 100 concurrent
  **1.9x SLOWER** than 25, because scrape.do queues rather than 429ing (it does not reject
  overload, it just gets in line). Whether the **AI Mode** endpoint behaves the same way is
  unverified — re-measure on a live run before trusting 100 for this pipeline. Note this is
  the shared per-account vendor cap: relationship deliberately has no concurrency knob of
  its own, so tuning this moves gmaps and AI Mode too.
- **The Gemini Batch wave count at 500k rows.** At the shared defaults
  (`GEMINI_BATCH_SHARD_SIZE=5000`, `GEMINI_BATCH_MAX_INFLIGHT=5`), 500k rows is 100 shards
  run 5 at a time = **20 sequential waves**, and Google's Batch API targets turnaround
  "within 24h" **per job**, not per wave. That phase, not the scraping, is the likely
  wall-clock bottleneck at scale — but this is inferred from the shard math and Google's
  documented SLA, not measured against a real 500k-row batch run.

### Live-verification checklist (not yet done — do this before trusting the pipeline at scale)

1. Upload a 100-row CSV to `/uploads/relationship`.
2. Judge `confirmed_relation.csv` by hand: genuine financial relationships vs. mere
   co-mentions the gate should have rejected.
3. Iterate the prompt by editing `backend/app/prompts/relationship_search.txt` and
   **restarting the worker** — the prompt is cached per process
   (`query_builders._RELATIONSHIP_PROMPT_CACHE`), so an edit needs a restart, but **no code
   change** is needed for a prompt change.
4. Record: wall-clock time, per-row latency, `scrapedo_billed_empty`,
   `scrapedo_error_requests`, and credits spent.
5. Re-run the same CSV at `SCRAPEDO_CONCURRENCY=25` and compare against the 100 run —
   this is the test that will prove or disprove the concurrency number above.
6. `kill -9` the worker mid-scrape, restart it, and confirm the run resumes and completes
   with zero lost or double-billed rows (proving the S3-only resume story).
7. Confirm Slack completion/failure pings and the Supabase run row both fire correctly.

### Files

`.env.example`, `CLAUDE.md`, `backend/tests/__init__.py`,
`backend/tests/test_relationship_store.py`, `backend/app/static/js/new_run.js`, `HANDOFF.md`
(this entry). No pipeline code changed in this session — Tasks 1-9 (see
`.superpowers/sdd/2026-08-04-relationship-scrapedo-ai-mode/`) already did the migration
itself; this is configuration and documentation only (Task 10).

---

## Previous completed session — 2026-08-03/04 (gmaps: SerpWow → scrape.do, + throughput work)

**Status: COMMITTED (`5158114`) on what is now `relationship-scrapedo`, 633/633 offline
tests + 6/6 `.mjs` DOM contracts passing, live-verified with three real 100-row runs,
NOT pushed.**

First pipeline off SerpWow. Read "Throughput" and "Still to do" before touching anything —
the headline finding is that **scrape.do, not our code, is the current speed limit**, and
lower concurrency is faster than higher.

### 1. The migration

SerpWow used **two** calls per row: `search_type=places` to harvest `data_cid`s, then one
`place_details` per CID (`1+N`, up to 21 requests) — only because `fetch_data_cids` threw
away everything but the CID. scrape.do's `/plugin/google/maps/search` returns
`website`/`title`/`address`/`phone`/`rating`/`reviews`/`type` **inline per `local_results[]`
entry**, which is everything this pipeline reads. So gmaps is now **one request per row**.

- **NEW `serpwow/scrapedo_maps_client.py`** — `process_gmaps_query(q, gl, client=None)`
  returning the envelope the scorer/reporting already expected. Errors are **returned, not
  raised**; the token is redacted from all error text; `results` is `local_results`
  **verbatim** (it is persisted as the row artifact). Reuses `outcomes.categorize_http_error`.
- **`/plugin/google/maps/place` is deliberately unused.** Verified empirically before
  coding: a `local_result` with no `website` has none in `/maps/place` either (checked
  `AMIGO CORPORATION`, `data_cid=15915247393667579758`), so hydration recovers nothing.
  Probe rows: a Turkish SME returned 1 result **with** a website; a Bangladeshi row
  returned 17 results, 11 with websites (the website-less ones were different companies,
  which `_score_gmaps_candidates` skips by design).
- **Zero SerpWow residue in gmaps.** A row's `cost_breakdown` has only `scrapedo_*` +
  `gemini_*` keys, and `CrawlResponse.serpwow_cost_usd`/`massive_proxy_cost_usd` are
  `Optional[float] = None` so gmaps leaves them unset rather than reporting a misleading
  `$0.00` (their XLSX columns come out blank; the other three pipelines still pass floats).
- **`build_summary` routes on the presence of `scrapedo_*` keys** and skips SerpWow
  accounting for those rows. **Load-bearing, not cosmetic**: a gmaps row carries a
  `formatted_results` entry, so without it the billable-count fallback infers 1 billable
  search and prices every row at `SERPWOW_USD_PER_SEARCH` (regression test
  `test_formatted_results_cannot_infer_billable_serpwow_searches`). It doubles as the
  back-compat path — a pre-migration gmaps run has no `scrapedo_*` keys, takes the SerpWow
  branch, and keeps its old cost card. `run_detail.js` branches on the data, not the pipeline.
- **Deleted:** `gmaps_client.py`, the `/gmaps/discover` + `/gmaps/details` routes (no
  `data_cid` step exists any more), **`aiohttp`** and **`redis`** from `requirements.txt`
  (aiohttp's only importers were the deleted file + route; redis was imported nowhere).
  `/gmaps/search` repointed at the new client. `POST /uploads/gmaps` 400s on a missing
  token, checked **after** CSV validation so a bad CSV still reports the CSV problem.
- **UI copy de-SerpWowed:** the New Run card ("Fast Scrape.do Maps discovery…") and
  Operations' table, which lists **every** non-AI-Mode pipeline so a provider name was
  simply wrong → "Pipeline Uploads History". **Two `"SerpWow"` strings in `run_detail.js`
  were deliberately KEPT** after verifying they are still correct: `providerLabel =
  "SerpWow"` is only a default (gmaps/AI Mode override it), and "Rows where SerpWow
  returned 200…" lives in `emptyResponsesSection`, unreachable for gmaps because
  `empty_response_breakdown()` returns `None` for anything but gsearch/relationship.
  Renaming either would make the UI wrong for the three pipelines still on SerpWow.

### 2. Bugs fixed along the way (all pre-existing, found by tracing)

1. **A provider failure was silently a business "not found".** `process_gmaps_query`
   returned `{"error": …}` and `run_gmaps_from_module` dropped it, so
   `classify_finalized_row` (which reads `context["formatted_results"]`, a key gmaps never
   set) fell through to `not_found`. A rate-limited or mis-keyed run reported "no website
   found" on **every row**, was invisible in the error breakdown, and could not be retried
   because nothing was marked `failed`. Now the error propagates and the executor writes a
   one-entry `formatted_results` tagged `error_source: scrapedo`; `_phase_stats` returns
   the phase's declared source so attribution is not hardcoded to SerpWow.
2. **`SCRAPEDO_TOKEN` was missing from `tests/__init__.py`'s hermeticity guard** — a real
   hole (AI Mode already read it) that a row-path test could have used to hit live
   scrape.do from a developer `.env`.
3. **`_write_json` was neither atomic nor off-loop.** A bare `path.write_text` meant a
   crash mid-write truncated `state.json` and lost **the whole run's** progress, not one
   row's. Now temp-file + `os.replace`, via `asyncio.to_thread`.
4. **Result ordering restored** — `position` order survives, where the old `set()` of CIDs
   randomised it and fed `_score_gmaps_candidates`'s `-0.01*idx` tiebreaker noise.

### 3. Throughput — measured, and the most important part of this session

Three live 100-row runs. **scrape.do queues rather than 429ing, so client concurrency above
its real capacity becomes latency, not throughput:**

| `WORKER_CONCURRENCY` | wall | per row | throughput |
|---|---|---|---|
| 100 | 134s | 28.6s | 0.75 rows/s |
| **25** | **70.5s** | **10.8s** | **1.42 rows/s** |

Going from 100 → 25 made it **1.9x FASTER**. Every row was 1 attempt, zero 429s, zero
errors; peak in-flight was 60, and an isolated call is 3.5s. **Keep 25. Do not raise it** —
rows already reach 43s against `SCRAPEDO_TIMEOUT_SECONDS=90`, so more queueing converts
slow rows into failed ones. Next experiment: 10, to find the knee.

At 1.42 rows/s → ~5.1k rows/hour → **500k ≈ 98 hours.** (An earlier estimate of ~4.9h was
wrong: it assumed the provider serves 100 concurrent.) Open question for scrape.do: the
account allows 200 concurrent, but the Maps *plugin* endpoint plainly does not deliver it.

Client-side fixes made (measured, none of them explain the 28s — that is the provider):
- **Shared pooled `httpx.AsyncClient`** instead of one per row: **636ms → 233ms** per call,
  i.e. ~400ms of DNS/TCP/TLS per row and ~55 hours across 500k. Keepalive sized to
  `SCRAPEDO_CONCURRENCY`; closed in `shutdown_event`.
- **Dropped the duplicated provider payload from state** (gmaps `context.gmaps.raw_response`
  was 36.6 of 39.2 KB; gsearch inlined one per phase). Row **39.2 KB → 2.2 KB**. Ceiling at
  1k rows **4.3 → ~76 rows/s**. Good to roughly **2.7k rows**; past that the per-upload
  lock binds again.
- **`state.json` S3 mirror throttled** to `SERPWOW_S3_STATE_FLUSH_SEC` (5s): it was re-PUT
  to the SAME key twice per row and grows with the run — ~**84MB per 100-row run**, which
  is what made an S3 benchmark run out of uplink. Local disk still written every time and
  **terminal snapshots always mirror**. boto3 retries now `adaptive` in both clients.
- Measured and **ruled out**: S3 PUT latency (317ms, ~1% of a row), boto3 pool (already
  100), the publish loop (no per-row persist), the state lock (~5ms at this size).

### 4. Concurrency model — two kinds of dial, one number to set

- **`WORKER_CONCURRENCY`** = worker **slots** (prefetch + consumer count) = messages in
  flight. `AI_MODE_WORKER_CONCURRENCY` **falls back to it** when unset, so a deployment
  sets one number; `SCRAPEDO_CONCURRENCY` defaults to 100. Needs a worker restart.
- **Per-provider caps** are separate semaphores so raising slots can never breach a vendor:
  SerpWow → `search_fetch_semaphore` (`SEARCH_FETCH_CONCURRENCY`, already existed);
  scrape.do → **`common/provider_limits.scrapedo_slot()`**, ONE gate shared by gmaps AND
  AI Mode because the cap is per *account* and both run in the same process. It stays even
  when only one pipeline runs at a time, because that is not fully manual: AI Mode's
  reconciler re-dispatches work on its own. The provider cap deliberately does **not**
  track `WORKER_CONCURRENCY` (a test asserts this) — otherwise raising slots would raise
  the vendor limit and defeat the point. Rate limits, if scrape.do has them, belong inside
  `scrapedo_slot()` where every caller already passes through.
- **One worker PROCESS is not one concurrent request.** The worker is async — a single
  process held 60 scrape.do calls in flight. The one-worker rule exists because two
  processes would race on `state.json` (the lock is in-process only), **not** for throughput.
- **Deployment (one EC2, per user):** API + worker must share a filesystem, and run exactly
  **one** worker. If they were split across containers with separate disks, the API would
  read a stale S3 `state.json` and its writes (stop / rerun-failed) would clobber the
  worker's rows.
- **Known minor inefficiency:** AI Mode's retry loop is inside its *synchronous* client, so
  the slot wrapped around `to_thread(scrape_batch_sync)` is held across its backoff sleeps.
  gmaps does not have this (only its HTTP call is wrapped). Negligible unless 429s are
  frequent; the fix is hoisting that retry loop out of the sync client.

### 5. Cost + call accounting semantics (get these right before changing them)

Billing is **10 credits per SUCCESSFUL (HTTP 200) call**; failures are free, which is why
retrying is cheap. `credits` is **derived** as `CREDITS_PER_CALL × successful_requests`,
never incremented by hand, so a run always reconciles as `requests == ok + failed`.

- **All 502s are retried** (3 retries, exponential backoff — 1/2/4s for 5xx, **5/10/20s for
  429** because a 1s/2s ramp exhausted every attempt on 27/50 rows of one run; `Retry-After`
  honoured, capped at 30s). scrape.do overloads 502 for both "transiently broken" and
  "Google has no listing" and we cannot tell which until the retries are spent. **Per user
  direction** — an earlier version treated `502 "no results"` as terminal after replaying 4
  such queries returned no-results every time. **Accepted trade: the ~12% of rows with no
  listing now burn 4 attempts + 7s backoff each, so runs are ~30% slower.** If a run still
  reports the same `rows_no_listing` count, the retries bought nothing — worth reverting.
- **A row that recovers after a 502 is NOT an error.** Failed attempts are bucketed by what
  the ROW became: `scrapedo_recovered_requests` (row succeeded — never an error),
  `scrapedo_error_requests` (failed after every retry — the only real errors), remainder =
  attempts on a no-listing row. The UI's failed badge and `run.log`'s `errors=` both use
  `scrapedo_error_requests` only. `run.log`:
  `scrapedo_requests=120 (ok=95 recovered=5 no_listing=12 errors=8) scrapedo_credits=950`.
- **`502 "no results"` after all retries** → `no_results=True`, unbilled, row classifies
  `not_found` with `row_error = "No Google Maps listing exists for this company."` — not an
  error, so it neither inflates the error count nor becomes eligible for a doomed rerun.
- **`scrapedo_billed_empty`** counts HTTP **200s that returned zero results** — credits
  spent for no data, i.e. the scrape.do refund claim. **Measured 0 on gmaps**, so gmaps is
  not leaking money. The `scrapedo_empty_requests/` folder at the repo root is **AI Mode**,
  which is where that spend actually goes — untouched by this work.
- **The `/uploads/ai-mode/... 404` on every run-detail load** was a deliberate probe (a run
  id alone does not say which engine owns it). Views that know the pipeline now pass
  `?engine=ai|serpwow` (`ui.runHref`/`engineOf`) so the right endpoint is tried first. The
  probe **remains as a fallback** — bookmarked URLs carry no hint and a stale hint must
  still resolve; `render()` is now an ordered candidate list.

### Two corrections to earlier claims in this file's history

- Raising `WORKER_CONCURRENCY` does **not** expose gsearch to ~500 concurrent SerpWow
  calls: `SEARCH_FETCH_CONCURRENCY=5` already gates SerpWow globally.
- `serpwow_response/` is **not** written locally for gmaps — it is S3-only. The raw payload
  is still persisted, just remotely.

### Files

`scrapedo_maps_client.py` (new), `common/provider_limits.py` (new), `modes/common.py`,
`modes/gmaps.py`, `modes/gsearch.py`, `outcomes.py`, `serpwow_reporting.py`, `schemas.py`,
`engine.py`, `core/s3.py`, `core/notify.py`, `ai_mode/broker.py`, `ai_mode/worker.py`,
`static/js/{run_detail,new_run,operations,runs,dashboard,ui}.js`, `requirements.txt`,
`.env.example`, `CLAUDE.md`. Tests: `test_scrapedo_maps_client.py` (new, 28),
`test_provider_limits.py` (new, 14), `test_gmaps_llm.py`, `test_gsearch_cost.py`,
`test_timing_summary.py`, `test_gmaps_status.py`, `tests/__init__.py`, and 3 `.mjs` contracts.

### Still to do — in priority order

1. ~~COMMIT THIS~~ **done** — this gmaps migration and the older 2026-07-23 relationship
   changes described further down both landed as separate commits (`5158114`, PR #13).
2. **Validate the 502-retry trade.** Run 100 rows and compare `rows_no_listing` against the
   12 seen before. Same number ⇒ the retries bought nothing but ~30% wall time.
3. **Test `WORKER_CONCURRENCY=10`** on the same CSV to find the throughput knee.
4. **Verify Slack + Supabase on a terminal run** — the only parts of the pipeline never
   confirmed live (UI, timing, costs, outcomes and CSVs all have been).
5. **500k needs the state store replaced.** `update_row_state` still rewrites the whole
   `state.json` per row (ceiling ~2.7k rows), and `report.json` embeds every row
   (unbounded, ~GB at 500k — AI Mode caps entities at 50k, this does not). Agreed
   direction: gmaps gets its own small runner reusing AI Mode's **primitives** —
   `s3_sync`, `StreamingRunReport`, the broker/own-queue pattern, `status.json` O(1)
   counters, file-presence-as-state — but NOT its `ModeConfig`/LLM-cleanup flow, which
   assumes a prompt-driven pipeline gmaps does not have. Design notes: a message carries a
   BATCH of ~20 rows (500k → 25k messages, not 500k) while files stay **per row**, since
   Maps search is one company per query — that is the one real divergence from AI Mode's
   1:1 batch:file model. Also needs sharded row directories (500k files in one dir/S3
   prefix is a slow LIST). This is also the prerequisite for autoscaling the worker.
6. **Decide gsearch / relationship / firmographics.** Still on `api.serpwow.com` via
   `run_serpwow_search` / `codetails.fetch_serpwow`; user intends to retire them. Retiring
   them BEFORE step 5 is cheaper — otherwise step 5 preserves behaviour for pipelines that
   are about to be deleted. Naming decision: `serpwow_*` → `scrapedo_*` per pipeline as it
   migrates, with the package rename (`app/services/serpwow/`, `serpwow_reporting.py`, the
   `serpwow_summary` `/status` field, the `serpwow_response/` S3 prefix) **last**.
7. **AI Mode's empty-response spend** — the real money leak (see §5), never investigated.

## Open findings — 2026-07-23 (S3 throttling + concurrency; DISCUSSION ONLY, no code changed)

Investigated during a live 1000-row relationship run. **No code was changed** — these are
recommendations/answers for a future decision. A scratch plan sits at
`~/.claude/plans/transient-nibbling-pearl.md` (transient).

- **S3 `SlowDown` in worker logs** (`ClientError (SlowDown): Please reduce your request rate`): S3's HTTP-503 throttle. It is **already retried (5× exponential backoff) and best-effort — non-fatal**, so the run is unharmed (`_write_s3_background`, `engine.py:855`; local disk is source of truth, S3 is the resume/backstop mirror). Store is **real AWS S3** (region `ap-south-1`, `AKIA…` IAM key, **no custom `endpoint_url`** — not R2/MinIO), so AWS auto-scales the prefix to 3,500 PUT/s; the 503s are the transient cold-prefix ramp **amplified by `state.json` being re-PUT to the SAME key on every row** (`engine.py:2867`; note `output.json` is already deferred to terminal-only, comment at `engine.py:2869`). **Recommended, NOT applied:** (a) **primary/minimal** — switch boto3 `retries` `"standard"`→`"adaptive"` in `core/s3.py:42` + `engine.py:740` (AWS's own client-side rate-limiter answer to SlowDown); (b) **optional** — throttle the per-row `state.json` S3 mirror behind a new `SERPWOW_S3_STATE_FLUSH_SEC` (~5s, mirrors AI-Mode's `AI_MODE_STATUS_FLUSH_SEC`; local write stays per-row, accepted tradeoff = S3 copy lags ≤5s on cold-start resume, safe since rows are idempotent). Also flagged: the AWS **secret key lives in `.env`** — keep it gitignored, never commit it.
- **Worker concurrency = `WORKER_CONCURRENCY` (env, default 4)** — sets BOTH the RabbitMQ channel prefetch and the consumer-loop count (`engine.py:3253` / `3856`), **identical for gsearch and relationship** (same `process_upload_job` path). So only 4 rows process at once; each relationship row fans **3** AI-Overview searches in parallel → ~`3 × WORKER_CONCURRENCY` in-flight SerpWow calls. Observed ~400 rows/hr at default 4. Raising it speeds runs **but requires a worker restart** (consumers are created at startup; won't affect an in-flight run) and is bounded by SerpWow's account rate limit (and would make the S3 SlowDown above worse). Not changed.
- **`websites_found` = 0 during a running relationship batch is EXPECTED, not a bug**: the gate/URL-selection runs in the Gemini **batch phase after Phase-1 scraping**, so found/confirmed populate only at the end. (Historical note: this was gated on a `RELATIONSHIP_LLM_BATCH=true` flag — "Batch: On" in the UI — at the time. That flag no longer exists; since the 2026-08 scrape.do migration the Gemini Batch verdict pass in `relationship_runner.run_verdict_phase` is unconditional, so the behaviour is now simply how the pipeline works.)

---

## Previous completed session — 2026-07-23 (relationship failure diagnostics, retries, outputs, and failed-row UI)

**Status: COMMITTED (PR #13, `3a3ed0c`), 564/564 offline tests passing, NOT live-verified,
NOT pushed.** (Below describes the pipeline as it was on 2026-07-23 — still SerpWow-backed,
3 AI-Overview searches/row; superseded by the 2026-08-04 scrape.do AI Mode migration, see
the top of this file.) Implemented with Ponytail: existing pipeline and
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
