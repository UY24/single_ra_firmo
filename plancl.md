# Plan: Plug scrape.do "AI Mode" into website_url_finder (v1)

## Context

`website_url_finder` finds official company URLs via **SerpWow + Gemini** (queue/worker pipeline,
docs in `website_url_finder/docs/`). The sibling project `/Users/ujjwalyadav/coding/forage/scrapeDo`
(`scrapedo_finder` package) does the same job a different way: it sends a batch of companies to
**scrape.do's Google "AI Mode"** (`GET https://api.scrape.do/plugin/google/search/ai-mode`, prompt
in the `q` param), saves the raw responses, then runs an **LLM cleanup** pass (already
provider-switchable: OpenAI *or* Gemini) to extract the official website per company.

We want this as a **new "AI Mode" capability inside website_url_finder (v1)**: upload a CSV → run
the scrape.do 2-phase pipeline → save **all raw scrape.do responses + all LLM output + timings**
under a new `ai_mode_result/` folder. v1 is sequential (batches of **10 rows** per scrape.do
request); **v2 will parallelize batches** (out of scope here, but designed for). The LLM provider
must be env-switchable (Gemini or OpenAI), reusing the existing `GEMINI_API_KEY` and adding
`OPENAI_*`. Per batch we must record **scrape.do response time** and **LLM cleaning time** and show
them in the final result.

## Decisions (confirmed with user)

1. **Standalone runner + new UI tab** — a `POST /uploads/ai-mode` endpoint runs the pipeline as an
   in-process background task and writes to `ai_mode_result/<run_id>/`. **Not** wired into
   RabbitMQ/state.json/S3 (those stay SerpWow-only).
2. **Auto-detect input mode** — detect "company" vs "address" from CSV headers and run the matching
   scrapeDo pipeline; map website_url_finder's existing columns into scrapeDo entities.
3. **Reuse `GEMINI_API_KEY` + add `OPENAI_*`** — a small adapter maps website_url_finder env →
   scrapeDo's `LLMConfig`, then calls scrapeDo's existing `make_llm_client`.
4. **Vendor** the `scrapedo_finder` package into website_url_finder (copy it in) — the two are
   separate git repos and website_url_finder has no requirements file; cross-dir imports are
   fragile. No new heavy deps (`httpx` already used; `python-dotenv` already used by `gmaps.py`).

## Architecture / flow (v1)

```
UI "AI Mode" tab → POST /uploads/ai-mode (CSV)
  → save CSV + create run_id (uuid) under ai_mode_result/<run_id>/
  → asyncio.create_task(asyncio.to_thread(run_ai_mode_sync, run_id, csv_path))   # scrapeDo is sync
  → return {run_id, status_url}

run_ai_mode_sync (the thin orchestrator, reuses scrapeDo building blocks):
  detect input_type from headers → load entities (EntityInput | CompanyEntityInput)
  Phase 1 (per batch of SCRAPEDO_BATCH_SIZE=10):
     query = build_search_query | build_company_search_query
     t0; payload = ScrapeDoClient.search_google_ai_mode(query); scrapedo_seconds = elapsed
     write raw → ai_mode_result/<run_id>/raw_scrapedo_response/request_NNN.json
  Phase 2 (per batch):
     messages = build_messages | build_company_messages(entities, text_blocks, references)
     t0; parsed, usage = make_llm_client(cfg).complete_json(messages); llm_seconds = elapsed
     results = parse_results | parse_company_results(parsed, entities)
  write ai_mode_result/<run_id>/{report.json, final_report.json, found.csv, notFound.csv, run.log,
        status.json}   ← final_report includes per-batch {scrapedo_seconds, llm_seconds} + totals
  update status (in-memory registry + status.json)
```

UI polls `GET /uploads/ai-mode/<run_id>/status`; downloads via
`GET /uploads/ai-mode/<run_id>/result?file=final_report.json|found.csv|...`.

## Changes (file by file)

