-- Escrow: a payment to a seller that offers x402 auth-capture is held onchain until Sentinel has checked what the
-- seller delivered, then captured (the seller is paid) or voided (the buyer gets it back). See sentinel/escrow.py.
--
-- escrow_payments: one row per escrowed attempt. payment_info is the escrow's PaymentInfo struct (salt as a string),
-- info_hash its id onchain. state follows the contract: authorized (held), captured, voided, or reclaimed (the buyer
-- took it back onchain after the capture deadline). verdict is the delivery check's outcome (pass, fail, unsure);
-- held means it waits for the owner on the Tasks page, and decided_by says who settled it.
-- payment_attempts: status escrowed is money held for a delivery check. Like approved it counts as spent and keeps
-- the in flight lock, so one_inflight_per_key now covers it. voided is money that went back: it counts for nothing.
-- delivery_verdict and delivery_reason record the delivery check on any payment, escrowed or exact.
-- journal_entries.step: an escrowed payment books twice (hold, then capture or void), so one_entry_per_attempt becomes
-- one entry per attempt and step. Every entry before this one is a payment.
-- RLS on with no policies and no grants, as in 004.

create table escrow_payments (
    attempt_id bigint primary key references payment_attempts (id),
    payment_info jsonb not null,
    info_hash text not null unique,
    capture_deadline timestamptz not null,
    refund_deadline timestamptz not null,
    authorize_tx text not null,
    capture_tx text,
    void_tx text,
    state text not null default 'authorized' check (state in ('authorized', 'captured', 'voided', 'reclaimed')),
    verdict text check (verdict is null or verdict in ('pass', 'fail', 'unsure')),
    verdict_rule text,
    verdict_reason text,
    held boolean not null default false,
    decided_by text check (decided_by is null or decided_by in ('sentinel', 'owner', 'deadline')),
    created_at timestamptz not null default now(),
    decided_at timestamptz
);
-- The sweeper's work list: what is still held, soonest deadline first.
create index escrow_payments_open_idx on escrow_payments (capture_deadline) where state = 'authorized';

alter table payment_attempts drop constraint payment_attempts_status_check;
alter table payment_attempts add constraint payment_attempts_status_check
    check (status in ('blocked', 'pending', 'authorized', 'approved', 'denied', 'cancelled', 'expired', 'settled',
                      'failed', 'escalated', 'escrowed', 'voided'));
drop index one_inflight_per_key;
create unique index one_inflight_per_key on payment_attempts (dedupe_key)
    where status in ('pending', 'authorized', 'approved', 'escrowed');

alter table payment_attempts
    add column delivery_verdict text check (delivery_verdict is null or delivery_verdict in ('pass', 'fail', 'unsure')),
    add column delivery_reason text;

alter table journal_entries
    add column step text not null default 'payment' check (step in ('payment', 'hold', 'capture', 'void'));
drop index one_entry_per_attempt;
create unique index one_entry_per_attempt on journal_entries (attempt_id, step)
    where attempt_id is not null and reverses_entry_id is null;

alter table escrow_payments enable row level security;
revoke all on escrow_payments from anon, authenticated;
