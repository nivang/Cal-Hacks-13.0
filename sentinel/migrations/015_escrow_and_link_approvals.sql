-- Escrow (014_escrow) and link approvals (010_link_approvals, 014_link_approval_intent_statuses) were built on
-- separate branches and each rebuilt the payment status check and the in flight lock. Databases ran them in different
-- orders, so this sets the final state for both features: every status either uses, and one live attempt per key
-- while it is waiting for approval, approved, or held in escrow.

alter table payment_attempts drop constraint payment_attempts_status_check;
alter table payment_attempts add constraint payment_attempts_status_check
    check (status in ('blocked', 'pending', 'authorized', 'approved', 'denied', 'cancelled', 'expired', 'settled',
                      'failed', 'escalated', 'escrowed', 'voided'));
drop index one_inflight_per_key;
create unique index one_inflight_per_key on payment_attempts (dedupe_key)
    where status in ('pending', 'authorized', 'approved', 'escrowed');
