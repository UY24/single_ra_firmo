# DECISIONS

Every meaningful decision taken while changing this code, and why. Newest first. A
decision belongs here when a reasonable engineer could have chosen otherwise — including
the things deliberately **not** done.

Companion files: `CLAUDE.md` (architecture), `FLOW.md` (call graph), `HANDOFF.md`
(current state + what's owed), `docs/HISTORY.md` (dated session write-ups).

---

## 2026-08-25 — a run published into a void, and a stale throughput number

### D73. Retry stays manual — so the button has to be findable and the rows nameable

Asked whether the re-drive scan should auto-retry a run left with unjudged rows. **No —
user's call: a rerun only ever happens when someone clicks.** Automatic retry spends Gemini
tokens unattended, and a permanently-poisoned shard would burn `drive_attempts` worth of
them with nobody watching. `redrivable()` is unchanged; `completed` stays terminal to the
scan.

That decision only holds if the manual path actually works, and it did not. A run whose
scrape succeeded and whose Gemini shard died reported `completed_with_errors` while **every
visible count read zero**: `failed_rows` is `counters.rows_failed`, which only bumps on a
SCRAPE failure; "View failed rows" is gated on `errors > 0`; the rows were `outcome=not_found`
carrying `"No firmographics extracted."` — byte-identical to a row Google had no AI Overview
for, which is FINAL; they were counted in `no_ai_overview` **despite having had an overview**;
they were absent from `retry.csv`; the run detail page had no Rerun button at all (that
section posts to the AI-Mode `/resume` endpoint); and `retry-failed-rows`, reached by
hand-typing the id into Operations, answered **"Enqueued 0 failed rows"** while correctly
republishing and redoing exactly that work.

Fixed as four small things, all naming the same distinction — *did the LLM ever run for this
row*:

- **`reporting.retry_row` grew an `llm_incomplete` reason**, last in precedence because it is
  the only one whose rerun costs nothing from the provider. Shared, so firmographics and
  relationship say it the same way.
- **The signal is object presence, not a flag.** `cleaned/` MISSING means the shard died and
  the row is retryable; `cleaned/` present with a null result means Gemini read the overview
  and had nothing to say — final, and rerunning re-buys that silence. `_row_result` now
  attaches `_cleaned` whenever the object exists rather than only when it carries fields,
  which is what makes the two distinguishable at all.
- **Its own bucket**, `empty_response_breakdown.llm_incomplete`, in both pipelines' summaries
  and as a chip on the billing card. Rolling it into `no_ai_overview` was doubly wrong: it
  overstated what Google failed to deliver and hid the one number on that card a rerun fixes.
- **A Rerun button on the run itself** for all three S3-only pipelines, plus a callout that
  renders `task_errors` — so `completed_with_errors` has a visible cause. The button's copy
  distinguishes the two costs: dead rows are re-scraped at full price, unjudged rows are not.
  Gated on `SCRAPEDO_PIPELINES`, not `NO_STATE_PIPELINES` — the question is "will
  `_find_s3_run` resolve this id", and that set omits gmaps because it answers a different
  one (which `/output` links to advertise).
- **`retry-failed-rows` counts unjudged rows too**, asking each pipeline in its own terms:
  firmographics has `pending_llm/` markers (only deferred rows owe an LLM), relationship owes
  one for every scraped row (its verdict IS the LLM, no inline path), gmaps has no LLM.

### D72. A dead Gemini shard leaves its rows retryable — no JSONL needed

Asked whether a timed-out batch should dump a JSONL to S3 so the LLM half could be retried
without re-paying scrape.do. **The scrape spend was never at risk**, and every input the
retry needs is already stored:

| what a retry needs | where it already lives |
|---|---|
| the SERP | `raw/<shard>/row_N.json` |
| our row result + cost | `rows/…` — also the phase-1 DONE marker, so a re-drive skips the row and buys nothing |
| which rows await an LLM | `pending_llm/…` markers, found with ONE list |
| the prompt | rebuilt from `rows/` by `_batch_item()`, deterministic |
| an in-flight job's name | `batches/<first>-<last>.json` |

A JSONL would be a fourth copy of the same bytes — multi-GB at 500k, and a second source of
truth to drift. Rejected.

**But the question was pointing at a real bug, in the layer below.** `is_terminal()` is true
for SUCCEEDED, FAILED, CANCELLED and EXPIRED alike — it answers "stop polling", not "there
are results". `is_success()` was supposed to draw that line and did not: for a non-SUCCEEDED
state it fell through to `done_flag and not batch_obj.get("error")`, and an expired or
cancelled job comes back `done: true` with no error body. So a dead shard read as a
successful one, `_write_fields` / `_write_verdicts` wrote a `cleaned/` object for **every key
in the shard**, and that object is the row's done-marker: the rows were blank forever, no
error raised, no retry possible, and the scrape.do credits behind them wasted. Note this is
the *likely* path — the 48h `TimeoutError` everyone worries about is the rarer one.

`engine`'s gsearch chunk driver had survived this all along by keeping a **private
`_failed_states` set** and checking it before trusting `is_success`. The two S3-only runners
were written later and never inherited the workaround. So the fix went where all three
callers route: `gemini_batch.FAILED_STATES` + `is_success` short-circuiting on it, and
engine's private copy deleted.

On top of that, `_abort_if_shard_died` in both runners: drop the job record (or the next
drive re-polls a corpse instead of buying a fresh shard) and **raise before anything is
written**, so the rows keep their `pending_llm/` marker. The raise lands in `driver.drain` →
`task_errors` → `completed_with_errors`, and `write_outputs` still runs, so the run is
honest rather than silently short. "Rerun failed" then re-publishes, the scrape phase skips
every row that has an object (0 credits), and only the pending rows are resubmitted to
Gemini. That IS the retry the JSONL was for.

**Still not done**, and it is the same shape as before: a run that ends
`completed_with_errors` this way is `completed` to the re-drive *scan*, so recovery is a
deliberate "Rerun failed", not automatic. Explicit re-drive works because `drive()` does not
check `TERMINAL_PHASES` — only `redrivable()` does.



Run `7a03eaa0` (gmaps, 100 rows) never started: no worker log line, `status.json` frozen at
`phase="queued"` with `updated_at == created_at`, zero objects under `raw/`, and Stop appeared
to do nothing. A run uploaded minutes later worked perfectly.

### D69. The publisher must declare the run queues, not only the worker

RabbitMQ told the truth by saying nothing. The `*_runs` queues were declared **and bound**
only inside `s3_run_driver.consume_runs` — which runs in the **worker**. The exchange is
DIRECT, so a run published before the worker had ever bound its queue against that broker
produced an unroutable message, and an unroutable message on a direct exchange with no
`mandatory` flag and no alternate-exchange is **silently discarded**: no error at the
publisher, no log, nothing sitting in any queue. Confirmed live — the mgmt API showed
`gmaps_runs` with 0 messages and a healthy consumer while the run sat queued.

`engine.startup_event` already does exactly the right thing for `singleRA_search_jobs`
(declare + bind at API start); the three run queues had simply never been added. Now
`declare_run_queues(channel, exchange)` does it for all three, called from BOTH startup paths.
Declaring is idempotent (same name, same durability), so either process may win the race.

**Not a new failure — it had happened before and nobody noticed.** `8ebbd930` (2026-08-18)
shows the identical signature: 100 rows, 0 scraped, 1369s of wall clock, `stopped`. Two
occurrences in a week, in the only window that matters (upload right after starting
everything), is what makes this a bug rather than an operational quirk.

**Why Stop "didn't work", and why it is not separately broken.** For an S3-only run, Stop
writes a marker and nothing else — the runner polls it between rows. With nothing driving the
run, nothing polls, so the marker sits there and the UI keeps showing `queued`. The marker is
durable and IS honoured: `run_row_phase` seeds `last_stop_check = -inf` specifically so a stop
set before the phase starts halts it on the first pass. So the run terminalises correctly —
just whenever the stale-run scan gets to it, up to `GMAPS_STALE_SEC` (900s) later. Deliberately
NOT fixed by having the stop endpoint terminalise the run itself: that would put status writes
and output writing back in the API process, which is the exact thing these pipelines moved
away from. D69 closes the window that made the delay visible.

### D70. An in-flight scrape.do run must not be labelled "SerpWow"

Second symptom of the same run, and a real bug on its own. The cost card picked its provider
from the DATA — `!!(cost.scrapedo_requests || scrapedo_credits || scrapedo_failed_requests)` —
deliberately, so a pre-migration gmaps run with real `serpwow_searches` keeps its old card.
But **zero is also what every scrape.do run reads before its first billed call lands**, so any
in-flight (or stalled) gmaps/relationship/firmographics run fell through to the default
`providerLabel = "SerpWow"`. Presence (`!= null`) cannot discriminate either: SerpWow runs
carry the `scrapedo_*` keys as 0 too.

Fix keeps the data check FIRST and adds the pipeline only as the tie-break at zero:
`SCRAPEDO_PIPELINES.has(s.pipeline) && !cost.serpwow_searches`. Both halves are pinned —
`inFlightScrapedoRunIsNotLabelledSerpWow` and `preMigrationGmapsKeepsSerpWowCard`.

### D71. The "keep concurrency at 25" guidance is withdrawn

The 2026-08-04 A/B (100 → 134s, 25 → 70.5s, "1.9x faster, do not raise") was the basis for
every throughput estimate in these docs. Re-measured from S3 counters across three completed
100-row gmaps runs, all at concurrency **100**: 44s → 36s → **34s (2.94 rows/s)**. That is 4x
the old 100 figure and 2x the best 25 ever recorded. The A/B predates the pooled
`httpx.AsyncClient` landing, which is the likeliest cause; scrape.do's own capacity changing is
the other. 500k drops from ~98h to **~47h**.

Stated precisely, because the temptation is to over-claim: this proves **100 got much faster**,
NOT that **100 beats 25 today** — there is no fresh 25 run to compare against. HANDOFF now
carries that as the owed measurement. The 429/502 attribution the docs already made stands and
is confirmed again here: 133 attempts for 100 rows, zero 429s, every failure free.

---

## 2026-08-20 — one batch toggle, not four

Asked why `LLM_BATCH=true` did or did not apply to firmographics. It did — but only because
all three per-pipeline overrides happened to be blank, which is a bad reason for a setting to
work. D53 kept them as overrides on the grounds that batching one pipeline and not another was
worth a tri-state; nobody has ever done that, and the tri-state is what made the question
un-answerable without reading four `.env` lines and the resolver.

### D68. Gemini is the only LLM provider; the OpenAI transport is deleted

Asked for directly, and the code agreed. `AI_MODE_LLM_PROVIDER=openai` reached one client
class with no tuned prompts of its own, `OPENAI_*_USD_PER_1M_TOKENS` defaulting to **0** (so a
run on it reported a $0.00 LLM bill), and `LLM_BATCH=true` already forced Gemini past it —
which D67 had just widened to every pipeline. A switch that is off in every deployment, priced
at zero when on, and overridden by the toggle you are most likely to set is not optionality,
it is an untested code path.

Deleted: `AI_MODE_LLM_PROVIDER`, the `OPENAI_*` env family (key, model, base_url, both pricing
rates), `llm_client.OpenAICompatibleClient` and its `parse_usage`, and
`calculate_llm_cost_usd`'s now-constant `provider` argument. `DEFAULT_LLM_BASE_URLS` was a
two-entry dict behind a one-provider decision, so it became the constant
`settings.GEMINI_BASE_URL`.

**Found while doing it: `settings.load_llm_config` had no callers at all** — a whole second
config path (`LLM_PROVIDER`/`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`, defaulting to openai)
left over from the standalone-script era, shadowing the real one in `ai_mode_service`. Deleted
too; it would have been the next person's wrong answer to "where does the provider come from".

**`LLMConfig.provider` went too.** First cut kept it as a *record* — it lands in
`status.json` and `final_report.json` — but that argument does not survive contact: nothing
reads it (not `run_detail.js`, not the DOM contracts, no test), and a field that can only ever
hold `"gemini"` is not a record, it is a constant shaped like a choice, which is exactly how
the next person reads it as "so I can switch it". `LLMConfig` is now
`{api_key, model, base_url=GEMINI_BASE_URL, max_retries, timeout_seconds}`, and **the only
thing env picks is the model**: `llm_batch.batch_model()` in batch mode, `GEMINI_MODEL`
inline. `validate()` is back to checking that the required values are non-empty, since there
is no longer an enum to police. `final_report.json`'s `llm` block is `{base_url, model}`.

**Kept**: `make_llm_client`, despite having no branch left — it adapts `LLMConfig` to the
client kwargs and is the seam all four offline AI-Mode tests patch, so deleting it would move
that construction into every caller and every test. `LLM_MAX_RETRIES` / `LLM_TIMEOUT_SECONDS`
are transport settings, not provider ones, and stay.

### D67. Delete the three overrides; `LLM_BATCH` is the only toggle

`AI_MODE_LLM_BATCH` / `GSEARCH_LLM_BATCH` / `FIRMOGRAPHICS_LLM_BATCH` are **gone**, and
`_OVERRIDE_KEYS` with them — `batch_enabled` is now "always-batch pipeline? → yes; toggleable
pipeline? → `LLM_BATCH`; otherwise no". This reverses D53's second half while keeping its
first: the *mechanical* knobs (shard/inflight/timeout/poll/model) stay one key each.

**Deleted, not deprecated.** A key that still quietly wins over the global is exactly the
failure being removed, so a leftover `FIRMOGRAPHICS_LLM_BATCH=true` in someone's `.env` must
be inert. `test_deleted_per_pipeline_toggles_have_no_effect` asserts both directions — the
ghost key can neither turn batching on nor off — alongside the existing guard for the deleted
`GSEARCH_GEMINI_*` names.

**The consequence was stated and accepted, not discovered.** `AI_MODE_LLM_BATCH` carried a
second meaning: it forces `provider="gemini"` over `AI_MODE_LLM_PROVIDER`, because batch
cleanup is Gemini-only. That side effect now rides the global, so `LLM_BATCH=true` moves AI
Mode's provider too. Decoupling it is a separate change (D53 already flagged it as not done);
hiding it behind a key that exists only for that purpose is not a decoupling, it is a place to
forget.

**What did NOT change**: `uses_shared_row_batch` stays a separate question from
`batch_enabled` — relationship batches through its own driver and `engine`'s gate must still
say no, or a duplicate job is seeded. `_flag`'s blank-counts-as-unset tri-state stays too,
even though with one key blank and false now agree: `.env.example` ships `NAME=`, and the
distinction between "off" and "not configured" is the resolver's to keep, not each caller's.

**Deliberately not done, same session** (asked, declined): re-adding the `output_json` column
to firmographics' CSVs — the SERP is one S3 GET away via `raw_response_s3_key` and inlining it
is ~10GB of CSV at 500k rows — and filling `enrichment_note` on partially-enriched rows, since
the six field columns already show what is missing.

---

## 2026-08-20 — firmographics off state.json: the last ceiling in the pipeline

Asked directly whether firmographics could handle 500k like gmaps and relationship. It could
not, and the honest answer was that only the PROVIDER had been migrated: the storage model
was untouched, so `update_row_state` still rewrote the whole `state.json` per row under a
per-upload lock. Bytes written grew with the SQUARE of the row count — ~7.6GB at the
documented 2.7k ceiling, **~262TB at 500k**. Not a slowdown, a wall.

### D60. Copy gmaps, do not invent

`s3_run_driver` already owns the generic half — one message per run, the bounded task window,
stop gating, the single-flight guard, the stale-run re-drive, the consumer, the
Supabase/Slack terminal write. So the new runner is ~380 lines of pipeline-specific work and
nothing else, and the endpoint/status/stop/retry paths needed a segment added rather than a
new branch (`_find_s3_run` resolves all three namespaces; `stop_upload` already routed
through it, so stop worked with zero new code).

### D61. Four object kinds, and why `pending_llm/` earns its place

`raw/` = the SERP verbatim, `rows/` = our result + cost (the phase-1 done marker, written
second so a crash re-scrapes), `cleaned/` = the six fields when a BATCH produced them.

First cut had only those three, and phase 2 computed its pending set by opening **every**
`rows/` object to ask "was this row deferred?". The test log is what exposed it — an inline
run printed `LLM phase: 3 candidate row(s)` for a phase with nothing to do, which is 500k
GETs at scale to discover there is no work. `pending_llm/` is a tiny marker written only when
batching defers a row, so the pending set is ONE paginated list and inline mode pays for a
single list call. `test_firmographics_runner` pins it by counting per-row reads.

Rejected: a `rows_deferred` counter in status.json. Counters are documented as a CACHE that a
re-drive rebuilds from the objects — deriving control flow from one would make a lost
status.json lose work.

### D62. The LLM phase is not branched around by the mode flag

`run_llm_phase` runs on every drive, inline or not, and costs one list when there is nothing
to do. Gating it on `llm_batch.batch_enabled(...)` would mean a run that batched last week
gets skipped after someone flips the toggle, stranding paid-for shards. Which mode a run used
has to be recoverable from the OBJECTS, and it is: `is_batch` in the summary comes from
`rows_cleaned`, not from current env.

### D66. The output CSV carries per-ROW facts only — and the first cut lost seven of them

Reported: "in serpwow one we had a lot more columns, why are those missing?" Three of the
four groups were dropped on purpose, one was a mistake.

Correctly gone: the **nine run-level values** the old export repeated identically on every
row (upload_id, file_status, batch_status, created_at, updated_at, total_rows,
processed_rows, success_rows, failed_rows) — `report.json` states them once; and the five
that were always blank for this pipeline (confidence_score, confidence, both
`website_company_descirption_ai`, massive_proxy_cost_usd).

Correctly REPLACED: the six `input_*` columns. The uploaded header now goes out verbatim,
which is strictly more — every column the user sent, under their own names, not six renamed
ones.

The mistake: seven genuine per-row facts (`row_index`, outcome, `summary`,
`gemini_cost_usd`, `total_cost_usd`, `processing_seconds`, `raw_response_s3_key`). Restored.

Dropped on request: `llm_model` and `scrapedo_credits`. Both were per-RUN facts wearing a
per-row column, and `llm_model` was worse than redundant — it was filled from a run-level
accumulator, so it reported `gemini-2.5-flash-lite` on a row whose LLM never ran. Their real
homes are `report.json`, `run.log` and the billing card.

`raw_response_s3_key` is blank unless a `raw/` object can actually exist: a row with no
website made no call, and one that died before returning a body has no SERP. Emitting the key
anyway sends a reader hunting for an object that was never written.

### D63. `enriched.csv` / `notEnriched.csv`, not found/notFound

Every other pipeline splits on "did we discover a website". This one is HANDED the website, so
reusing those names would publish a discovery result it never computed — the same reasoning
that keeps it out of `REPORTING_PIPELINES` and reports `confidence_mode: null`. The retry
membership rule is still the SHARED `reporting.retry_row`, so the three rerun lists cannot
drift in vocabulary.

Consequence accepted and surfaced: `/uploads/{id}/output?format=csv` — the export built on
2026-08-18 — now 404s for this pipeline, because there is no state.json to build it from.
`enriched.csv` supersedes it and is strictly better (streamed, carries the input columns).
`run_detail.js` learned `NO_STATE_PIPELINES` so it stops advertising a link that cannot work.

### D64. The test suite was not hermetic against `.env`, and it made a real API call

Found mid-migration: four failures came from `AI_DEEP_BATCH_SIZE=2` in a developer `.env`
leaking into the test process, and one from `LLM_BATCH=true` doing the same — that one flipped
AI Mode's cleanup into the Gemini BATCH path during an "offline" test, which then made a real
HTTPS call to `generativelanguage.googleapis.com` and failed with HTTP 400.

`tests/__init__.py` blanked credentials but not behaviour toggles. It now blanks both. A
credential blank stops a leak; a toggle blank stops the suite's RESULT from depending on whose
machine it runs on — and stops an offline test from spending money.

Two tests had to say what they meant rather than inherit it: one now sets
`GSEARCH_LLM_BATCH=true` explicitly, and one asserted a model against
`os.getenv(key, default)` — which returns `""` for a blank-but-present var — and now asserts
against `llm_batch.batch_model()`, the single source of truth.

### D65. Measured, not asserted

`writes/row` converges to **2.01** and `bytes/row` to **881** across 10 → 800 row runs, i.e.
flat. That is the whole claim of the migration, so it was measured against the real runner and
the real store rather than reasoned about.

## 2026-08-19 (b) — one resolver for all Gemini batch config

Pushback, fairly: adding firmographics batch mode I made `FIRMOGRAPHICS_LLM_BATCH` fall back to
**`GSEARCH_LLM_BATCH`** — a pipeline-specific key acting as a global default. That reads as
nonsense. Measuring the rest turned up more of the same.

### D53. `LLM_BATCH` is the global; the three old keys are demoted to overrides

A global default deserves a global name. The per-pipeline keys stay so one pipeline can differ
from the rest, which is a real need (batching gsearch but not AI Mode, say), but you normally
set one value.

Rejected: **one toggle with no overrides at all.** Two concrete blockers, not stylistic ones.
`relationship` has no inline path — its Gemini call IS the verdict — so the toggle would have
to be silently ignored, and a setting that cannot be honoured is worse than no setting. And
`ai_mode_service.build_ai_mode_llm_config` makes its toggle **force `provider="gemini"`** over
`AI_MODE_LLM_PROVIDER`, so a shared flag would quietly move AI Mode off an OpenAI gateway — a
billing change disguised as a transport change. AI Mode therefore keeps its own key.

**Superseded in part by D67 (2026-08-20): the three overrides are deleted.** The first blocker
never applied to them (relationship is handled by `_ALWAYS_BATCH`, not by an override); the
second was real and is now an accepted, documented consequence of the global rather than a
reason for a fourth key. The mechanical-knob half of D53 stands unchanged.

### D54. "Is it batched?" and "is it batched BY ENGINE?" are different questions

First cut collapsed them and `test_gsearch_batch_gate` caught it immediately: `relationship`
answered True to engine's gate, which would have seeded a **second, duplicate** Gemini job on
top of the one `relationship_runner` already drives. Now `batch_enabled` answers the honest
question (relationship: yes, always) and `uses_shared_row_batch` answers engine's (gsearch and
firmographics only). The test was right and the design was wrong.

