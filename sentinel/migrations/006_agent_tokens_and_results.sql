-- Agent bearer tokens and the stored body of every paid response.
--
-- Tokens: agents.token_hash holds sha256(token). Unique, so one token maps to exactly one agent.
-- Results: attempt_results keeps what we received for a settled payment, so payment_status can hand the data
-- back to the agent. Write once, like the journal: it is evidence of what was delivered (response_hash on the
-- attempt proves it). Not covered by the dashboard read policies, so viewers cannot read paid bodies.

create unique index agents_token_hash_key on agents (token_hash) where token_hash is not null;

create table attempt_results (
    attempt_id bigint primary key references payment_attempts (id),
    body text not null,
    content_type text,
    stored_at timestamptz not null default now()
);

create trigger attempt_results_append_only before update or delete on attempt_results
    for each row execute function reject_ledger_change();
create trigger attempt_results_no_truncate before truncate on attempt_results
    for each statement execute function reject_ledger_change();

alter table attempt_results enable row level security;
revoke all on attempt_results from anon, authenticated;
