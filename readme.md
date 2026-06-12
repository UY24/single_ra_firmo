# website_url_finder

Company-website discovery behind one FastAPI app: cheap **SerpWow pipelines** (gmaps /
gsearch / full / url_discovery / firmographics) plus **AI Mode** (`ai_bulk` / `ai_deep`,
scrape.do Google AI Mode + LLM cleanup). Company/run tracking lives in Supabase; the UI is
served at `/app`.

## Prerequisites

- Python 3.12 (Homebrew python is fine: `brew install python@3.12`)
- A Supabase project (free tier works) for company/run tracking
- Docker — **only** if you use the SerpWow pipelines (they need RabbitMQ; AI Mode does not)

## Setup

1. Clone and enter the repo:

   ```sh
   git clone <repo-url> && cd website_url_finder
   ```

2. Create the venv if missing:

   ```sh
   python3.12 -m venv .venv
   ```

3. Install dependencies:

   ```sh
   .venv/bin/pip install -r backend/requirements.txt
   ```

4. Create your `.env` and fill it in:

   ```sh
   cp .env.example .env
   ```

   Fill the **"1. REQUIRED — core"** section (`SCRAPEDO_TOKEN`, `GEMINI_API_KEY` — or an
   OpenAI key via the AI Mode section — plus `API_PORT` if you want a non-default port)
   and the **"2. Supabase"** section (next step). Everything else has working defaults.

5. Supabase: create a project, open the dashboard **SQL Editor**, run the contents of
   `supabase/migrations/001_init.sql`, then paste into `.env`:

   - `SUPABASE_URL` — the **bare** project URL (`https://<ref>.supabase.co`), **not** the
     postgres `:5432` connection string
   - `SUPABASE_SERVICE_ROLE_KEY` — the full **service_role** secret (~200+ chars) from
     Project Settings → API

   Upload endpoints return 503 until both are set.

## Run

```sh
.venv/bin/python run.py    # from the repo root (`python run.py` if the venv is active)
```

or equivalently:

```sh
cd backend && ../.venv/bin/python -m app.main
```

Open `http://localhost:8080/app` (port = `API_PORT` in `.env`).

**RabbitMQ (SerpWow pipelines only):** AI Mode runs standalone. To use the SerpWow
pipelines, start RabbitMQ via the bundled compose file (it provides a
`rabbitmq:3.13-management` container; user/pass default to `guest`/`guest` unless set in
`.env`):

```sh
docker compose up -d rabbitmq
```

then run the worker in a second terminal:

```sh
cd backend && ../.venv/bin/python -m app.services.serpwow.worker
```

Management UI: `http://localhost:15672`.

## Tests

```sh
cd backend && ../.venv/bin/python -m unittest discover -s tests
```

All offline — no live API calls.

## Typical workflow

Run the cheap SerpWow pipelines first (`gmaps` / `gsearch` / `full`) — they resolve ~80%
of companies. Feed the unresolved-residue CSV into AI Mode: `ai_bulk` for broad batches,
`ai_deep` for thorough per-entity investigation. Uploads show a **preview** step (column
mapping + row count) before the run starts, and finished runs have a **"Re-run failed
rows"** button that carries resolved rows over.

## More docs

- `HANDOFF.md` — architecture and current state (start at the top)
- `docs/` — SerpWow pipeline internals, configuration reference, API endpoints
