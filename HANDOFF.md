# HANDOFF — `website_url_finder`

Last updated: 2026-08-11. Architecture lives in `CLAUDE.md`; the gmaps call graph is in
`FLOW.md`; decisions and their reasoning in `DECISIONS.md`; every dated session write-up is
archived newest-first in `docs/HISTORY.md`. This file is only current state + what's owed.

## Status

- Branch **`relationship-scrapedo`**, **5 commits ahead of `origin/relationship-scrapedo`** (pushed branch exists; these are not on it) — newest `ced718c` (output CSVs carry the input columns). Only the firmographics column rename below is uncommitted.
- **778/778** offline tests + **6/6** `.mjs` DOM contracts passing.
- **Three** pipelines are off SerpWow onto scrape.do: **gmaps** (Google Maps, 2026-08-03),
  **relationship** (Google AI Mode, 2026-08-04) and **firmographics** (Google Search,
  2026-08-19). **gsearch alone** still calls `api.serpwow.com`.
  AI Mode (`ai_bulk`/`ai_deep`) is broker-driven.
- **All three scrape.do pipelines are now S3-only** (no state.json, no local disk, one message
  per run). firmographics migrated 2026-08-20 — measured at **2.01 S3 writes / ~881 bytes per
  row, flat at any run size**. **gsearch is the last state-driven pipeline**, and the ~2.7k-row
  ceiling now applies to it alone.
- gmaps is live-verified (3 real 100-row runs). relationship has had real runs — the last three
  commits are fixes they surfaced — but the checklist below is not finished.

## Commands

```bash
docker compose up -d rabbitmq                     # broker + mgmt UI on 15672
cd backend && ../.venv/bin/python -m app.main     # API; UI at http://localhost:11500/app
python worker.py                                  # repo root; exactly ONE process

cd backend && ../.venv/bin/python -m unittest discover -s tests -t .   # 778; -t . is MANDATORY
cd backend && for f in tests/*.mjs; do node "$f" || echo "FAIL $f"; done
```

`-t .` makes `tests/__init__.py`'s hermeticity guard run so real cloud creds from `.env` don't
leak into tests. The `.mjs` contracts are not part of the unittest run — run them after touching
any `static/js` file.

## Blockers before 500k

1. ~~**`GEMINI_BATCH_TIMEOUT_SEC` is 30 min in the live `.env`.**~~ **CODE FIXED 2026-08-19** —
   one `GEMINI_BATCH_TIMEOUT_SEC`, default **172800 (48h)**, shared by all four pipelines via
   `common/llm_batch.py`; `RELATIONSHIP_BATCH_TIMEOUT_SEC` and `AI_MODE_BATCH_TIMEOUT_SEC` are
   deleted. **Still owed from YOU:** the live `.env` pins `1800` on line 227 — edit it or the
   old deadline stands. Original note below for context.
   **`GEMINI_BATCH_TIMEOUT_SEC` was 30 min in the live `.env`, not the 48h relationship wants.**
   `relationship_runner.py:289` defaults to 172800 but reads the *shared* SerpWow key, and
   `.env:212` sets `1800`. A Gemini verdict shard that outlives 30 min raises and the run's rows
   keep no `cleaned/` object. Either give relationship its own key or raise the shared one.
2. ~~**`retry-failed-rows` can't complete at scale.**~~ **DONE** — verified in code 2026-08-20:
   `engine.retry_failed_rows` LISTs `errors/`, honours `limit`, and calls
   `s3_run_store.delete_objects` in 1000-key batches returning a CONFIRMED count. Original note:
   one serial DELETE per dead row (50k = 20+ min and a client timeout), `limit` ignored,
   failures swallowed.
3. ~~**`phase="failed"` is terminal for the re-drive scan.**~~ **DONE** — verified in code
   2026-08-20: `s3_run_driver.redrivable` re-drives a `failed` run while
   `drive_attempts < max_attempts` (`{GMAPS,FIRMOGRAPHICS,RELATIONSHIP}_MAX_DRIVE_ATTEMPTS`, 3),
   and the staleness gate applies to failed runs too so an instantly-dying run cannot spin.
   Original note: any transient error over a multi-day run parked it permanently.
4. ~~**`GEMINI_BATCH_MAX_INFLIGHT` is silently capped.**~~ **DONE 2026-08-19.** The worker
   sizes its default executor to `max(32, max_inflight + 16)` in `start_worker_consumers`.
   Note the blocker was **relationship-only**: engine's chunk driver awaits `asyncio.sleep`
   between polls and AI Mode polls all shards from one thread, so neither ever held a thread
   per shard. Original note: **`GEMINI_BATCH_MAX_INFLIGHT` was silently capped.** Each shard holds a thread for its whole
   multi-hour poll, so raising it past the default `ThreadPoolExecutor`'s `min(32, cpu+4)`
   (**6 on a 2-vCPU box**) creates nothing. Needs a sized executor.
