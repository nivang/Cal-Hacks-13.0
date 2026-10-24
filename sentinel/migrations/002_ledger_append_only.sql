-- The books and approvals are append only. Corrections are reversing entries, never edits.
-- Fires for every role, including the table owner.

create function reject_ledger_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception '% is append only', tg_table_name using errcode = 'integrity_constraint_violation';
end;
$$;

create trigger journal_entries_append_only before update or delete on journal_entries
    for each row execute function reject_ledger_change();
create trigger journal_lines_append_only before update or delete on journal_lines
    for each row execute function reject_ledger_change();
create trigger approvals_append_only before update or delete on approvals
    for each row execute function reject_ledger_change();

create trigger journal_entries_no_truncate before truncate on journal_entries
    for each statement execute function reject_ledger_change();
create trigger journal_lines_no_truncate before truncate on journal_lines
    for each statement execute function reject_ledger_change();
create trigger approvals_no_truncate before truncate on approvals
    for each statement execute function reject_ledger_change();
