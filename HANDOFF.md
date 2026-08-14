# HANDOFF — `website_url_finder`

Last updated: 2026-08-11. Architecture lives in `CLAUDE.md`; the gmaps call graph is in
`FLOW.md`; decisions and their reasoning in `DECISIONS.md`; every dated session write-up is
archived newest-first in `docs/HISTORY.md`. This file is only current state + what's owed.

## Status

- Branch **`relationship-scrapedo`**, **12 commits ahead of `origin/relationship-scrapedo`** (pushed branch exists; these are not on it), plus the uncommitted gmaps billing-display change below.
- **764/764** offline tests + **6/6** `.mjs` DOM contracts passing.
- Two pipelines are off SerpWow onto scrape.do: **gmaps** (Google Maps, 2026-08-03) and
  **relationship** (Google AI Mode, 2026-08-04). **gsearch** and **firmographics** still call
  `api.serpwow.com`. AI Mode (`ai_bulk`/`ai_deep`) is broker-driven.
- gmaps is live-verified (3 real 100-row runs). relationship has had real runs — the last three
  commits are fixes they surfaced — but the checklist below is not finished.

## Commands

```bash
docker compose up -d rabbitmq                     # broker + mgmt UI on 15672
cd backend && ../.venv/bin/python -m app.main     # API; UI at http://localhost:11500/app
python worker.py                                  # repo root; exactly ONE process

cd backend && ../.venv/bin/python -m unittest discover -s tests -t .   # 764; -t . is MANDATORY
cd backend && for f in tests/*.mjs; do node "$f" || echo "FAIL $f"; done
```

`-t .` makes `tests/__init__.py`'s hermeticity guard run so real cloud creds from `.env` don't
leak into tests. The `.mjs` contracts are not part of the unittest run — run them after touching
any `static/js` file.

## Blockers before 500k

1. **`GEMINI_BATCH_TIMEOUT_SEC` is 30 min in the live `.env`, not the 48h relationship wants.**
   `relationship_runner.py:289` defaults to 172800 but reads the *shared* SerpWow key, and
   `.env:212` sets `1800`. A Gemini verdict shard that outlives 30 min raises and the run's rows
   keep no `cleaned/` object. Either give relationship its own key or raise the shared one.
2. **`retry-failed-rows` can't complete at scale.** LISTs all of `raw/` then issues one *serial*
   `to_thread` DELETE per dead row in a single HTTP request — 50k dead rows is 20+ minutes and a
   client timeout. It also ignores its `limit` param, and `s3_run_store.delete_object`
   swallows failures, so a row can count as retried and never be rescraped. Fix: `delete_objects`
   in 1000-key batches + honour `limit`.