### D55. Blank counts as unset — in ONE place

`env.get_bool_env` falls back only when a variable is ABSENT, while `.env.example` ships every
key as `NAME=`. So a blank override reads as an explicit "off" and silently defeats the global.
I hit this once already in `constants.py`; the tri-state read (`True` / `False` / `None` for
absent-or-blank) now lives only in the resolver, so no call site can get it wrong again.
`get_bool_env` itself is untouched — other callers depend on its absent-only semantics.

### D56. Two names for one number, deleted

`GSEARCH_GEMINI_CHUNK_SIZE` and `GSEARCH_GEMINI_MAX_INFLIGHT` were second names for
`GEMINI_BATCH_SHARD_SIZE` / `GEMINI_BATCH_MAX_INFLIGHT` with **identical defaults** (5000 / 5).
Tuning the documented pair did nothing for gsearch or firmographics. Same for the timeout
(`RELATIONSHIP_BATCH_TIMEOUT_SEC`, `AI_MODE_BATCH_TIMEOUT_SEC`) and the poll interval
(`AI_MODE_BATCH_POLL_SEC`). All deleted; a test asserts setting the dead names has no effect,
so they cannot creep back as a silent alias.

Unified timeout is **48h**, Gemini's own job expiry, not the old 1800. A Batch job runs and
bills on Google's side whether or not we are still polling, so a 30-minute deadline abandons
work already paid for. Closes HANDOFF blocker #1.

