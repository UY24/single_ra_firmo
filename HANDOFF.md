# HANDOFF — `website_url_finder`

Last updated: 2026-06-07. Read this first if you're picking up this repo.

This project now contains **two independent website-discovery systems** that share one FastAPI app
+ one UI:

1. **SerpWow pipelines** (the original) — RabbitMQ + worker + S3, 5 modes (`full`, `url_discovery`,
   `firmographics`, `gmaps`, `gsearch`). Fully documented in `docs/` (start at
   [`docs/README.md`](docs/README.md)).
2. **AI Mode** (added this session) — a standalone scrape.do "Google AI Mode" + LLM-cleanup
   pipeline, vendored from the sibling `scrapeDo` project. Documented in
   [`docs/13.ai-mode.md`](docs/13.ai-mode.md).

There is **no** `requirements.txt`/`Dockerfile`. Deps are inferred from imports — see
[`docs/09.how-to-run.md`](docs/09.how-to-run.md). Python 3.12.

---

## 1. What changed recently (this session)

1. **Full docs set written + codex-reviewed** — `docs/01`…`docs/12` describe the SerpWow service
   accurately (incl. as-built caveats). Three codex findings were verified true and fixed in the
   docs (see `docs/01` §7, `docs/02`, `docs/05`, `docs/08`).
2. **AI Mode v1 built** — new tab + endpoints + vendored package + glue. Sequential (batches of
   10). See §3.
3. **Per-flow timing added to the SerpWow modes** — see §4.

Pending decisions/TODOs are in §6. **Read §6 before doing more scrapeDo work.**

---

## 2. Repo map (key files)

```
app.py                  # the whole SerpWow service (FastAPI, worker, Gemini batch, XLSX) — 7.4k lines
                        #   + 4 NEW AI-mode endpoints near the end (POST/GET /uploads/ai-mode*)
ai_mode_service.py      # NEW — AI Mode orchestrator (drives scrapedo_finder, writes ai_mode_result/)
scrapedo_finder/        # NEW — vendored scrape.do package (see §3 + §6 for the file list)
worker.py               # SerpWow worker entrypoint (NOT needed for AI Mode)
gmaps.py, codetails.py  # SerpWow Google-Maps + firmographics clients
templates/ui.html       # single-page UI; 7 tabs now (7th = "AI Mode")
docs/                   # numbered documentation set (01..13) + 11.handoff.md (SerpWow handoff)
ai_mode_result/         # NEW — per-run AI-mode output (gitignored; created on first run)
.env.example            # all env vars (SerpWow + AI Mode); some legacy keys nothing reads (docs/02 §B)
plancl.md               # the approved implementation plan for AI Mode
```

---

## 3. AI Mode (the new feature)

**Goal:** upload a CSV → for each batch of 10 companies, send ONE prompt to scrape.do Google AI
Mode, then clean the response with an LLM (Gemini *or* OpenAI) → save raw responses + cleaned
results + timings under `ai_mode_result/<run_id>/`. Standalone: **no RabbitMQ/S3/state.json**.

**Flow:** `POST /uploads/ai-mode` → `ai_mode_service.prepare_ai_mode_run` (validate CSV, detect
mode, register run) → API schedules `asyncio.to_thread(run_ai_mode_sync, run_id)` → orchestrator
loops batches: Phase 1 scrape.do (timed) → Phase 2 LLM clean (timed) → writes outputs. UI polls
`/uploads/ai-mode/<run_id>/status` every 2 s.

**Endpoints** (in `app.py`, just before `if __name__ == "__main__":`):
- `POST /uploads/ai-mode` (multipart `file`) → `{run_id, total_rows, input_type, llm_provider, llm_model, batch_size, status_url, result_url}`
- `GET /uploads/ai-mode` → `{count, runs:[...]}`
- `GET /uploads/ai-mode/{run_id}/status` → status dict
- `GET /uploads/ai-mode/{run_id}/result?file=<name>&download=true` → file (allowlist:
  `final_report.json, found.csv, notFound.csv, report.json, run.log, input.csv`)
- Module-level `ai_mode_tasks: set[asyncio.Task]` keeps background tasks alive.

