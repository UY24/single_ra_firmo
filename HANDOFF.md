# HANDOFF — `website_url_finder`

Last updated: 2026-07-15. Read this first if you're picking up this repo. Durable architecture (module map, S3 layout, pipeline internals) lives in `CLAUDE.md`; older dated sessions are archived in `docs/HISTORY.md`.

---

## Current status

- **Active branch: `adaptiveRelationshipSearch`**, based on `aiModeBroker`. Adaptive grounded relationship search implementation and focused verification are complete.
- Focused relationship suite: **158/158** passing. The full suite is still pending Task 7; do not record a new full-suite count until it runs.
- Commits after `426b3e6` are local-only. Do not push or otherwise change the remote without explicit approval.

## What works now

Two independent discovery systems behind one FastAPI app + vanilla-JS UI (see `CLAUDE.md` for the architecture):

- **SerpWow pipelines** — `full`, `url_discovery`, `firmographics`, `gmaps`, `gsearch`, `relationship`. gsearch/gmaps/relationship have confidence scoring + `found.csv`/`notFound.csv`/`report.json`/`run.log` output parity, S3 mirroring, Supabase counters, and run-detail UI.
- **AI Mode** — `ai_bulk`, `ai_deep` (scrape.do -> LLM cleanup), **now broker-driven** (RabbitMQ scrape phase in the worker process, reconciler self-healing, streaming assembly, Gemini Batch API support). See the AI Mode section in `CLAUDE.md`.
- Cross-cutting: unified CSV input, per-company/per-pipeline S3 layout, Supabase run tracking, best-effort Slack completion/failure pings (with split LLM vs SerpWow cost), the redesigned dark UI across all views.

## Active risks / unfinished work

- **`aiModeBroker` is NOT live-verified** — offline tests only (532/532). Live smoke needed before relying on it (see its older session below for the checklist). **AI Mode now REQUIRES RabbitMQ + the worker process** — uploads 503 without the broker; the worker env needs `SCRAPEDO_TOKEN` + LLM keys + S3/Supabase/Slack vars (shared repo-root `.env` covers same-host setups). Run exactly ONE worker process.
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

1. Run Task 7's full offline suite (`cd backend && ../.venv/bin/python -m unittest discover -s tests -t .`).
2. Live-smoke the relationship pipeline against SerpWow; verify adaptive request counts, grounded decisions, and confirmed/no-URL output before deployment.
3. **Live-smoke `aiModeBroker`** (checklist in the older session notes below), then decide merge/push (needs user approval to push).

## Conventions (do not break)

- **Never** add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits — tell any committing subagent the same.
- **Never** `git push` or query the production Supabase DB without explicit user approval.
- `docs/` is **gitignored** — specs/plans/handoff-history there are on-disk only; code under `backend/` commits normally.

---

## Latest completed session — 2026-07-15 (adaptive grounded relationship search, branch `adaptiveRelationshipSearch`)

**Status: implementation and focused verification complete; full suite pending Task 7.** The branch is based on `aiModeBroker`. The focused relationship suite is **158/158**; a live SerpWow smoke is still required.

- Search is an ordered registry: phase 1 combined relationship + URL, phase 2 financial evidence, phase 3 URL recovery. `RELATIONSHIP_SEARCH_POLICY` defaults to adaptive missing-evidence/missing-URL routing; `sequential` runs registry order. `RELATIONSHIP_MAX_PHASES` caps actual requests per unique X↔Y pair.
- Candidate extraction now preserves typed-URL and bare-domain provenance and fixes valid-domain handling seen with Modal while rejecting IP/file/X-domain candidates.
- Gemini is evidence-only in sync and batch modes: the shared, complete-record-bounded evidence set and gate reject outside knowledge, invented URLs, unknown IDs, and confirmations without supplied relationship evidence. Compatibility without evidence IDs is restricted to flagged legacy batch artifacts.
- Confirmed relationships with no validated Y URL remain `notFound` with reason `confirmed_relationship_url_not_validated` and retain accepted supporting evidence in CSV/report output.
- Commits after `426b3e6` are local-only; do not push or modify the remote without explicit approval.

## Older session — 2026-07-14 (AI Mode → RabbitMQ broker rework, branch `aiModeBroker`)

**Status at that session: code complete (3 commits), 532/532 offline, NOT live-verified.** Full architecture now documented in `CLAUDE.md` (AI Mode engine section). Motivation: 500k–1M-row recurring runs — the old in-process `run_ai_mode_sync` held every scrape payload + all results in RAM (OOM at scale), had no redelivery (a crash meant a manual whole-run re-drive), and a hard kill left runs showing `running` forever with the "Rerun failed" button unreachable.

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
