# DECISIONS

Every meaningful decision taken while changing this code, and why. Newest first. A
decision belongs here when a reasonable engineer could have chosen otherwise — including
the things deliberately **not** done.

Companion files: `CLAUDE.md` (architecture), `FLOW.md` (call graph), `HANDOFF.md`
(current state + what's owed), `docs/HISTORY.md` (dated session write-ups).

---

## 2026-08-11 (d) — every output CSV is the input file plus the computed columns

*"i want what the input file has thats all."*

`found.csv` / `notFound.csv` carried a fixed seven columns and dropped every input column
that wasn't one of the five `parse_entities_csv` maps. First finding: **this was not a
recent regression** — `CSV_COLUMNS` is unchanged since `9cc4248` (2026-06-26). Relationship
was already doing it right; `retry.csv` (this session) made the contrast visible.

### D20. Passthrough, not passthrough-plus-canonical

The output header is now the **input header verbatim and in order**, then
`website_url, confidence, flags, attempt_log` (+ `error` on notFound). If the upload says
`entity_name`, the output says `entity_name` — no synthesised `company_name`.

Rejected: also emitting canonical `company_name`/`country` for backwards compatibility.
It duplicates columns the input already has, and nothing in the repo reads these files
back (`grep` over `app/`: found.csv is written and served, never parsed). "Same as the
input" is the whole request; a second naming convention next to it defeats it.

### D21. `passthrough_fieldnames` lives in `common/text.py`, not `serpwow_reporting.py`

`ai_mode/run_reporting.py` is deliberately standalone (docstring: models + `serpwow.outcomes`
only), and `serpwow_reporting` pulls in `serpwow_client` → `httpx`. `common/text.py` is
already the home of the one other AI-Mode↔SerpWow shared helper (`slugify_company`) and
imports nothing but `re`. Ten pure lines go there; both engines import them.

The optional `source_overrides` param is not speculative: `s3_run_store.iter_input_rows`
overwrites a real `row_index` column with its own index, so **both** S3 pipelines need the
remap, and AI Mode's plain `DictReader` must not have it. Two callers each way.
`relationship_outputs._passthrough_fieldnames` becomes a one-line wrapper — its existing
collision test passed unchanged, which is the proof the move was behaviour-preserving.

### D22. AI Mode: the writer owns the input cursor

Considered handing the original rows in per batch (`add_results(results, originals)`).
Rejected: that puts alignment in the **caller**, across two `add_batch` sites with a
`continue` between them, where a desync is invisible and no unit test of the writer can
catch it.

Instead `StreamingRunReport(run_dir, company_column)` opens its own forward-only cursor and
pulls exactly one input row per `EntityResult`. Alignment becomes an invariant of the
writer; the caller diff is 3 lines and the failed-batch `continue` is structurally safe
(that path emits one result per entity too, through the same `add_results`).

Memory is unchanged — a cursor, never a `sno → row` map (that would be a second full-run
structure at 1M rows).

### D23. The join is positional-by-consumption, and the blank-name row is the trap

`parse_entities_csv` **skips a row whose company cell is empty without spending an `sno`**,
so the Nth result is *not* the Nth data row. The cursor replays that exact rule
(`if not (row.get(company_column) or "").strip(): continue`). Without it, every row after
the first blank one is written with the previous row's cells — silently, with no other
symptom. That is the one test written to fail loudest, at both the writer and end-to-end
level.

Not joining on `EntityResult.sno` deliberately: `results.py:79` takes the LLM's *echoed*
sno when present, so it can be hallucinated.

Guards that fall back to today's columns: positional (headerless) input — different skip
rule, no real header — and an unreadable `input.csv`. The fallback is also what keeps the
classic `write_outputs()` entry point and every temp-dir unit test working untouched.

A `close()` canary warns when the cursor has unconsumed rows left. Kept despite being
unreachable-by-design: it is the only symptom a desync would ever produce.

### D24. Collisions: `error` is reserved for BOTH files

An input column named `website_url` becomes `website_url__orig`; the computed value keeps
the plain name. `error` is reserved for `found.csv` too, even though only `notFound.csv`
writes it, so the two files' passthrough naming stays parallel.

### D25. gsearch is excluded — settled 2026-08-16: it is being removed

Raised as a scope question and answered by the user: **gsearch is being removed, so it gets
none of this work.** Recorded because the cost was real either way — it is not a
reporting-layer fix: `csv_input.parse_csv_rows` keeps 6 mapped keys and discards the rest,
the uploaded bytes are dropped at the end of the request, and nothing carries the original
record into `state.json`, so it would need an `input.csv` write at upload plus a deliberate
join (its row index counts only kept rows).

**firmographics** writes no found.csv at all (not in `REPORTING_PIPELINES`) and
**`output.xlsx`** has the same problem via its fixed `input_*` block — both belong to the
pipelines on their way out, so neither is worth touching. Said out loud rather than
silently skipped.

### D26. Verified against real data, not just fixtures

Re-ran `gmaps_outputs.write_outputs` over the finished S3 run `8ffe96d9` with the S3 writes
intercepted in-process (nothing sent, existing outputs untouched): the new `found.csv`
header is that run's own 10 input columns in order, then the 4 computed. 771 tests + 6 DOM
contracts green, and **no existing test needed editing** — which is the evidence that
splitting `CSV_COLUMNS` into `RESULT_COLUMNS` + the three echoed fields left the gsearch
path byte-identical.

---

## 2026-08-11 (c) — the billing card says what was PAID for, in four numbers

User's spec, verbatim: *"billed / billed but no result like api returned empty result
block / failed even after retries (so technically its 502s so no billed)"*. Three buckets
answering one question — what did the credits buy — plus the attempt count already there.

### D16. Split on BILLED, not on failure type

The previous five chips split by *what went wrong* (empty, no-listing, errored). The new
four split by *what it cost*, which is the question the card exists to answer:

| chip | meaning | billed |
|---|---|---|
| Billed calls | `87 of 100 rows` | yes — × 10 = the invoice |
| Billed but no result | empty results array **or** an error body | **yes — refund claim** |
| Failed after retries | every attempt failed (502/429/timeout), incl. "no results" | no |
| Unbilled attempts | the retry cost, in calls | no |

The old "No Maps listing" chip is gone as a separate number, per the user's "rename to
errors like 4 times did still failed": from a billing standpoint a 502 "no results" and a
502 "request failed" are the same event — four attempts, nothing charged. `report.json`
still carries `no_listing` separately (`empty_response_breakdown`), and `run.log` still
prints `rows_no_listing=`, so the distinction is not lost for anyone who needs it — it is
just not a *billing* distinction.

### D17. A 200 with an error body is a billed row (new `scrapedo_billed_errors`)

`process_gmaps_query` already treats it correctly (`successful_requests=1`, 10 credits),
and the row was already visible — but split across "Billed calls" and "Failed after
retries", so nothing said *we paid for that error*. Added `scrapedo_billed_errors` (a ROW
count, `outcome == "error" AND successful_requests > 0`) to `_build_cost`.