5. ~~**gmaps state store.**~~ **DONE 2026-08-10.** gmaps is S3-only: no `state.json`, object
   presence as row state, O(1) counters, streaming outputs, one message per run on `gmaps_runs`.
   It reuses *relationship's* primitives (`s3_run_store` + the new `s3_run_driver`), not AI Mode's,
   and is two phases rather than three — its LLM confidence modes were deleted, so there is no
   batch pass. Old state-driven gmaps runs are not migrated (hard cutover; their S3 data remains).
6. ~~**firmographics cannot reach 500k.**~~ **DONE 2026-08-20** — S3-only, three phases,
   `firmographics_runs` queue. Its LLM phase finds its work with one LIST (`pending_llm/`
   markers) rather than a GET per row.
7. **API-process memory — partly fixed, and this is the LAST real blocker.**
   `parse_relationship_csv` (2026-08-05) and `parse_entities_csv` (2026-08-10, via
   `sample_limit`) now validate every row while retaining none, so a 500k-row upload no longer
   materialises its rows in the API process. **Still open, re-confirmed 2026-08-20:**
   `engine.upload_result_file` calls `s3_run_store.get_bytes` and returns `Response(content=…)`
   — the WHOLE output CSV in API-process RAM, per concurrent download. It now hits all three
   S3-only pipelines (`enriched.csv`, `found.csv`, `confirmed_relation.csv`), which at 500k rows
   are several hundred MB each. Fix: `StreamingResponse` over the boto3 body. While there: that
   branch also serves `.csv` as `text/plain` and ignores `download`, so a 500MB file opens in
   the browser instead of downloading.

