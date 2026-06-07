# Scrape.do AI Mode Integration Plan

## Summary
Add Scrape.do as a new v1 upload mode inside `website_url_finder`, separate from the current SerpWow/RabbitMQ pipelines. The user uploads the existing app CSV format, the API runs one sequential Scrape.do job in 10-row request batches, cleans each Scrape.do response with Gemini or OpenAI, saves all raw and LLM artifacts under `website_url_finder/ai_mode_result/<upload_id>/`, and exposes final results through the normal status/output flow.

## Key Changes
- Add a new pipeline/tab: `scrapedo_ai_mode`, exposed as `POST /uploads/scrapedo-ai-mode`.
- Accept the existing `website_url_finder` CSV columns and map them to Scrape.do inputs:
  `company_name -> entity_name`, `country -> country`, `full_address/input_full_address -> address`, `firm_id -> firm_id`.
- Run v1 jobs as an API background task, not RabbitMQ:
  one upload creates one task, groups rows in batches of `10`, and processes groups sequentially.
- Vendor/reuse the existing Scrape.do client, prompt rendering, LLM clients, and cleanup parsing inside `website_url_finder` so deployment does not depend on the sibling `../scrapeDo` folder.
- Store artifacts in:
  `website_url_finder/ai_mode_result/<upload_id>/`
  with `report.json`, `final_report.json`, `found.csv`, `notFound.csv`, `run.log`, `raw_scrapedo_response/request_NNN.json`, and `llm_cleanup/request_NNN.json`.
- Add env config to `.env.example`:
  `SCRAPEDO_TOKEN`, `SCRAPEDO_BATCH_SIZE=10`, Scrape.do timeout/retry/query-size fields, and namespaced LLM settings:
  `SCRAPEDO_LLM_PROVIDER=gemini|openai`, `SCRAPEDO_LLM_API_KEY`, `SCRAPEDO_LLM_MODEL`, `SCRAPEDO_LLM_BASE_URL`, retries, timeout.
  Gemini falls back to existing `GEMINI_API_KEY/GEMINI_MODEL`; OpenAI falls back to `OPENAI_API_KEY` if present.
- Update `/uploads/{id}/status` and `/uploads/{id}/output` behavior through existing state/output payloads:
  row is `completed` only when an official website is found; no website or scrape/LLM failure is `failed`.
- Add per-row context fields for final output and XLSX:
  `scrapedo_request_index`, `scrapedo_response_time_seconds`, `llm_cleanup_time_seconds`, `ai_mode_total_time_seconds`, `raw_response_path`, `llm_response_path`, `llm_provider`, `llm_model`, and token usage.
- Add a UI tab for Scrape.do AI Mode:
  upload CSV, poll status, show processed/success/failed counts, show result directory, and enable JSON/XLSX downloads.

## Implementation Notes
- Do not change the current `full`, `url_discovery`, `firmographics`, `gmaps`, or `gsearch` behavior.
- Do not require RabbitMQ for Scrape.do uploads.
- Keep raw JSON out of normal `/output`; output stores paths plus timings only.
- Add `ai_mode_result/` to `.gitignore`.
- V1 is sequential by design. Structure the group runner so v2 can add controlled parallelism by processing multiple groups concurrently.

## Test Plan
- Unit test CSV mapping from existing app CSV headers into Scrape.do entity inputs.
- Unit test env loading for Gemini and OpenAI provider selection/fallbacks.
- Integration test with fake Scrape.do client and fake LLM client:
  creates upload, processes 10-row groups sequentially, writes all artifact files, updates state rows, and produces `found.csv`/`notFound.csv`.
- Verify no-RabbitMQ behavior: Scrape.do upload works when `rabbitmq_exchange` is `None`.
- Verify `/uploads/{id}/output?format=json` and XLSX include timing/path fields.
- UI smoke test: upload button calls `/uploads/scrapedo-ai-mode`, polling stops at terminal status, downloads unlock.

## Assumptions
- V1 uses only the address-style Scrape.do flow, mapped from the existing app CSV format.
- `ai_mode_result` lives under the project folder as requested.
- No website found is treated as a failed row to match current `website_url_finder` semantics.
- Raw Scrape.do and LLM payloads are saved to files and referenced by path, not embedded inline in final output.
