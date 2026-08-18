# website_url_finder

Company-website discovery behind one FastAPI app: cheap **SerpWow pipelines** (gmaps /
gsearch / firmographics / relationship) plus **AI Mode** (`ai_bulk` / `ai_deep`,
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
     postgres `:5432/postgres` connection string
   - `SUPABASE_SERVICE_ROLE_KEY` — the full **service_role** secret (~200+ chars) from
     Project Settings → API

   Upload endpoints return 503 until both are set.

   Quick sanity check without printing secrets:

   ```sh
   awk -F= '/^SUPABASE_URL=/{print $2}' .env
   ```

   The value must look like `https://abcxyz.supabase.co` and must not contain `:5432` or
   `/postgres`.

## Run locally

You run two things in **separate terminals**: the **app** (always), and the **SerpWow
worker** (only if you use the SerpWow pipelines — AI Mode needs no worker).

**Terminal 1 — the app (dev, auto-reload):**

```sh
.venv/bin/ uvicorn run:app --reload --host 127.0.0.1 --port 8080
```

Then open `http://localhost:8080/app` (port = `API_PORT` in `.env`). The server hot-reloads
on code changes. `run:app` is intentional — uvicorn needs the `module:asgi_app` import
string (`run.py` exposes `app`); plain `uvicorn run --reload` won't work.

No-reload alternatives (same app):

```sh
.venv/bin/python run.py                          # from repo root, reads API_* from .env
cd backend && ../.venv/bin/python -m app.main    # or run the module directly
```

**Terminal 2 — the SerpWow worker (SerpWow pipelines only):** AI Mode runs entirely
in-process and needs neither RabbitMQ nor a worker. For the SerpWow pipelines
(firmographics / gmaps / gsearch / relationship), first start RabbitMQ (the bundled
compose file provides a `rabbitmq:3.13-management` container; user/pass default to
`guest`/`guest` unless set in `.env`):

```sh
docker compose up -d rabbitmq
```

then start the worker in the second terminal:

```sh
.venv/bin/python worker.py        # from repo root (resolves into backend/app)
```

RabbitMQ management UI: `http://localhost:15672`. Uploads sit `queued` until the worker is
running.

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

- `CLAUDE.md` — architecture (durable)
- `HANDOFF.md` — current state: what's done, what's owed, what's blocking 500k
- `docs/` — SerpWow pipeline internals, configuration reference, API endpoints
