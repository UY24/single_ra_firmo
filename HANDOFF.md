# HANDOFF — `website_url_finder`

Last updated: 2026-08-05. Architecture lives in `CLAUDE.md`; every dated session write-up is
archived newest-first in `docs/HISTORY.md`. This file is only current state + what's owed.

## Status

- Branch **`relationship-scrapedo`**, tree clean, **12 commits ahead of `origin/relationship-scrapedo`** (pushed branch exists; these are not on it).
- **723/723** offline tests + **6/6** `.mjs` DOM contracts passing.
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

cd backend && ../.venv/bin/python -m unittest discover -s tests -t .   # 723; -t . is MANDATORY
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
   client timeout. It also ignores its `limit` param, and `relationship_store.delete_object`
   swallows failures, so a row can count as retried and never be rescraped. Fix: `delete_objects`
   in 1000-key batches + honour `limit`.
3. **`phase="failed"` is terminal for the re-drive scan.** Any transient error over a multi-day
   run parks it permanently; recovery is hand-typing the run id into Operations. Fix: attempts
   counter in `status.json` (AI Mode's `requeue_attempts` pattern) and let the scan pick `failed`
   up N times — a re-drive is free.
4. **`GEMINI_BATCH_MAX_INFLIGHT` is silently capped.** Each shard holds a thread for its whole
   multi-hour poll, so raising it past the default `ThreadPoolExecutor`'s `min(32, cpu+4)`
   (**6 on a 2-vCPU box**) creates nothing. Needs a sized executor.
5. **gmaps state store.** `update_row_state` rewrites the whole `state.json` per row (ceiling
   ~2.7k rows) and `report.json` embeds every row. Agreed direction: a small gmaps runner reusing
   AI Mode's *primitives* (`s3_sync`, `StreamingRunReport`, own queue, O(1) counters,
   file-presence-as-state) — not its `ModeConfig`/LLM flow. Message carries a batch of ~20 rows,
   files stay per row. Needs sharded row dirs.
6. **API-process memory.** `parse_relationship_csv` builds two full dict copies of every row for
   preview and upload (>1GB twice at 500k); `/uploads/{id}/result` reads a whole several-hundred-MB
   CSV into memory instead of streaming the boto3 body.

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

**gmaps:** validate the 502-retry trade (compare `rows_no_listing` against the 12 seen before —
same number means the retries bought only ~30% wall time), test `WORKER_CONCURRENCY=10` for the
throughput knee, and confirm Slack + Supabase on a terminal run (the only parts never seen live).

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