**`ai_mode_service.py` public API** (other code depends on these names):
`prepare_ai_mode_run(raw_csv, filename)`, `run_ai_mode_sync(run_id)`, `list_ai_mode_runs()`,
`get_ai_mode_status(run_id)`, `get_ai_mode_result_path(run_id, file_name)`. Plus helpers
`build_ai_mode_settings()`, `build_ai_mode_llm_config()`, `load_address_entities()`.

**Input auto-detect:** header `Company Name ENG` → **company** mode (`load_company_entities`);
otherwise **address** mode (flexible mapping: `entity_name|company_name|company|name` + `country`
+ `address|input_full_address|full_address`). So existing SerpWow CSVs work as address mode.

**LLM provider (env-switchable):** `AI_MODE_LLM_PROVIDER=gemini|openai` (default `gemini`).
Gemini reuses existing `GEMINI_API_KEY` + `GEMINI_MODEL`; OpenAI uses `OPENAI_API_KEY` +
`OPENAI_MODEL` (+ optional `OPENAI_BASE_URL`). Provider switching is implemented inside the
vendored `scrapedo_finder/llm_client.py` (`make_llm_client`); the glue just maps env → `LLMConfig`.

**On-disk per run** `ai_mode_result/<run_id>/`: `input.csv`, `status.json`, `report.json`,
`final_report.json`, `found.csv`, `notFound.csv`, `run.log`, `raw_scrapedo_response/request_NNN.json`.

**Timing (the key requirement):** `final_report.json.requests[]` has per-batch
`{scrapedo_seconds, llm_seconds, combined_seconds}`; `summary` has `scrapedo_seconds_total`,
`llm_seconds_total`, `batch_duration_seconds`, `token_usage`. `status.json` carries running
totals so the UI shows live progress.

**Env vars (AI Mode):** `SCRAPEDO_TOKEN` (required), `SCRAPEDO_BATCH_SIZE` (10), `SCRAPEDO_*`
timeouts/locale; `AI_MODE_LLM_PROVIDER`, `GEMINI_API_KEY`/`GEMINI_MODEL` or
`OPENAI_API_KEY`/`OPENAI_MODEL`/`OPENAI_BASE_URL`, `LLM_MAX_RETRIES`, `LLM_TIMEOUT_SECONDS`.
Full table in `docs/13.ai-mode.md`.

**How to run AI Mode:** AI Mode needs **no RabbitMQ/worker** (runs in-process).
```bash
SCRAPEDO_TOKEN=... GEMINI_API_KEY=...   # or AI_MODE_LLM_PROVIDER=openai + OPENAI_API_KEY=...
python app.py        # open /ui → "AI Mode" tab → upload a CSV
```

**Verified (offline, mocked scrape.do + LLM):** both address & company modes run end-to-end →
`status=completed`, correct `ai_mode_result/<run_id>/` layout, per-batch + summary timing fields,
bad-input rejection, path allowlist. `app.py` AST-parses; routes + globals wired. (Full `import app`
needs the project venv: `aio_pika`, `fastapi`, `boto3`, `pydantic`, `httpx`.)

---

## 4. Per-flow timing for the SerpWow modes (this session)

`process_upload_job` (the worker) now stamps `result["context"]["timing"]["total_seconds"]` =
total per-row processing time, for **all 5** upload pipelines. It surfaces in `output.json` and as
a new XLSX column **`output_processing_seconds`** (`build_upload_output_xlsx_bytes`). The synchronous
`/crawl*` endpoints don't get it (they bypass the worker). Docs updated: `docs/03` §5 (`timing` key)
and §7 (column). XLSX header/value columns verified aligned (38 == 38).

> If you want a finer breakdown (SerpWow vs Gemini time) like AI Mode has, you'd instrument inside
> `execute_company_lookup` — more invasive; not done.

---

## 5. Documentation index (`docs/`)

`README.md` (index) · `01` architecture · `02` config (used vs legacy env) · `03` data models ·
`04` app.py reference · `05` pipelines & flow · `06` API · `07` UI · `08` modules & scripts ·
`09` how to run · `10` data files · `11` handoff (SerpWow) · `12` per-tab LLM map · `13` AI Mode.

---

## 6. Outstanding decisions & TODOs (IMPORTANT)