### D57. The model fallback was a live bug, so the test guards the invariant not the instance

`relationship_runner` read `GEMINI_BATCH_MODEL` with no `GEMINI_MODEL` fallback: setting only
`GEMINI_MODEL=gemini-2.5-pro` moved three pipelines and left relationship on flash-lite.
Rather than assert that one line is fixed, the test **scans every pipeline module for a direct
env read of any batch key** and fails if one exists. Verified by reintroducing the bug and
watching it fail, then restoring — a guard nobody has checked can fail is not a guard.

### D58. Blocker #4 is relationship-only, so the fix is small

HANDOFF says raising `GEMINI_BATCH_MAX_INFLIGHT` "creates nothing" because each shard holds a
thread. That is true of exactly one of the three drivers:

- `engine._run_one_gemini_chunk` — `await asyncio.sleep` between polls; a thread only for the
  brief `get_batch`. Bounded by async tasks.
- `ai_mode_service` — polls every in-flight shard inside ONE `to_thread`.
- `relationship_runner._poll_to_terminal` — blocking `time.sleep` INSIDE a thread, one per
  in-flight shard for the job's whole life. This is the one.

So the fix is `loop.set_default_executor(ThreadPoolExecutor(...))` at worker startup, exactly
as that function's own docstring prescribes — not a rewrite of the poll loop. Sized
`max(32, max_inflight + 16)`: that executor is shared with every S3 write and CSV parse in the
worker, so sizing it to the batch count alone would trade one bottleneck for a worse one.