It buys two correctness properties, not just a label:
- **Billed but no result** = `billed_empty + billed_errors` — every credit that bought nothing.
- **Failed after retries** = `no_listing + (errored − billed_errors)` — subtracting the
  billed ones is what stops a paid error row being counted in both chips.

Defaulted to 0 in `_build_cost`, so relationship/gsearch reports are unchanged.

### D18. `retry.csv` marks a billed error "refundable" too

Same claim as a billed-empty row, and retry.csv is the file the claim is made from. The
flag keys off `credits` being non-zero on an errored row — which can only happen via a 200
— so a row that died before any 200 correctly stays unmarked. One condition in the shared
helper, so relationship's billed-error rows (200 + `insufficient credit` body) inherit it.

### D19. `rows/` keeps its name

Asked whether `rows/` holds "cleaned data". It does not — gmaps has no LLM. `raw/` is the
call and everything that came back (query, attempt counts, credits, the Maps listings
verbatim); `rows/` is our verdict on it (chosen website, confidence and why, cost
breakdown, error classification). Renaming it to `cleaned/` for symmetry with relationship
would be actively wrong *and* expensive: `rows/` is the DONE marker, so a rename hides
every finished row from the resume scan and a re-drive re-scrapes them at 10 credits each.
Documented the two layouts in `FLOW.md` §3 instead.

---

## 2026-08-11 (b) — `retry.csv`: the rerun / refund list (gmaps + relationship)

Trigger: "are the 13 No-Maps-listing rows HTTP 200 with no data? if yes I want to claim a
refund — and either way give me a CSV so I can rerun them."