### 6a. scrapeDo file reduction — DECISION PENDING (user asked to confirm first)
The vendored `scrapedo_finder/` has 20 `.py` files, but `ai_mode_service.py` only uses 12. The
import graph was verified: **these 8 files are imported by nothing the integration uses and are
safe to delete** (the package still imports and AI Mode still works):

`__main__.py`, `cli.py`, `cleanup_cli.py`, `runner.py`, `cleanup_runner.py`,
`company_cleanup_runner.py`, `reporting.py`, `csv_loader.py`

They are scrapeDo's standalone-CLI / high-level-orchestration layer, which the glue replaced.
**Trade-off of deleting:** you lose the ability to run scrapeDo's `scrapedo-finder` /
`scrapedo-clean` CLIs from the vendored copy (the original sibling repo still has them).
→ **Awaiting user's go-ahead before deleting.**

**Files KEPT / used by AI Mode** (do NOT remove): `__init__.py`, `models.py`, `settings.py`,
`scrapedo_client.py`, `llm_client.py`, `prompting.py`, `company_prompting.py`, `extraction.py`,
`company_extraction.py`, `cleanup_reporting.py`, `company_reporting.py`, `company_csv_loader.py`,
+ `prompts/` (`search_query_template.txt`, `company_search_template.txt`).

### 6b. TODO: `scrapedo_finder/README.md` (per-file usage) — NOT YET WRITTEN
User requested a README documenting each file's purpose in the scrapedo_finder folder. Use the
"used vs removable" split in 6a. (This handoff captures the classification; the dedicated README
is still owed.)

### 6c. v2 (not built): parallelize AI-mode batches
The single seam is the per-batch `for` loop in `ai_mode_service.run_ai_mode_sync`. Swap for bounded
`asyncio.gather` over `asyncio.to_thread` batch jobs (or push batches to RabbitMQ). v1 is sequential
by design.

### 6d. Minor: `prepare_ai_mode_run` validates the LLM config at upload time
So uploading AI Mode without an LLM key → HTTP 400 with scrapeDo-worded message
(`Required: LLM_API_KEY, ...`). `SCRAPEDO_TOKEN` is validated at run time instead (missing →
run `status=failed`). Acceptable for v1; reword if desired.

---

## 7. Carry-over caveats from the SerpWow service (still true)

- **Batch-pending row marking is not pipeline-scoped** (`app.py` `process_upload_job`): with
  `ENABLE_GEMINI_BATCH_POSTPROCESS=true`, no-URL rows in *any* pipeline are marked `completed`
  /pending, but the batch only runs for `full`. Enable the flag for `full` only. (`docs/02`, `docs/05` §4.)
- **Two batch-recovery scripts are broken**: `scripts/push_processed_rows_to_gemini_batch.py` (only
  with `--force-new-job`) and `scripts/requeue_wait_and_push_remaining_to_gemini_batch.py` (main
  path) reference `app.upload_lock`, which doesn't exist → `AttributeError`. Fix:
  `app.get_upload_lock(upload_id)`. (`docs/08`.)
- **No browser/proxy crawling** in the SerpWow service; `massive_proxy_cost_usd` is always `0.0`;
  several `.env.example` keys (`MASSIVE_*`, `REDIS_*`, `BROWSER_POOL_SIZE`, …) are read by nothing.
  (`docs/02` §B.)
- `analyze_with_gemini` (app.py) is dead code; `templates/ui copy.html` is an unused backup.

---

## 8. Fast orientation for a new agent

- **Understand the SerpWow service:** read `docs/README.md` → `docs/01` → `docs/04`/`docs/05`.
- **Work on AI Mode:** read `docs/13.ai-mode.md` + `ai_mode_service.py` (self-contained) +
  `scrapedo_finder/` (vendored; clients in `scrapedo_client.py` / `llm_client.py`).
- **Run/verify offline:** mock `ai_mode_service.ScrapeDoClient` and `ai_mode_service.make_llm_client`,
  then call `prepare_ai_mode_run` + `run_ai_mode_sync` (no network/keys needed). `python3 -c
  "import ast; ast.parse(open('app.py').read())"` checks app.py without the heavy deps.
- **Don't** wire AI Mode into RabbitMQ/state.json — it's intentionally standalone.
- **Before deleting any scrapeDo file**, re-check the import graph and get user confirmation (§6a).