### D59. Operational note, not a code change

The live `.env` pins `GEMINI_BATCH_MODEL=gemini-2.5-flash-lite` and
`GEMINI_BATCH_TIMEOUT_SEC=1800`, and has `GSEARCH_LLM_BATCH=true` with no `LLM_BATCH`. The new
defaults do not reach that deployment until those lines are edited — deliberately left to the
user rather than changed under them.

## 2026-08-19 — firmographics onto scrape.do Google Search; `xlsx_export` renamed

### D37. `xlsx_export.py` → `output_export.py`

The module writes CSV *and* XLSX since yesterday, and CSV is now the advertised format, so
the filename named the secondary one. `output_export` pairs with `csv_input` (input parsing
↔ output export) and stays clear of the `*_outputs.py` family (`gmaps_outputs`,
`relationship_outputs`), which writes run-level found/notFound files rather than the
per-upload table. Blast radius was four references.

### D38. Two endpoints, because the AI Overview is not always ready

`/plugin/google/search` returns `ai_overview` inline only when
`ai_overview.state == "complete"`. On `"deferred"` the content needs a second call to
`/plugin/google/search/ai-overview?session_key=…`. Both are implemented; a row costs 10
credits normally and 15 when Google defers.

Rejected: treating a deferred row as a not-found and skipping the follow-up. It would make
the fill rate depend on Google's generation latency, which is not a property of the company
being enriched — the same row would enrich or not depending on when it ran.

