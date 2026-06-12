# website_url_finder

Two website-discovery systems behind one FastAPI app:

1. **AI Mode** (`ai_bulk` / `ai_deep`) — scrape.do Google AI Mode + LLM cleanup. Standalone
   (no RabbitMQ/S3 needed). Most recent work lives here.
2. **SerpWow pipelines** (legacy) — RabbitMQ + worker + S3 (`full`, `url_discovery`,
   `firmographics`, `gmaps`, `gsearch`).

Start with **`HANDOFF.md`** (the "REWORK COMPLETE" section at the top) for the full picture.

## Quick start

```sh
cd website_url_finder
cp .env.example .env       # fill in SCRAPEDO_TOKEN, GEMINI_API_KEY, SUPABASE_* at minimum
cd backend
../.venv/bin/python -m app.main
```

Open the UI at `http://localhost:8080/app` (port from `.env` `API_PORT`; the legacy `/ui`
path redirects here). Code lives under `backend/app/` (`core/`, `routers/`, `services/`,
`models/`, `prompts/`, `static/`, `templates/`).

Sample input CSVs are in `samples/` (e.g. `samples/sample_address.csv`). The unified input
format requires a company-name column + a country column; see HANDOFF.md for accepted aliases.
Run outputs land in `ai_mode_results/<company-slug>/<run_id>/`.

## Tests

All offline (no live API calls):

```sh
cd website_url_finder/backend
../.venv/bin/python -m unittest discover -s tests
```

## Supabase (company/run tracking)

Apply `supabase/migrations/001_init.sql` to your project, then set in `.env`:

- `SUPABASE_URL` — the bare project URL (`https://xyz.supabase.co`), **not** the postgres
  `:5432` connection string
- `SUPABASE_SERVICE_ROLE_KEY`

Upload endpoints return 503 until these are configured. Tracking failures never fail runs.

## RabbitMQ via Docker (SerpWow pipelines only)

AI Mode does **not** need RabbitMQ. For the legacy SerpWow pipelines, run RabbitMQ from
Docker while the app and worker run on your machine.

Start RabbitMQ:

```sh
cd website_url_finder
docker compose up -d rabbitmq
```

Check it is healthy:

```sh
docker compose ps
```

The app connects to `127.0.0.1:5672`, matching the existing `.env` values:

```env
RABBITMQ_HOST=127.0.0.1
RABBITMQ_PORT=5672
RABBITMQ_USER=<your-rabbitmq-user>
RABBITMQ_PASS=<your-rabbitmq-password>
RABBITMQ_VHOST=/
```

The RabbitMQ management UI is available at `http://localhost:15672`. Log in with the same
`RABBITMQ_USER` and `RABBITMQ_PASS` from `.env`.

Run the app and worker in separate terminals:

```sh
cd website_url_finder/backend
../.venv/bin/python -m app.main
```

```sh
cd website_url_finder/backend
../.venv/bin/python -m app.services.serpwow.worker
```

Stop RabbitMQ:

```sh
cd website_url_finder
docker compose down
```

If you change RabbitMQ credentials after the volume has already been created, reset the
RabbitMQ data volume:

```sh
cd website_url_finder
docker compose down -v
docker compose up -d rabbitmq
```
