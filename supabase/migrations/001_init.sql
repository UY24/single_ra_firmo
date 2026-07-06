create table companies (
  id uuid primary key default gen_random_uuid(),
  name text unique not null,
  created_at timestamptz not null default now()
);

create table runs (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id),
  pipeline text not null,        -- gmaps | gsearch | full | firmographics | url_discovery | ai_bulk | ai_deep
  run_ref text not null,         -- on-disk run_id / upload_id
  status text not null,          -- queued | running | completed | completed_with_errors | failed
  total_rows int,
  success_count int, failed_count int,
  websites_found int, websites_not_found int,
  token_usage jsonb,             -- {prompt_tokens, completion_tokens, total_tokens}
  cost jsonb,                    -- {llm_usd, scrapedo_usd, total_usd, scrapedo_requests}
  duration_seconds numeric,
  file_links jsonb,              -- {"found.csv": "<local path or S3 url>", ...}
  rerun_of uuid references runs(id),
  error text,
  started_at timestamptz, finished_at timestamptz,
  created_at timestamptz not null default now()
);
create index runs_company_idx on runs(company_id);

-- Row Level Security: enabled with NO policies on purpose.
-- Only the FastAPI backend talks to these tables, using the service_role key,
-- which bypasses RLS. Enabling RLS without policies means the anon/authenticated
-- keys get zero access — exactly what we want for an internal tool.
alter table companies enable row level security;
alter table runs enable row level security;