### D39. The deferred follow-up is NEVER retried

The session key is single-use and expires after 60 seconds; a reused or expired key returns
`404 {"error": "session not found"}`. A retry therefore *cannot* succeed, so the client
makes exactly one attempt and, on failure, keeps the search result it already paid for. The
failure is recorded as `ai_overview_error` and the row is **not** an error: the search was
billed and its SERP is intact, there is just no overview to normalise. It counts as
`billed_no_overview` — the refund-claim bucket — which is the honest classification.

This is the one place in the repo where "retry transient failures" is wrong, so it is
called out in the client rather than left to be re-derived.

### D40. The deferred stub is discarded before the follow-up

Caught by a test, not by review: on a deferred row the inline `ai_overview` block is
`{state, session_key}` — a stub with no content. Leaving it in place made
`billed_no_overview` false (the dict is non-empty) and would have handed the stub to the
LLM as if it were an overview. It is now dropped, and only what the follow-up returns
counts as content. The same test run caught `deferred` being derived from the FINAL state,
which a successful follow-up rewrites to `"complete"` — hiding the 5 credits it had just
cost. `deferred` is now recorded independently.

### D41. Credits only; the sole USD figure is the LLM

`serpwow_cost_usd` and `massive_proxy_cost_usd` are left `None` rather than `0.0` —
"not applicable" instead of a misleading zero, the convention gmaps set — and
`total_cost_usd == gemini_cost_usd`. The per-ENDPOINT counts are kept alongside the
credit total because 10 and 5 credits are not interchangeable: a 100-row run billing 1150
credits is unexplainable from `scrapedo_credits` alone, but obvious as
"100 searches + 30 deferred follow-ups".

