# HANDOFF — `website_url_finder`

Last updated: 2026-07-13. Read this first if you're picking up this repo. Durable architecture (module map, S3 layout, pipeline internals) lives in `CLAUDE.md`; older dated sessions are archived in `docs/HISTORY.md`.

---

## Current status

- **Active branch: `uiuximp`** — the "Midnight Ledger" UI/UX redesign + batch-lifecycle hardening + Slack split-cost precision. **COMPLETE and reviewed** (no remaining Critical/Important issues). Working tree clean; **local commits are NOT yet pushed** to `origin/uiuximp`.
- Full suite: **487/487** passing.
  ```bash
  cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
  ```
  (`-t .` is **mandatory** — it makes `tests/__init__.py`'s hermeticity guard run so real cloud creds from `.env` don't leak into tests.)

## What works now

Two independent discovery systems behind one FastAPI app + vanilla-JS UI (see `CLAUDE.md` for the architecture):

- **SerpWow pipelines** — `full`, `url_discovery`, `firmographics`, `gmaps`, `gsearch`, `relationship`. gsearch/gmaps/relationship have confidence scoring + `found.csv`/`notFound.csv`/`report.json`/`run.log` output parity, S3 mirroring, Supabase counters, and run-detail UI.
- **AI Mode** — `ai_bulk`, `ai_deep` (scrape.do -> LLM cleanup), resume-aware, Gemini Batch API support.
- Cross-cutting: unified CSV input, per-company/per-pipeline S3 layout, Supabase run tracking, best-effort Slack completion/failure pings (now with split LLM vs SerpWow cost), the redesigned dark UI across all views.

## Active risks / unfinished work

- **`uiuximp` is NOT live-verified** — offline tests only. Before relying on it, run a live smoke: one reporting run (row processing -> batch finalization -> files available), one cancel/delete from Operations, one explicit retry, one Slack completion message confirming split costs. **Restart both API server and worker** first — several changes are in `services/serpwow/engine.py`, not static UI only.
- **`errorTaxonomy` branch (2026-07-10) is NOT merged — user's call.** Introduces the `found`/`not_found`/`error` outcome taxonomy (error source+category, `not_found` becomes `completed`). Reviewed, 416/416 offline, but **not live-verified** (needs a run showing the found/not_found split and a forced SerpWow 429/timeout landing as a real `error/serpwow` rather than a silent not_found). Details in `docs/HISTORY.md` (2026-07-10).
- **Other historical branches** (`gmapsfix`, `gsearchFix`, `relationshipMode`) — `relationshipMode` was merged into `revampCode` via PRs #5/#6; the gmaps/gsearch branches carry earlier features that are captured in `CLAUDE.md`. See `docs/HISTORY.md` if you need their state.

## Run / test commands

```bash
# Web server (FastAPI app is defined in serpwow/engine.py; main.py wires the rest)
cd backend && ../.venv/bin/python -m app.main       # or: ../.venv/bin/python ../run.py
# UI at http://localhost:<API_PORT>/app  (default port 11500; .env overrides)

# SerpWow worker — needed ONLY for SerpWow pipelines, not AI Mode
docker compose up -d rabbitmq                        # broker + mgmt UI on 15672
python worker.py                                     # from repo root

# Tests (offline, unittest — NOT pytest; -t . is mandatory)
cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
```

## Immediate next steps

1. Decide the source-control move for `uiuximp`: push the remaining local tip and open/refresh the PR, or merge into the chosen base branch.
2. Run the live smoke test above (restart server + worker first).
3. Decide whether to merge `errorTaxonomy`, and live-verify it if so.

## Conventions (do not break)

- **Never** add a `Co-Authored-By: Claude` (or any AI-attribution) trailer to commits — tell any committing subagent the same.
- **Never** `git push` or query the production Supabase DB without explicit user approval.
- `docs/` is **gitignored** — specs/plans/handoff-history there are on-disk only; code under `backend/` commits normally.

---

## Latest completed session — 2026-07-13 (Midnight Ledger UI/UX + Slack cost precision, branch `uiuximp`)

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
