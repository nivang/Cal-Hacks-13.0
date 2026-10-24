-- What the catalog advertised for each URL the agent was shown while working on a task (find_services). The intent
-- rules compare the seller's actual 402 against it: a higher price or a different wallet is a bait and switch, a
-- different description means the paywall sells something else than the listing, and a URL the agent says came from
-- the catalog but is not here was made up or fed to it. One row per task and URL; a later search refreshes it.
-- Seller written text, kept server side: RLS on with no policies and no grants, as in 004.

create table listings_seen (
    mandate_id bigint not null references mandates (id),
    url text not null,
    listed_amount numeric(18,6),
    listed_pay_to text,
    name text,
    description text,
    seen_at timestamptz not null default now(),
    primary key (mandate_id, url)
);

alter table listings_seen enable row level security;
revoke all on listings_seen from anon, authenticated;
