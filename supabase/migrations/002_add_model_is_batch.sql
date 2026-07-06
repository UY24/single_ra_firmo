-- Add model name and batch-mode flag to the runs table.
-- Apply in the Supabase dashboard SQL Editor.
alter table runs add column if not exists model text;
alter table runs add column if not exists is_batch boolean;