### A. Vendor the package — `website_url_finder/scrapedo_finder/`
Copy these modules verbatim from `../scrapeDo/scrapedo_finder/` (relative imports keep working as a
package): `scrapedo_client.py`, `csv_loader.py`, `company_csv_loader.py`, `prompting.py`,
`company_prompting.py`, `extraction.py`, `company_extraction.py`, `cleanup_reporting.py`,
`company_reporting.py`, `reporting.py`, `models.py`, `settings.py`, `llm_client.py`,
`cleanup_runner.py`, `company_cleanup_runner.py`, `__init__.py`. Also copy the prompt templates
`prompts/search_query_template.txt` and `prompts/company_search_template.txt` into
`website_url_finder/scrapedo_finder/prompts/` and point the query builders at that path.
*(Skip the standalone CLIs `cli.py`/`cleanup_cli.py`/`runner.py` — the glue replaces them, though
`runner.py`'s building blocks are reused.)*

Two small edits inside the vendored package:
- **Per-batch LLM timing** in `cleanup_runner.py` and `company_cleanup_runner.py`: wrap
  `self.llm_client.complete_json(messages)` (≈ `cleanup_runner.py:61` / `company_cleanup_runner.py:61`)
  with `time.perf_counter()` and record `llm_seconds` onto each request; add a `requests[]` block to
  the report dict (via `cleanup_reporting.build_report_dict` / `company_reporting`) carrying
  `{request_index, entity_count, llm_seconds}`. (scrape.do per-request time already exists in
  `report.json.requests[].time_taken_seconds`.)
- Ensure `prompts` path resolves to the vendored templates (the builders read template files
  relative to the package).

### B. New glue module — `website_url_finder/ai_mode_service.py`
- `build_ai_mode_settings()` → scrapeDo `Settings` from env (`SCRAPEDO_TOKEN`,
  `SCRAPEDO_BATCH_SIZE` default **10**, timeouts/retries/locale opts). Reuses
  `scrapedo_finder.settings.Settings`.
- `build_ai_mode_llm_config()` → scrapeDo `LLMConfig` from website_url_finder env:
  `AI_MODE_LLM_PROVIDER` (`gemini`|`openai`, default `gemini`); gemini → `GEMINI_API_KEY` +
  `GEMINI_MODEL` (existing) + scrapeDo's gemini base URL; openai → `OPENAI_API_KEY` +
  `OPENAI_MODEL` + `OPENAI_BASE_URL` (default `https://api.openai.com/v1`). Then
  `make_llm_client(cfg)`.
- `detect_input_type_and_load(csv_path)` → if headers contain `Company Name ENG`/`Country Code`
  → company mode (reuse `load_company_entities`); else address mode using a flexible mapper
  (`entity_name`←`entity_name|company_name|company|name`, `country`←`country|country code|nation`,
  `address`←`address|input_full_address|full_address|fulladdress`, optional `firm_id`) producing
  scrapeDo `EntityInput`.
- `run_ai_mode_sync(run_id, csv_path)` → the orchestrator above, reusing `ScrapeDoClient`,
  `chunked`, the query builders, `build_messages`/`build_company_messages`,
  `parse_results`/`parse_company_results`, `make_llm_client`, and the report writers; writes the
  `ai_mode_result/<run_id>/` layout (mirrors scrapeDo's `reports/batch_*` files) + a `status.json`.
- In-memory `ai_mode_runs: dict[str, dict]` status registry + helpers to list/read runs from
  `ai_mode_result/` on disk (so status survives restarts).

### C. `website_url_finder/app.py` endpoints (new, near the other `/uploads/*`)
- `POST /uploads/ai-mode` — accept CSV `UploadFile`, validate `.csv`, save under the run dir,
  create `run_id`, launch the background task, return
  `{run_id, total_rows, input_type, llm_provider, status_url, result_url}`.
- `GET /uploads/ai-mode` — list runs (from `ai_mode_result/` + registry).
- `GET /uploads/ai-mode/{run_id}/status` — `{run_id, status, input_type, llm_provider, llm_model,
  total_rows, batch_size, batches_total, batches_done, scrapedo_seconds_total, llm_seconds_total,
  created_at, updated_at, error}`.
- `GET /uploads/ai-mode/{run_id}/result?file=...` — stream a file from the run dir
  (final_report.json / found.csv / notFound.csv / report.json), with download headers.
Reuse existing helpers: `_now_iso`, `_get_bool_env/_get_int_env`, `_safe_name`, the `UploadFile`
pattern from `_create_upload_with_rows`.

### D. UI — `website_url_finder/templates/ui.html`
Add a 7th tab **"AI Mode"** mirroring the existing tab pattern (tab button + view + JS):
upload control, a provider/model indicator, and a runs table (run_id, status, input mode, rows,
batches done/total, **scrape.do time**, **LLM time**, total time, download links). Poll
`/uploads/ai-mode/{run_id}/status` every 2 s like the other tabs; reuse `escapeHtml`, `shortTs`,
chip helpers.

### E. Config & ignores
- `.env.example`: add `SCRAPEDO_TOKEN=`, `SCRAPEDO_BATCH_SIZE=10`, `SCRAPEDO_TIMEOUT_SECONDS=90`,
  `SCRAPEDO_MAX_RETRIES=2`, `SCRAPEDO_MAX_QUERY_CHARS=6000`, optional `SCRAPEDO_HL/GL/DEVICE/...`;
  `AI_MODE_LLM_PROVIDER=gemini`, `OPENAI_API_KEY=`, `OPENAI_MODEL=gpt-4o-mini`, `OPENAI_BASE_URL=`.
  (`GEMINI_API_KEY`/`GEMINI_MODEL` already present.)
- `.gitignore`: add `ai_mode_result/`.

### F. Docs — `website_url_finder/docs/13.ai-mode.md` (+ link in `docs/README.md`)
Document the new mode: flow, env vars, input auto-detect, the `ai_mode_result/` layout, the
per-batch timing fields, provider switch, and the v1/v2 boundary.

## Timing design (the key feature)

`final_report.json.requests[]` (one per batch of 10) records:
`{request_index, entity_count, scrapedo_seconds, llm_seconds, combined_seconds}` and
`summary` carries `{scrapedo_seconds_total, llm_seconds_total, batch_duration_seconds,
token_usage}`. scrape.do time comes from Phase-1 `time.perf_counter()` (already in scrapeDo's
runner pattern); LLM time is the new per-request instrumentation added in §A. The UI surfaces both
totals per run.

## v1 scope vs v2

- **v1 (this plan):** sequential batches of 10; one scrape.do request per batch; LLM cleanup per
  batch; everything saved under `ai_mode_result/`; provider switch; per-batch timing; UI tab.
- **v2 (NOT now):** parallelize batches (bounded `asyncio.gather` over `asyncio.to_thread` batch
  jobs, or push batches onto the existing RabbitMQ worker). The orchestrator's per-batch loop is
  the single seam that changes.

## Verification

1. **Offline (no API cost):** unit-test `detect_input_type_and_load` (company vs address headers,
   column mapping) and `build_ai_mode_llm_config` (gemini vs openai → correct `LLMConfig`/client
   type). Reuse scrapeDo's existing `transport=` injection seam on the clients
   (`llm_client.py`/`scrapedo_client.py`) to mock scrape.do + LLM HTTP in an integration test that
   runs `run_ai_mode_sync` end-to-end against fakes and asserts the `ai_mode_result/<run_id>/`
   files + per-batch timing fields exist.
2. **Live smoke (needs `SCRAPEDO_TOKEN` + `GEMINI_API_KEY`):** `python app.py`, open `/ui` → "AI
   Mode" tab, upload `smalltest.csv` (auto-detected as address mode), watch status → `completed`,
   then confirm `ai_mode_result/<run_id>/` has `raw_scrapedo_response/request_*.json`,
   `final_report.json` (with `scrapedo_seconds`/`llm_seconds` per batch), `found.csv`/`notFound.csv`.
3. **Provider switch:** set `AI_MODE_LLM_PROVIDER=openai` + `OPENAI_API_KEY`/`OPENAI_MODEL`, re-run,
   confirm `final_report.json.llm.provider == "openai"` and results populate.
4. Confirm the existing SerpWow pipelines/UI/tests are untouched (no edits to `execute_*`,
   RabbitMQ, or state.json paths).