3. **`phase="failed"` is terminal for the re-drive scan.** Any transient error over a multi-day
   run parks it permanently; recovery is hand-typing the run id into Operations. Fix: attempts
   counter in `status.json` (AI Mode's `requeue_attempts` pattern) and let the scan pick `failed`
   up N times — a re-drive is free.
4. **`GEMINI_BATCH_MAX_INFLIGHT` is silently capped.** Each shard holds a thread for its whole
   multi-hour poll, so raising it past the default `ThreadPoolExecutor`'s `min(32, cpu+4)`
   (**6 on a 2-vCPU box**) creates nothing. Needs a sized executor.
5. ~~**gmaps state store.**~~ **DONE 2026-08-10.** gmaps is S3-only: no `state.json`, object
   presence as row state, O(1) counters, streaming outputs, one message per run on `gmaps_runs`.
   It reuses *relationship's* primitives (`s3_run_store` + the new `s3_run_driver`), not AI Mode's,
   and is two phases rather than three — its LLM confidence modes were deleted, so there is no
   batch pass. Old state-driven gmaps runs are not migrated (hard cutover; their S3 data remains).
6. **API-process memory — partly fixed.** `parse_relationship_csv` (2026-08-05) and
   `parse_entities_csv` (2026-08-10, via `sample_limit`) now validate every row while retaining
   none, so a 500k-row upload no longer materialises its rows in the API process. Still open:
   `/uploads/{id}/result` reads a whole several-hundred-MB CSV into memory instead of streaming
   the boto3 body.

## gmaps billing display — done 2026-08-11 (uncommitted)

Run `8ffe96d90db341ca85147ead3459c05c` (100 rows) billed **870 credits**, and the detail
page explained none of it — it rendered gsearch's "Empty responses (HTTP 200) / All phases
0 / Some phases 0", two keys gmaps never emits.

**The billing itself was already correct.** From S3: `139 attempts = 87 billed 200s + 52
free failures`, `87 × 10 = 870`, `13 no-listing rows × 4 attempts = 52`. That reconciles
with scrape.do's dashboard to the credit — "1000 if everything succeeded" minus 13 rows
Google has no Maps listing for. No accounting code changed.

What changed (reasoning in `DECISIONS.md`, call graph in `FLOW.md` §6):
- `run_detail.js` — `emptyResponsesSection` takes the whole summary and grew a **gmaps
  branch**: "Scrape.do billing (10 credits per HTTP 200)", four chips split by what the
  credits bought — *Billed calls (87 of 100 rows) · Billed but no result · Failed after
  retries · Unbilled attempts (52)*. "Billed but no result" is `billed_empty +
  billed_errors` (an empty results array **or** an error body — both charged, both
  refundable); "Failed after retries" folds the old "No Maps listing" chip in, because a
  502 "no results" and a 502 "request failed" are the same billing event: four attempts,
  nothing charged. `report.json` still reports `no_listing` separately. gsearch and
  relationship branches untouched.
- `serpwow_reporting._build_cost` — new `scrapedo_billed_errors` (rows that errored with a
  billed 200). Subtracting it from `errored` is what stops a paid error row appearing in
  two chips at once. Defaults to 0, so gsearch/relationship reports are unchanged.
- `s3_run_store.Counters` — `rows_no_listing` was **never in `_FIELDS`**, so
  `gmaps_runner`'s `bump(rows_no_listing=1)` had been a silent no-op for the life of the
  pipeline (every in-flight run reported 0 no-listing rows). Added the field, and `bump`
  now **raises** on an unknown counter instead of dropping it.
- `engine._gmaps_fallback_summary` — mid-run cost now carries
  `scrapedo_successful_requests` (= `credits // CREDITS_PER_CALL`) + `scrapedo_failed_requests`,
  so the card is populated before `report.json` exists.

Left alone on purpose: the Slack ping still reads `139 requests · 870 credits` (attempts
next to credits — real but cosmetic; fix it as part of a vocabulary pass across run.log +
report.json + Slack, not alone).

## `retry.csv` — the rerun / refund list, done 2026-08-11 (uncommitted)

**The 13 no-listing rows were never billed.** Every one reads `successful_requests: 0,
credits: 0, no_results: true` — four 502 `{"error":"no results"}` in a row (the live
`.env` has `SCRAPEDO_MAX_RETRIES=3`), never an HTTP 200. Nothing to claim from scrape.do
on that run; `billed_empty` (the 200-charged-for-nothing case) was 0.

**gmaps and relationship now both write `retry.csv`** next to their other outputs, served
by `/uploads/{id}/result?file=retry.csv` and linked in the Files card. It holds only rows
with nothing to show for themselves — `billed_empty` (charged, no data → **refund claim**),
`no_listing` (gmaps, unbilled), `error` (dead after every retry, or never processed) —
with the **original input columns verbatim** plus a `retry_reason` column
(`<kind>: <detail> | attempts=N credits=N`), so the download uploads straight back to
`/uploads/gmaps` or `/uploads/relationship`. Answered rows are excluded: rerunning them
re-buys the same answer. Relationship's `no_ai_text` rows are excluded too (citations but
no prose — the gate still produced a verdict); the count stays in `report.json`.

A billed error (HTTP 200 whose body carried the error) is marked `— billed, refundable`
too; a row that died before any 200 is not, because it cost nothing. Membership rule +
reason strings live in ONE shared helper, `serpwow_reporting.retry_row()`, so the two
pipelines can't drift.

**S3 layout, for the record:** `raw/` = the call and everything that came back (query,
attempt counts, credits, the Maps listings verbatim). `rows/` = **our verdict** on it
(chosen website, confidence + why, cost breakdown) and the row's DONE marker — *not*
cleaned data; gmaps has no LLM. Renaming `rows/` to match relationship's `cleaned/` would
hide every finished row from the resume scan and re-drive them at 10 credits each, so it
keeps its name (`FLOW.md` §3).

**Run 8ffe96d9 predates the file** — its 13 rows are backfilled on disk at
`docs/retry_8ffe96d9_gmaps.csv` (read-only against S3; its existing outputs were not
rewritten). Upload that to re-run them.

## Settled by live probes (2026-08-05, 16 calls / 160 credits)

