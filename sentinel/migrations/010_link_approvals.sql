-- One purchase approval, separate from permanent seller trust.
alter table payment_attempts add column approval_expires_at timestamptz;
alter table payment_attempts drop constraint payment_attempts_status_check;
alter table payment_attempts add constraint payment_attempts_status_check
    check (status in ('blocked', 'pending', 'authorized', 'approved', 'denied', 'cancelled', 'expired', 'settled',
                      'failed', 'escalated', 'escrowed', 'voided'));
drop index one_inflight_per_key;
create unique index one_inflight_per_key on payment_attempts (dedupe_key)
    where status in ('pending', 'authorized', 'approved', 'escrowed');
create index payment_approval_expiry on payment_attempts (agent_id, approval_expires_at)
    where status in ('pending', 'authorized');