### D42. `COST_SUMMARY_PIPELINES`, a superset of `REPORTING_PIPELINES`

firmographics needs the billing card, the Supabase cost fields and the Slack credit line.
It does **not** need `found.csv`/`notFound.csv`: it is handed the website, so that split
would publish a discovery result it never computed. Rather than add it to
`REPORTING_PIPELINES` and then special-case the file writer, the two concerns got two
sets — money at the `/status`/Supabase/Slack sites, files still on `REPORTING_PIPELINES`.

Rejected: a bespoke firmographics summary block on `/status`. `build_summary` already
routes on the presence of the `scrapedo_*` cost keys, so the existing one worked unchanged
once the pipeline emitted them; a parallel implementation would be a second thing to keep
in sync with the UI.

### D43. `official_website` stops meaning "found" for this pipeline

The migration surfaced a pre-existing bug rather than causing it: `classify_finalized_row`
checks `official_website` first, and for firmographics that is the INPUT echoed back. Every
row was therefore `found` — including rows whose provider call failed outright, which
reported success at $0. A firmographics branch now classifies on what the row *produced*:
provider error ⇒ `error`, no overview or no extracted fields ⇒ `not_found`, fields ⇒
`found`.

The no-explicit-outcome fallback existed in **two** places with the same wrong test
(`_derive_outcome` in reporting, `_outcome_of` in `summarize_upload_state`). Fixing one
would have made report.json and `/status` disagree about the same run, so both now call one
shared `reporting.row_produced_a_result(result, pipeline)`.

firmographics also joined the 3-way `row_status` remap: a website with no AI overview is
`completed`/`not_found`, not a `failed` row that "Rerun failed" would re-buy at 10 credits
to get the same empty answer.

### D44. `codetails.py` deleted

firmographics was its only caller. It also `print`ed the full request URL — API key
included — to stdout on every row, and re-read `.env` at import time inside the worker.
Its query wording survives as `modes.common.build_firmographics_query` so migrated runs
stay comparable with the SerpWow ones.

### D45b. SerpWow removed from the firmographics path — where it stops

