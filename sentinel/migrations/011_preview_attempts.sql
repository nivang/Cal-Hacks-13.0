-- Services Sentinel stopped while the agent was only searching (find_services, quote) go into the payment log, so
-- the owner sees every service that was turned away, not just the ones paid for. The agent is shown only what it
-- may buy.
--
-- payment_attempts.preview: true for a row written by a preview: blocked, or escalated (held for the owner, who can
-- Allow once from the Tasks page). Nothing was attempted or paid. Null or false is a real pay call. The rate and
-- drift rules (intent.tier0) and the judge's history count real pay calls only. One preview row per task, URL and
-- status, so searching again does not repeat it.

alter table payment_attempts add column preview boolean;
create index payment_attempts_preview_idx on payment_attempts (mandate_id, url, status) where preview;
