-- Default deny. RLS is on for every table and there are no policies, so the anon and authenticated
-- roles (the Supabase API) can read and write nothing. Sentinel's server connects with the database
-- owner credentials from DATABASE_URL, which are never shipped to a browser.
-- Grants are revoked as well, as a second layer.

alter table accounts enable row level security;
alter table agents enable row level security;
alter table sellers enable row level security;
alter table payment_attempts enable row level security;
alter table approvals enable row level security;
alter table journal_entries enable row level security;
alter table journal_lines enable row level security;

revoke all on accounts, agents, sellers, payment_attempts, approvals, journal_entries, journal_lines
    from anon, authenticated;