### D8. Answered from the stored rows, not from memory — and the answer is *no*

Read all 100 `rows/` objects of run `8ffe96d9`. Every one of the 13 shows
`successful_requests: 0, failed_requests: 4, credits: 0, no_results: true` — four
consecutive 502 `{"error":"no results"}`, **never an HTTP 200**. `billed_empty` is 0 for
the whole run.

So: **nothing to refund on that run** (they cost zero credits), and they *are* the
"failed after every retry" case — with `SCRAPEDO_MAX_RETRIES=3` in the live `.env`, four
attempts each. The two cases are genuinely different and the code already keeps them
apart; the run just had no surface that showed which one happened.

### D9. One `retry.csv` per run, not two files and not a UI table

Rejected: separate `refund.csv` + `rerun.csv`. The row sets overlap conceptually (a
billed-empty row is *both* refundable and worth rerunning), and two files means two
downloads and two upload decisions. One file with a `retry_reason` column is filterable in
Excel in five seconds and re-uploadable as-is.

Rejected: rendering the rows in the run-detail page. The stated purpose is to **re-upload**
them; a table would still need an export.

### D10. Membership rule: rows with nothing to show for themselves

In: `billed_empty` (charged, no data — the refund case), `no_listing` (gmaps, unbilled),
`error` (died after every retry, or never processed).

Out: **every row the provider actually answered**, including "Maps had listings but none
carried a website" and relationship's not_confirmed/unclear verdicts. Rerunning those buys
the same answer twice, at full price. Also out: gmaps' "Row has no company name" rows —
nothing to search, so a rerun cannot help.

Deliberately out for relationship: `no_ai_text` rows (a billed 200 with citations but no
prose). They are the broader "the prompt underperformed" signal, and the gate still had
candidates to work with and produced a verdict. Including them would put most of a
weak-prompt run in the rerun list. `report.json`'s `empty_response_breakdown.no_ai_text`
still reports the count.

### D11. Original input columns, verbatim, plus exactly one

`retry.csv` exists to be uploaded straight back to `/uploads/{gmaps,relationship}`, so it
carries the input header as-is — not `CSV_COLUMNS`, not relationship's enriched
passthrough (which suffixes colliding names, breaking round-trip fidelity). One added
column, `retry_reason`, suffixed with `_` if the input already has one.

Reason format is uniform across both pipelines so one grep works on either file:
`<kind>: <detail> | attempts=N credits=N`.

### D12. One shared helper, in `serpwow_reporting`

`retry_column()` + `retry_row()` live in `serpwow_reporting.py` — already the shared,
dependency-light module gmaps imports, and relationship now imports it too (it pulls in
only `models/results` and `serpwow_client`, so no cycle). The alternative — the same
15 lines in both `gmaps_outputs` and `relationship_outputs` — is exactly how the two
pipelines' vocabularies drift apart.

Written into the existing streaming loops via one more spooled temp file, so memory stays
O(one row) and nothing about this scales with run size.

### D13. Always write the file, even with zero rows

A header-only `retry.csv` is one S3 PUT and makes the Files card unconditional. The
alternative — conditional write plus conditional advertising — adds a branch to two output
modules and the file-availability probe to save an empty object.