Removed: `codetails.py`; `standardize_serpwow_ai_overview_with_gemini` →
`standardize_ai_overview_with_gemini` (firmographics is its only caller, and its prompt no
longer tells the model it is reading a "SerpWow AI Overview"); the explicit
`serpwow_cost_usd=None` / `massive_proxy_cost_usd=None` arguments (they are already the
schema defaults, so passing them only put the old provider's name back in the module); and
the per-row raw object's name.

That last one mattered most: every firmographics row was being stored as
`serpwow_response/000001_x_serpwow.json` while containing a scrape.do SERP. It is now
`search_response/000001_x_search.json`, chosen by `engine._raw_artifact_names(pipeline)`.
gsearch deliberately keeps `serpwow_response/` — it IS still on SerpWow, and changing it
would split its pre- and post-change runs across two folders for no gain. The shared
uploader lost its provider name too (`upload_raw_response_to_s3`), because it serves both.

**Kept SerpWow-named, deliberately:** the `services/serpwow/` package path (the agreed last
rename), the shared `reporting` module, and `CrawlResponse.serpwow_cost_usd` /
`s3_serpwow_json_key` with their CSV columns. Those are cross-pipeline schema: renaming
them changes gsearch's output columns and every stored row's shape, which is a migration,
not a cleanup.

### D49. `serpwow_reporting.py` → `reporting.py`

Asked directly: why is there still a file with that name? Because it was named when SerpWow
was the only provider, and by now three of the four pipelines it serves are on scrape.do. It
never contained provider-specific code — its cost accounting routes on the DATA (does the
row's `cost_breakdown` carry `scrapedo_*` keys?), which is exactly why adding two migrated
pipelines to it needed no branch. `reporting.py` inside `services/serpwow/` is unambiguous;
the package rename stays the agreed last step.

Deliberately left: `CrawlResponse.serpwow_cost_usd`. It is cross-pipeline schema and appears
in gsearch's output; renaming it is a migration of every stored row, not a cleanup.

### D50. firmographics batch mode rides `GSEARCH_LLM_BATCH`

`FIRMOGRAPHICS_LLM_BATCH` defaults to whatever `GSEARCH_LLM_BATCH` is, so one switch moves
both, and a dedicated key exists only for when they should differ. Same fallback idiom the
repo already uses for `AI_MODE_WORKER_CONCURRENCY` → `WORKER_CONCURRENCY`.

**Blank counts as unset.** `get_bool_env` falls back only when the variable is ABSENT, and
`.env.example` ships every key as `NAME=` — so `FIRMOGRAPHICS_LLM_BATCH=` sitting blank in a
real `.env` would have returned False and quietly made "one toggle for both" untrue. Fixed in
the helper, not in `get_bool_env`, whose absent-only semantics other callers depend on.

Note for whoever deploys this: the live `.env` has `GSEARCH_LLM_BATCH=true`, so firmographics
batching becomes ACTIVE on the next worker restart without anyone setting a new key. That is
what "use the same toggle" means, but it is a behaviour change, not a no-op.

### D51. One gate, one prompt — the duplication was the real risk

The batch/inline decision existed twice: `engine._batch_postprocess_enabled_for` and a second
`_get_bool_env("GSEARCH_LLM_BATCH")` inside `modes/gsearch`. Adding a third copy for
firmographics is how the executor ends up batching while the engine thinks it is inline (or
vice versa) — one pays twice, the other never fills the fields. It now lives once, in
`constants.py`, next to the other per-pipeline policy sets.

Same reasoning for the prompt: the normalisation text was inline in
`standardize_ai_overview_with_gemini`, and the batch path builds its own request. Two copies
means a batched run and an inline run can answer the same row differently — the one thing a
mode toggle must never do. Extracted to `gemini_llm.build_ai_overview_prompt`, used by both;
a test asserts the batch request carries it.

### D52. Rows with no AI overview are never submitted to the batch

Nothing to normalise, so a request could only come back empty — and Gemini Batch bills input
tokens for it. Those rows are already terminal as `not_found` from the scrape phase. Costs
one `continue` in `_build_batch_items_for_state`.

### D47. Two misnamed S3-key columns collapse into one

`s3_html_key` and `s3_serpwow_json_key` were separate CSV columns that always held the
**same value** (the worker assigned one from the other), under two names that were both
wrong: nothing ever contained HTML, and the SerpWow one carried a scrape.do payload on every
migrated pipeline. Now one `raw_response_s3_key`. Nothing outside the export and the state
plumbing read either — no UI, no endpoint — so this was a deletion, not a migration.

`engine._raw_response_key(row)` reads the new field then both legacy names, so a run written
before today still shows its artifact on the detail page. Keeping the fallback is 4 lines;
the alternative is a rewrite of every stored `state.json`.

Rejected: keeping `s3_serpwow_json_key` as a duplicate alias "for compatibility". Nothing
consumes it, and a second column of identical data is what created the confusion.

### D48. LLM tag, model and per-row phase times for firmographics

The run header showed nothing about the LLM, because `confidence_mode: None` (D43's honest
answer — the pipeline is handed the website) also gated the Batch and Model chips. A paid
Gemini call per row was invisible, including whether it was batched.

`build_summary` now emits `llm_mode` and `model`; the UI has an `else if (g?.llm_mode)`
branch for "uses an LLM, but not for confidence" and renders `LLM: Inline` + `Model: …`.
Answering "is batch on?" in the UI is the point — it is a real cost question and the code
gives one answer (`_batch_postprocess_enabled_for` is gsearch-only), so the page should say
so rather than leave it to be read out of the source.

**Times are per-row AVERAGES, not sums, and labelled `/row`.** gmaps and AI Mode have
run-level phases (scrape everything, then clean everything) so their `phase_seconds` are
real wall-clock durations. firmographics interleaves both per row across many workers, so
there is no such thing as "the scraping phase" — a sum of per-row seconds at concurrency 100
would print as many times the run's actual duration and look like a bug in the timer.
Averages are the only honest reduction, and the `/row` suffix stops them being read as wall
clock.

Rejected: reusing the existing `phase_seconds` keys (`scraping`/`cleaning`). Zero UI change,
but it would put a per-row average behind a label the other pipelines use for wall clock,
which is exactly the kind of quiet unit mismatch nobody catches later.

Also fixed on the way: the worker **replaced** `context["timing"]` with
`{total_seconds: …}`, silently dropping any split an executor had already recorded. It
merges now.

### D46. Not done: `knowledge_graph` is fetched and still unread

The Search endpoint returns `knowledge_graph` (address, phone, founded, HQ, type) and
`organic_results` in the same 10-credit call, and the pipeline still normalises
`ai_overview` alone — so the fill-rate upside of the migration is unclaimed, and a row with
no overview reports nothing despite the SERP holding real contact data. The whole SERP IS
persisted as the row's raw artifact, so this can be claimed later without re-buying a
single call. Left out because it changes what the LLM is asked, which is a prompt decision,
not a provider swap.

## 2026-08-18 — per-row output: CSV instead of XLSX, and the UTF-8 fix

A 71-row firmographics run was downloaded as XLSX. Two problems: the format is awkward to
diff/load, and non-ASCII output (`ul. Leśna 10`, Turkish depot addresses, Bengali phone
lists) needed the encoding pinned down rather than trusted.

### D33. One table builder, two writers

`build_upload_output_table(output_data)` now owns the 38-column header and the per-row
extraction; `build_upload_output_xlsx_bytes` and the new `build_upload_output_csv_bytes`
both call it. A second copy of that column list is the obvious alternative and is exactly
how the two exports would silently drift — a column added for the CSV and missing from the
XLSX is the kind of bug nobody notices for a month. `test_output_csv.py` asserts the CSV
header equals the table's, so the drift is caught mechanically, not by review.

### D34. `utf-8-sig`, not plain UTF-8 — the BOM *is* the fix

Excel decodes a BOM-less CSV as the OS legacy codepage, which is what renders `Leśna` as
`LeÅ›na`. Nothing about the bytes is wrong in that case and no amount of `charset=utf-8` in
the response header changes it, because a double-clicked file never sees the header. The
3-byte BOM is what switches Excel to UTF-8, and `csv`/`pandas` strip it automatically via
`utf-8-sig`, so no downstream reader regresses. CRLF (the `excel` dialect default) for the
same audience. `Content-Type: text/csv; charset=utf-8` is still set, for the *inline browser
preview* path where the header is what's honoured.

Rejected: UTF-16LE + tab separator (the other thing Excel opens reliably). It defeats every
plain-text tool and `grep`, for a file whose main appeal over XLSX is being plain text.

### D35. The CSV does NOT inherit the XLSX cell cap

`_sanitize_excel_text` truncates at 32767 because that is Excel's per-cell limit. A CSV has
no such limit, and `output_json` is the one column where the tail matters — a silently
truncated blob looks like valid JSON that won't parse. So `_csv_text` keeps the control-char
strip (NUL genuinely breaks readers) and drops the cap. Consequence accepted: a >32767-char
field will annoy Excel in that one cell. Losing data to protect a spreadsheet's feelings is
the worse trade.

Also `ensure_ascii=False` for `output_json` in the CSV path only, so that column reads as
`Leśna` rather than `Leśna`. The XLSX path keeps `ensure_ascii=True` — its XML writer
is ASCII-safe by construction and there was no reason to touch it.

### D36. XLSX kept, just unlinked

`?format=xlsx` still answers; only the UI links changed (`run_detail.js` Files card →
`output.csv`, `operations.js` history column → CSV). Deleting the format would break
bookmarks and the two DOM contracts for a saving of one branch, and the ask was about which
format is *offered*, not about removing one. `output_csv_url` was added next to
`output_xlsx_url` in both upload/summary payloads rather than replacing it.

## 2026-08-18 — firmographics: the preview now uses its own parser

Follow-on from the rename below, reported from the UI: uploading a firmographics CSV
previewed as *"No recognized headers found; positional parsing used (column 1 = company
name, column 2 = country)"* — about a file whose first column is a URL.

### D29. Preview with the parser the UPLOAD uses, not a shared one

`/uploads/preview` runs `parse_entities_csv`, which requires company_name + country. A
firmographics CSV has neither, so it fell through to the positional branch and reported a
mapping the upload would never use. Relationship already had its own preview endpoint for
exactly this reason; firmographics now has `/uploads/firmographics/preview` alongside it,
and `new_run.js` picks the endpoint per pipeline from a small table.

Rejected: a `pipeline` form field on the shared preview. It would put a per-pipeline
branch inside a route that currently has none, for the same number of lines, and the
two-endpoint precedent already exists.

### D30. One alias table, shared by the parser and the preview

`firmographics_columns(fieldnames)` in `csv_input.py` replaces six near-identical
alias loops (net line **deletion**) and is what both the parser and the preview resolve
through — so the preview cannot advertise a mapping the upload disagrees with. The
sample rows come from the real parser output, so what the preview shows is literally
what will be enriched.

### D32. No invented company names — use the columns the file has

`company_name` fell back to the URL's domain in TWO places (`csv_input` when the CSV had
no company column, and the executor when the value was blank), so `acme.com` appeared in
the output looking like a real company name and went into the LLM prompt as one. Both
fallbacks deleted; an absent column now means an empty cell. `_domain_from_url` drops out
of both modules with it.

