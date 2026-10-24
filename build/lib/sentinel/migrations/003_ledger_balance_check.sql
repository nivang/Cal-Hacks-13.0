-- Every journal entry must balance at commit: debits equal credits and are above zero.
-- Deferred, so an entry and its lines can be inserted in any order inside one transaction.
-- The function builds the table name from the firing trigger's schema so this file also works in "test".

create function check_entry_balanced() returns trigger
language plpgsql
set search_path = ''
as $$
declare
    target_entry bigint;
    total_debit numeric;
    total_credit numeric;
begin
    if tg_table_name = 'journal_entries' then
        target_entry := new.id;
    else
        target_entry := new.entry_id;
    end if;

    execute format(
        'select coalesce(sum(debit), 0), coalesce(sum(credit), 0) from %I.journal_lines where entry_id = $1',
        tg_table_schema
    ) into total_debit, total_credit using target_entry;

    if total_debit <> total_credit or total_debit = 0 then
        raise exception 'journal entry % is unbalanced: debits % credits %', target_entry, total_debit, total_credit
            using errcode = 'check_violation';
    end if;
    return null;
end;
$$;

create constraint trigger journal_entries_balanced after insert on journal_entries
    deferrable initially deferred for each row execute function check_entry_balanced();
create constraint trigger journal_lines_balanced after insert on journal_lines
    deferrable initially deferred for each row execute function check_entry_balanced();