Cost paid: `_s3_run_available_files` now does 5 scoped LISTs per terminal status call
instead of 4 (asserted in `test_relationship_endpoint`, now derived from
`_RELATIONSHIP_FILES` so the next file doesn't break it).

### D14. Backfilled run 8ffe96d9's list locally, did not touch its S3 outputs

The run finished before this code existed. Regenerating its outputs in place would have
rewritten `found.csv` / `notFound.csv` / `report.json` / `run.log` to produce one new file
— a destructive-shaped operation for an additive want. Instead ran the *same* `retry_row`
helper read-only over its `rows/` objects and wrote
`docs/retry_8ffe96d9_gmaps.csv` (13 rows) on disk. Future runs get the file from the
pipeline itself.

### D15. TDD, and what the tests pin

Both features were written test-first and watched fail (`KeyError: …/retry.csv`,
`AttributeError: 'NoneType'`) before any production code existed. The tests pin the
membership rule and the round-trip contract, which is what could silently rot:
- gmaps: only the no-listing + dead rows appear, with the original columns in order;
  a billed-empty row reads "refundable" with `credits=10`.
- relationship: confirmed row absent; empty / dead / never-processed present, in row order.

Two existing tests changed expectations, both legitimately: the put_fileobj key set (a
fifth output exists) and the terminal-status LIST count (4 → `len(_RELATIONSHIP_FILES)`).

---

## 2026-08-11 (a) — gmaps credit reporting

Trigger: run `8ffe96d90db341ca85147ead3459c05c`, 100 companies. scrape.do's dashboard
showed **870 credits**; the run detail page showed an "Empty responses (HTTP 200)" card
reading *All phases 0 / Some phases 0*, which is gsearch's shape and explains nothing.

### D1. Verify the billing against the provider before changing any accounting code

Pulled the run's real artifacts out of S3 first (`report.json`, `status.json`, `run.log`):

```
scrapedo_requests 139 = successful 87 + failed 52
credits 870 = 87 × 10
rows: 68 found, 32 not found, 0 errored, 13 no-listing (× 4 attempts = 52)
```

870 is **exactly right** and matches the dashboard to the credit. The reported bug was a
*display* bug, not an accounting one.

**Decision: change nothing in `scrapedo_maps_client` or the cost builders.** The rule
"10 credits per HTTP 200, data or not" is already implemented — a 200 with empty
`local_results` still sets `successful_requests=1` and is additionally flagged
`billed_empty`, and a 200 whose body carries an `error` is billed too. Rewriting correct
arithmetic to fix a UI complaint would have been the expensive wrong move.

**Why this first:** the alternative — patching the numbers until they matched someone's
expectation — risks "fixing" a report that was already truthful. Ground truth was two
S3 GETs away.

### D2. Fix the card by branching on the data, not by deleting it

The ask was "not needed here like this". Two readings: remove the card for gmaps, or make
it say something true. Chose the second.

`emptyResponsesSection` already branched relationship (`no_ai_text`) vs gsearch
(`all_phases`/`some_phases`). gmaps hit the gsearch fallback and rendered two keys it
never emits — permanent zeroes. Added a third branch keyed on `eb.no_listing != null`.

**Why not delete the section for gmaps:** the gap between "100 rows × 10 credits = 1000"
and the 870 actually billed is exactly what the operator needs to see, and no other
surface answers it — the cost card shows the total, not why. Deleting would have removed
the only place the answer could live.

**Why key on the data (`eb.no_listing != null`) and not on `s.pipeline === "gmaps"`:**
this file's established rule, and it earns its keep — a pre-migration gmaps run carries
`serpwow_*` keys instead and must keep its old rendering. A pipeline-key branch would
break those runs.

### D3. What the card shows — five numbers, chosen to reconcile the bill

*Billed calls (87 of 100 rows) · Billed but empty (0) · No Maps listing (13) ·
Failed after retries (0) · Unbilled attempts (52)*

Rationale per chip:
- **Billed calls, as a fraction of rows** — one row can be billed at most once, so
  `87 of 100` *is* the 870-vs-1000 explanation in one line.
- **Billed but empty** — the only genuine "empty HTTP 200" for this pipeline, and the
  refund claim to raise with scrape.do. Keeps the card's original intent alive.
- **No Maps listing** — the 13 free rows.
- **Failed after retries** — the user asked explicitly for rows that retried and still
  died. Reads `outcome_breakdown.errored` (rows), not `scrapedo_error_requests`
  (attempts), because the failed-rows viewer next to it is also row-shaped.
- **Unbilled attempts** — the retry story, i.e. work done for free (52 here).

Rejected: a sixth "expected credits (1000)" chip. It is `rows × 10` — arithmetic the
heading already states, and a number that would be wrong the moment a pipeline bills
differently.

### D4. `Counters.bump` now raises on an unknown counter (root cause, not symptom)

`gmaps_runner:93` has always called `counters.bump(rows_no_listing=1)`, but
`Counters._FIELDS` never listed `rows_no_listing` and `bump` skipped unknown keys with
`if key in self.values`. The counter has been a no-op for the life of the pipeline, so
`_gmaps_fallback_summary` served `no_listing: 0` for every in-flight run.

Two changes, both required:
1. Added `rows_no_listing` to `_FIELDS` (fixes gmaps).
2. Made `bump` raise `KeyError` on an unknown key (fixes the *class*).

**Why raise rather than just add the field:** the silent skip is what let a whole counter
be dead without anyone noticing. Every call site in the repo passes literal keywords
(checked), so this can only ever fire on a typo — at import-adjacent speed, in tests, not in
production data. Verified all call sites before changing it.

**Why not `assert`:** stripped under `python -O`.

Tradeoff accepted: a typo now fails the row task instead of quietly under-reporting. That
is the right direction — `drain()` counts the raise into `task_errors`, so it surfaces.

### D5. Mid-run status derives billed calls from credits

`_gmaps_fallback_summary` (served until `report.json` exists) had only `requests` and
`credits`, so the new card would have shown "0 of 100 rows billed" mid-run while the
credits chip climbed.

**Decision: derive `scrapedo_successful_requests = credits // CREDITS_PER_CALL`** rather
than add a 13th counter to `status.json`.

Credits are *defined* as `10 × billed 200s` in exactly one place
(`scrapedo_maps_client._envelope`), so this is exact arithmetic, not an estimate. Imported
`CREDITS_PER_CALL` instead of hardcoding `10` so the two can never drift. A new counter
would have been a second source of truth for a number already stored.

### D6. Left alone, deliberately

- **The Slack ping** renders `139 requests · 870 credits`, which invites a divide-by-10
  that doesn't work. Real but cosmetic, and the wording (`scrapedo_requests` = attempts)
  is consistent with `run.log` and `report.json`. Renaming it in Slack alone would split
  the vocabulary across surfaces for one line of prose. Worth doing only as part of a
  vocabulary pass over all three.
- **`run.log`'s `no_listing=52` (attempts) next to `rows_no_listing=13` (rows)** — same
  reasoning; both are documented in `FLOW.md` §4.
- **Anything in `gmaps_outputs` / `_build_cost`** — the terminal report was already right.

### D7. Tests: one per behaviour that could silently regress

- `test_a_no_listing_row_lands_on_the_live_counter` — the counter is now real *and* lands
  in status.json. This is the test that would have caught D4 originally.
- `test_bump_rejects_an_unknown_counter` — locks the guard in.
- `test_the_live_summary_splits_billed_from_unbilled_calls` — the D5 arithmetic, seeded
  with the real run's numbers (139/870 → 87/52).
- `gmapsBillingBreakdown` in `run_detail_dom_contract.mjs` — asserts the new chips *and*
  that "All phases" is gone, seeded with run 8ffe96d9. The DOM contract is the only thing
  that can catch the original defect, which was pure rendering.

No new test framework, no fixtures: unittest + the existing `FakeS3`, and the repo's own
`.mjs` contract harness. 759 offline tests + 6 `.mjs` contracts green.

---

## Older decisions

Pre-dating this file. The reasoning for the big architectural calls is embedded in
`CLAUDE.md` and in module docstrings, which is where it belongs — this file is for
decisions made *while changing* the code from here on. The ones worth knowing:

- **gmaps and relationship are S3-only** (no `state.json`, no local disk): `update_row_state`
  rewrote the whole state file per row under a lock, so write cost grew with the square of
  the row count — ~275TB of rewrites at 500k rows. Object presence is now the row state.
- **One RabbitMQ message per RUN** for those two pipelines, not per row: parallelism comes
  from a bounded task window sized by `SCRAPEDO_CONCURRENCY`. The broker is a durable start
  signal, not a work distributor. Consequence: the consumer acks on receipt (the repo's
  only inversion of ack-after-persist), because a multi-hour run outlives RabbitMQ's
  30-minute `consumer_timeout`; `redrive_stale_runs` supplies the durability instead.
- **One scrape.do semaphore for the whole process** (`provider_limits.scrapedo_slot()`):
  the vendor cap is per *account* and gmaps + AI Mode share a worker, so two caps could sum
  past it. It deliberately does not track `WORKER_CONCURRENCY` — that would let raising
  worker slots silently raise the vendor limit.
- **502 is always retried; a 502 `{"error":"no results"}` that survives every retry is a
  business not-found, not a failure.** scrape.do overloads the status and failed attempts
  are free, so the only cost of finding out is latency.
- **gmaps confidence is heuristic and that is the only mode.** The LLM/batch modes routed
  through the shared gsearch chunk engine, whose state lives in `state.json` — the exact
  thing that capped the pipeline at ~2.7k rows.