## gmaps billing display — done 2026-08-11 (commit `15167f3`)

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
- `reporting._build_cost` — new `scrapedo_billed_errors` (rows that errored with a
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

## `retry.csv` — the rerun / refund list, done 2026-08-11 (commit `15167f3`)

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
reason strings live in ONE shared helper, `reporting.retry_row()`, so the two
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

## firmographics: `website_url` + a preview that detects the real columns — 2026-08-18 (uncommitted)

The firmographics upload demanded `official_website` and **rejected `website_url`** — the
very name gmaps/AI Mode/relationship write into found.csv, so you could not feed a run's
output straight into an enrichment run. `website_url` is now the canonical column (first
alias, and the name shown in both error messages and the New Run "Expected CSV columns"
chip); `official_website`/`website`/`url`/`domain` still resolve, so no existing CSV
breaks. The internal dict/state key stays `official_website` — renaming that touches the
executor, state rows and the XLSX `input_official_website` column for no user benefit.
First-ever test for this parser: `tests/test_firmographics_csv.py`.

**The preview lied about the columns.** `/uploads/preview` runs `parse_entities_csv`
(needs company_name + country), so a firmographics CSV fell through to POSITIONAL parsing
and reported *"col 1 = company name, col 2 = country"* for a file whose first column is a
URL. Fixed the way relationship already was: its own `/uploads/firmographics/preview`,
with `new_run.js` picking the endpoint per pipeline. Both the parser and the preview now
resolve headers through ONE table, `csv_input.firmographics_columns()` (which replaced six
near-identical alias loops — net line deletion), so the preview cannot advertise a mapping
the upload disagrees with, and its sample rows come from the real parser output.

**No invented company names.** `company_name` used to fall back to the URL's domain — in
the parser when the CSV had no company column, and again in the executor when the value
was blank — so `acme.com` looked like a real company name in the output and was fed to the
LLM as one. Both fallbacks are gone: no column, empty cell.

**Firmographics now accepts the same company aliases as every other pipeline**
(`…|entity_name|entity|organization|…`). Found by running the new endpoint against a real
`found.csv`: its company column is `entity_name`, which firmographics rejected, so a
round-tripped file silently used the DOMAIN as the company name for every row — which
would have quietly defeated the point of the rename.

## Output CSVs carry the input file's columns — done 2026-08-11 (commit `ced718c`)

`found.csv` / `notFound.csv` used to be a fixed seven columns
(`company_name, company_local_name, country, website_url, confidence, flags, attempt_log`)
and dropped every input column outside the five `parse_entities_csv` maps. **Not a
regression** — `CSV_COLUMNS` is unchanged since `9cc4248` (2026-06-26); relationship was
already passing its input through, which is what made the gap visible.

Now, for **gmaps + AI Mode** (relationship already did this): the input header **verbatim
and in order**, then `website_url, confidence, flags, attempt_log` (+ `error` on notFound).
Verified against real run `8ffe96d9` — its 10 columns
(`entity_name … firm_id`) come out first, in order.

- The rule lives once, in `common/text.passthrough_fieldnames` / `passthrough_row` —
  **there, not in `reporting`**, because `ai_mode/run_reporting.py` is
  standalone-by-contract and `reporting` pulls in `httpx`.
  `relationship_outputs._passthrough_fieldnames` is now a one-line wrapper over it.
- `reporting.CSV_COLUMNS` is split into `RESULT_COLUMNS` + the three echoed fields
  and is byte-identical, so the **gsearch** writer is untouched.
- **AI Mode: the writer owns the input.csv cursor.** `StreamingRunReport(run_dir,
  company_column)` pulls one input row per `EntityResult`, so alignment is an invariant of
  the writer, not of a caller with two `add_batch` sites and a `continue` between them.
  Caller diff is 3 lines. **The trap it must respect:** `parse_entities_csv` skips a row
  with an empty company name *without spending an sno*, so the cursor replays that rule —
  otherwise every row after the first blank one gets the previous row's cells, silently.
  Two tests pin it (writer-level and end-to-end).
- Falls back to the old columns for headerless/positional input or an unreadable
  input.csv.

**gsearch is deliberately excluded — user's call, 2026-08-16: it is being removed, so it
gets no passthrough work.** (For the record, it would not have been a reporting fix:
`parse_csv_rows` keeps 6 mapped keys, the uploaded bytes are dropped at the end of the
request, and nothing carries the original record into `state.json`, so it would need an
`input.csv` write at upload plus a deliberate join.) **firmographics** writes no found.csv
at all, and **`output.xlsx`** has the same problem via its fixed `input_*` block — both
follow gsearch out, so neither is worth touching either.

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

- ~~**`SCRAPEDO_CONCURRENCY=100` measured 1.9x slower than 25.**~~ **OBSOLETE — re-measured
  2026-08-25.** Three completed 100-row gmaps runs at concurrency **100**, from S3 counters:

  | run | date | scrape_s | rows/s |
  |---|---|---|---|
  | `64b81c68` | 2026-08-25 | **34** | **2.94** |
  | `1a46cba0` | 2026-08-18 | 36 | 2.78 |
  | `8ffe96d9` | 2026-08-11 | 44 | 2.27 |

  The 2026-08-04 table (100 → 134s/0.75 rows/s, 25 → 70.5s/1.42 rows/s) no longer describes
  this system: **100 is now ~4x faster than 100 was, and ~2x faster than the best 25 ever
  measured.** The A/B predates the pooled `httpx.AsyncClient` landing, so the old "keep 25, do
  not raise" guidance is withdrawn. 500k at 2.94 rows/s ≈ **47 hours**, not 98.
  **Still owed:** a fresh run at 25 — all three above are at 100, so we know 100 improved, not
  that it beats 25 today. Whether the *AI Mode* endpoint behaves the same is still unverified,
  and it is the shared per-account cap, so tuning it moves gmaps and AI Mode together.
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
- **Decide gsearch.** It is the ONLY pipeline left on SerpWow and the only one still
  state-driven, so the ~2.7k-row ceiling and `serpwow_client` exist for it alone. Retiring it
  would let the `services/serpwow/` package rename (and `CrawlResponse.serpwow_cost_usd`)
  finally happen. firmographics is no longer part of this decision — it migrated 2026-08-19/20.
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

## Owed verification — firmographics S3-only (2026-08-20)

Offline-green (12 dedicated tests drive the real runner + real store against a fake S3) but
**never run live**. Checklist, mirroring what gmaps and relationship went through:

1. Upload a 100-row CSV to `/uploads/firmographics`; confirm `enriched.csv` /
   `notEnriched.csv` / `retry.csv` / `report.json` / `run.log` all appear in the Files card,
   and that `output.json` / `output.csv` are NOT offered (they 404 by design now).
2. Reconcile `report.json`'s `scrapedo_credits` against scrape.do's dashboard:
   `10 x search 200s + 5 x ai-overview 200s`. The deferred count is the number to watch —
   it has never been observed on a real run.
3. `kill -9` the worker mid-scrape, restart, confirm the re-drive resumes with zero
   re-scraped rows and zero re-spend (the offline test asserts this; live proves it).
4. Run once with `LLM_BATCH=true` and once `false`; the six fields must match. Both paths send
   a byte-identical prompt, so a difference means the batch mapping is wrong.
5. Slack ping + the Supabase row on a terminal run.
