-- Reconcile statuses regardless of whether link approvals or intent binding was installed first.
alter table payment_attempts drop constraint payment_attempts_status_check;
alter table payment_attempts add constraint payment_attempts_status_check
    check (status in ('blocked', 'pending', 'authorized', 'approved', 'denied', 'cancelled',
                      'expired', 'settled', 'failed', 'escalated'));