- **We were never truncating the AI Mode answer.** `raw/` is byte-identical to the wire
  (verified against run `6a71458d…`: top-level keys are exactly scrape.do's, and the
  `💡 …dive deeper…` closing paragraphs are present in 5 stored rows). What dropped content
  was the *read* side — see `evidence_json` in CLAUDE.md.
- **The answer's SHAPE is nondeterministic, and that is Google's, not ours.** 13 successful
  calls with the same query: 10 returned a single `paragraph`, 2 an `ordered_list`, 1 the
  heading form ending at `WEBSITE` with nothing after it. Serial vs an 8-way burst vs
  Postman-style (`token`+`q` only, literal `\n`) made **no difference** — so neither
  `SCRAPEDO_CONCURRENCY` nor the query encoding causes the cut answers, and there is no
  wait/render param on that endpoint to tune. A complete answer can arrive as one
  `ordered_list` whose last item is `"WEBSITE: https://www.pitchly.com"`.
- **502 rate is ~7% even serially** (1 of 14 calls, body `{"error":"folwr request failed"}`),
  and the retries do run: the Nodai row's error object records `request_count=4`. An HTTP
  200 is deliberately never retried — the credits are already spent.

## Unproven numbers

- **`SCRAPEDO_CONCURRENCY=100` is a target, not a measurement.** On the *Maps* endpoint, 100
  measured **1.9x slower** than 25 — scrape.do queues instead of 429ing. Whether the *AI Mode*
  endpoint behaves the same is unverified. It's the shared per-account cap, so tuning it moves
  gmaps and AI Mode too. Do not raise anything here without re-measuring.
- **Gemini Batch wave count at 500k.** At the defaults (shard 5000, inflight 5) that's 100 shards
  in 20 sequential waves, and Google's "within 24h" SLA is per job. Inferred, not measured — this
  phase, not scraping, is the likely wall-clock wall.

## Owed verification

**relationship** (upload a 100-row CSV to `/uploads/relationship`):
1. Judge `confirmed_relation.csv` by hand — genuine relationships vs. co-mentions the gate should
   have rejected. Prompt iterates by editing `backend/app/prompts/relationship_search.txt` and
   **restarting the worker** (cached in `query_builders._RELATIONSHIP_PROMPT_CACHE`); no code change.
2. Re-run at `SCRAPEDO_CONCURRENCY=25` vs 100 — this is what settles the number above.
3. `kill -9` the worker mid-scrape, restart, confirm resume with zero lost or double-billed rows.
4. Confirm Slack pings and the Supabase row.

**gmaps:** the 502-retry trade is now measured — run `8ffe96d9` (2026-08-11) came back with
**13** `rows_no_listing` against the 12 seen before, i.e. `SCRAPEDO_MAX_RETRIES=3` bought
essentially nothing on those rows and cost 52 extra (free) attempts of wall time. Dropping it
back to 2 is worth one A/B. Still owed: `WORKER_CONCURRENCY=10` for the throughput knee, and
Slack + Supabase on a terminal run (the only parts never seen live). Reload the detail page
after the billing-card change and eyeball the five chips against `report.json`.

**`aiModeBroker`:** never live-smoked (offline only). Matters more now that the 500k plan builds
gmaps on AI Mode's primitives — checklist in `docs/HISTORY.md` (2026-07-14).

## Also open

- **`errorTaxonomy` branch is NOT merged — user's call.** `found`/`not_found`/`error` taxonomy,
  reviewed, offline-green, not live-verified. Details in `docs/HISTORY.md` (2026-07-10).
- **Decide gsearch / firmographics.** User intends to retire them; retiring before the gmaps state
  rework is cheaper than preserving behaviour for pipelines about to be deleted. Rename
  `serpwow_*` → `scrapedo_*` per pipeline as it migrates, package rename last.
- **AI Mode's empty-response spend** (`scrapedo_empty_requests/` at the repo root) — the real money
  leak, never investigated. gmaps measured `scrapedo_billed_empty=0`, so it isn't leaking.

## Conventions (do not break)

- **Always use ponytail for coding** — laziest solution that works: YAGNI, reuse what's here,
  stdlib/native before new deps, shortest correct diff. Never shortcut understanding first.
- **Never** add a `Co-Authored-By: Claude` / AI-attribution trailer — tell committing subagents too.
- **Never** `git push` or query the production Supabase DB without explicit approval.
- `docs/` is **gitignored** — on-disk only; code under `backend/` commits normally.
- **Log decisions in `DECISIONS.md`** (newest first, including what was deliberately not
  done) and keep `FLOW.md` honest about what calls what — update its §6 table whenever the
  part of the path you touched moves.
