-- No approval flow: the guard blocks any seller the owner has not approved, so nothing waits for a human.
--
-- Older runs left payment_attempts rows in 'pending'. Those rows still count as in flight spend and hold the
-- dedupe lock (one_inflight_per_key), so they are closed out as blocked. payment_attempts is not part of
-- the append only ledger, so this update is allowed. The 'pending' and 'denied' check values and the approvals
-- table stay in the schema, unused, so this migration only changes rows.

update payment_attempts set status = 'blocked' where status = 'pending';