Deletion over addition: the honest empty value is also the smaller diff, and a blank cell
is unambiguous where a fabricated one is not.

### D31. Firmographics accepts the same company aliases as everything else

Found while eyeballing the endpoint against a real `found.csv`: its company column is
`entity_name` (the input's own header, passed through), which `parse_entities_csv`
accepts but firmographics did not — so a round-tripped file silently fell back to the
DOMAIN as the company name for every row. The alias list is now the canonical one
(`company_name|company|name|entity_name|entity|organization|organisation|legal_name`).
That round-trip is the whole point of the `website_url` rename, so it has to work.

---

## 2026-08-18 (a) — firmographics' input column is `website_url`

The firmographics upload required `official_website` (aliases `website`/`url`/`domain`) —
and did **not** accept `website_url`, which is the exact name every other pipeline WRITES
into found.csv. So the natural workflow (run gmaps or AI Mode, feed the found rows into
firmographics for enrichment) failed validation on the file we had just produced.

### D27. Rename the user-facing column, keep the old names as aliases

`website_url` is now the canonical name — first in the alias tuple, and the name in both
error messages and the "Expected CSV columns" chip in `new_run.js`.
`official_website`/`website`/`url`/`domain` still resolve, so no existing CSV breaks.

### D28. The internal key stays `official_website`

The parsed row's dict key, the state row's field, the executor argument and the XLSX's
`input_official_website` column are untouched. Renaming those is a different change with a
real blast radius (state rows, the firmographics executor, the XLSX schema) and no user
benefit — the ask was about the column a user has to type in their CSV. The mapping is
one line in `csv_input.parse_firmographics_csv_rows`.

Covered by `tests/test_firmographics_csv.py` — the first test this parser has ever had:
`website_url` resolves, every legacy alias still resolves, and the error message names
the column it actually wants.

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

### D21. `passthrough_fieldnames` lives in `common/text.py`, not `reporting.py`

`ai_mode/run_reporting.py` is deliberately standalone (docstring: models + `serpwow.outcomes`
only), and `reporting` pulls in `serpwow_client` → `httpx`. `common/text.py` is
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

### D12. One shared helper, in `reporting`

`retry_column()` + `retry_row()` live in `reporting.py` — already the shared,
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
