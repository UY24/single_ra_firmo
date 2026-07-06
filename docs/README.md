# `website_url_finder` — Documentation Index

This folder documents the **Single RA ISI** service (a.k.a. *Company URL Discovery* / `single_ra_firmo`):
a FastAPI + RabbitMQ batch system that, given a list of companies, finds each company's
**official website URL** and writes a **corporate business description** (in the company's
country language and in English) plus firmographic fields (address, phone, email, industry,
products, services).

It is the engineering implementation of the brief in
`AI descriptions requirements.pdf` (see [01.overview-and-architecture.md](01.overview-and-architecture.md)).

> **Scope note for reviewers (codex):** These docs describe the code *as it exists in the
> repository today*. Where the code contains dead/legacy elements (e.g. an unused function,
> `.env` keys that nothing reads, a backup HTML file), the docs say so explicitly rather than
> pretending the feature is wired up. Every claim was checked against the source; file/line
> references use the form `app.py:1234`.

## Read in this order

| # | Doc | What it covers |
|---|-----|----------------|
| 00 | **README.md** (this file) | Index, repository map, glossary |
| 01 | [01.overview-and-architecture.md](01.overview-and-architecture.md) | Business goal, the 5 pipelines, components, deployment topology, end-to-end data flow |
| 02 | [02.configuration.md](02.configuration.md) | Every environment variable — which ones the code actually reads, defaults, and which are legacy/unused |
| 03 | [03.data-models.md](03.data-models.md) | Pydantic models, the `state.json` / `output.json` schemas, the row & `context` objects, CSV input formats, XLSX columns, S3 layout |
| 04 | [04.app.py-reference.md](04.app.py-reference.md) | Function-by-function reference for `app.py` (the 7,470-line core), grouped by section |
| 05 | [05.pipelines-and-flow.md](05.pipelines-and-flow.md) | Deep dive into the URL-discovery algorithm (transliteration → query phases → SerpWow → GMaps → Gemini) and the Gemini batch lifecycle |
| 06 | [06.api-endpoints.md](06.api-endpoints.md) | Every HTTP endpoint: method, params, response shape |
| 07 | [07.ui.md](07.ui.md) | `templates/ui.html` — the 6 tabs, JavaScript functions, polling |
| 08 | [08.supporting-modules-and-scripts.md](08.supporting-modules-and-scripts.md) | `worker.py`, `batch_runner.py`, `gmaps.py`, `codetails.py`, `convertjtox.py`, `scripts/*` |
| 09 | [09.how-to-run.md](09.how-to-run.md) | Dependencies, env setup, running the API + worker, the helper scripts, end-to-end walkthrough |
| 10 | [10.data-files-and-artifacts.md](10.data-files-and-artifacts.md) | The CSV test inputs, `input.json`, `output.xlsx`, the loose `*.xlsx` exports |
| 11 | [11.handoff.md](11.handoff.md) | **Engineering handoff** — one-page brain-dump, the Gemini LLM call map, caveats, next steps |
| 12 | [12.tab-llm-flow.md](12.tab-llm-flow.md) | Per-UI-tab flowchart: pipeline + exactly where the Gemini LLM is used |
| 13 | [13.ai-mode.md](13.ai-mode.md) | AI Mode — scrape.do Google AI Mode + Gemini/OpenAI cleanup, saved to ai_mode_result/ |

## Repository map

```
website_url_finder/
├── app.py                     # 7,470 lines — the entire service: FastAPI app, URL-discovery
│                              #   pipeline, Gemini helpers, SerpWow client, S3/state persistence,
│                              #   RabbitMQ producer + worker, Gemini batch post-processing, XLSX writer
├── worker.py                  # Standalone worker entrypoint (consumes RabbitMQ jobs)
├── batch_runner.py            # CLI: run Gemini batch post-processing for one upload_id
├── gmaps.py                   # SerpWow Google-Maps (places + place_details) client
├── codetails.py               # SerpWow AI-overview firmographics lookup for a domain (+CLI)
├── convertjtox.py             # One-off: flatten input.json → output.xlsx via pandas
├── templates/
│   ├── ui.html                # Active single-page UI (dark theme, 6 tabs) served at GET /ui
│   └── ui copy.html           # Legacy/backup light-theme UI — NOT served by the app
├── scripts/
│   ├── debug_s3.py            # Ad-hoc: fetch a state.json straight from S3
│   ├── debug_state.py         # Ad-hoc: print an upload's state summary via app helpers
│   ├── push_processed_rows_to_gemini_batch.py        # Re-run batch for processed rows
│   └── requeue_wait_and_push_remaining_to_gemini_batch.py  # Requeue → wait → partial batch
├── .env.example               # Template of all env vars (superset; some keys are legacy)
├── .gitignore
├── AI descriptions requirements.pdf   # The product brief
├── input.json                 # Sample combined /output JSON (124 Polish-etc. companies)
├── output.xlsx                # convertjtox.py output built from input.json
├── *.csv                      # Test input lists (smalltest, mediumtest, poc_1k_sample, …)
└── *.xlsx                     # Loose per-upload XLSX exports (named by upload_id)
```

There is **no** `requirements.txt`, `pyproject.toml`, `Dockerfile`, or `config.yaml` in the
repo. Dependencies are inferred from imports — see [09.how-to-run.md](09.how-to-run.md).

## What the service does, in one paragraph

A user uploads a CSV of companies (name + country, optionally address/industry/firm_id) through
the web UI or `POST /uploads`. The API parses the CSV, writes an upload `state.json` (locally in
`/tmp/single_ra_isi/<upload_id>/` and to S3), and publishes one **RabbitMQ job per row**. A
separate **worker process** consumes those jobs and, for each company, runs the URL-discovery
pipeline: it transliterates the inputs to Latin script, parses city/state, builds a battery of
Google search "dork" queries across phases, fires them at **SerpWow** (a Google-results API),
optionally enriches via **Google Maps** (also through SerpWow), and uses **Gemini** to pick the
most plausible official URL and to extract firmographics. Results are written back into
`state.json`. When all rows for a "full" upload finish, an optional **Gemini batch** job
re-processes every row in one large call to produce the long bilingual descriptions. The UI polls
status and offers JSON/XLSX downloads.

## Glossary

| Term | Meaning |
|------|---------|
| **Upload** | One CSV submission, identified by a UUID `upload_id`. Has a `state.json`. |
| **Row / job** | One company line. Each row becomes one RabbitMQ message and one pipeline run. |
| **Pipeline** | The processing mode for an upload: `full`, `url_discovery`, `firmographics`, `gmaps`, or `gsearch`. |
| **SerpWow** | Third-party API (`api.serpwow.com`) that returns Google search results, AI overviews, and Google Maps data. The service's only web-data source. |
| **Gemini** | Google `generativelanguage.googleapis.com` LLM, called per-row (synchronously) and/or as a batch job. Default model `gemini-2.5-flash-lite`. |
| **GMaps** | Google Maps lookups performed *through SerpWow* (`gmaps.py`), used as a URL fallback/enrichment. |
| **Firmographics** | Address, phone, email, industry, products, services for a company. |
| **Gemini batch** | A single large `batchGenerateContent` call that post-processes all rows of a `full` upload to produce final URLs + bilingual descriptions. |
| **State** | `state.json` — the authoritative per-upload record (rows, statuses, results, batch metadata). |
| **Artifact** | A file written per upload to local disk and/or S3 (`state.json`, `output.json`, the batch JSONL/JSON, per-row SerpWow raw JSON). |
