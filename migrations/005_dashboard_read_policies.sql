-- Dashboard read access, so a browser dashboard can read Sentinel's data without the database password.
--
-- Who can read: only signed in users whose app_metadata.role is 'dashboard_viewer'. app_metadata can
-- be set only server side (Supabase dashboard or the admin API), never by the user, so an open sign up
-- does not let a stranger grant themselves access. A signed in user without the claim sees zero rows.
-- Access is select only. Nothing here lets the browser insert, update or delete.
--
-- Grant the role to a viewer (server side, example):
--   update auth.users set raw_app_meta_data = raw_app_meta_data || '{"role": "dashboard_viewer"}' where email = '...';

-- Table level read access. agents is column level so token_hash (the agent's credential hash) is never exposed.
grant select on accounts, sellers, payment_attempts, approvals, journal_entries, journal_lines to authenticated;
grant select (id, name, per_payment_cap, weekly_budget, expense_account_id) on agents to authenticated;

-- The (select auth.jwt()) wrapper makes Postgres evaluate the claim once per query instead of once per row.
create policy dashboard_read on accounts for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
create policy dashboard_read on agents for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
create policy dashboard_read on sellers for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
create policy dashboard_read on payment_attempts for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
create policy dashboard_read on approvals for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
create policy dashboard_read on journal_entries for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
create policy dashboard_read on journal_lines for select to authenticated
    using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'dashboard_viewer');
