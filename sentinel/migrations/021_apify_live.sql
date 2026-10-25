-- Apify credit runs on the Live page, with the same checks a payment gets (sentinel/apify_runs.py).
-- A run Sentinel refuses is now stored as status BLOCKED (actual_charge 0, so it spends nothing) with the rule and
-- detail that stopped it, the way a blocked payment attempt is. actor_title and actor_description are the Actor's
-- own Store text at purchase time: what the delivery check holds the results against. The intent tier and reason, the injection model's
-- score and the delivery check's verdict use the payment_attempts column names, so the Live page reads both alike.
-- Dedupe ignores BLOCKED rows: a refused try never stops a later allowed one with the same input.

alter table apify_runs
    add column if not exists url text,
    add column if not exists actor_title text,
    add column if not exists actor_description text,
    add column if not exists rule text,
    add column if not exists detail text,
    add column if not exists intent_tier smallint,
    add column if not exists intent_reason text,
    add column if not exists ml_model text,
    add column if not exists ml_score double precision,
    add column if not exists ml_threshold double precision,
    add column if not exists delivery_verdict text,
    add column if not exists delivery_reason text;

alter table apify_runs drop constraint if exists apify_runs_agent_id_mandate_id_actor_input_hash_key;
create unique index if not exists apify_runs_one_per_input on apify_runs (agent_id, mandate_id, actor, input_hash)
    where status <> 'BLOCKED';
create index if not exists apify_runs_mandate on apify_runs (mandate_id, created_at);

alter table live_checks drop constraint if exists live_checks_kind_check;
alter table live_checks add constraint live_checks_kind_check check (kind in ('pay', 'search', 'apify'));
