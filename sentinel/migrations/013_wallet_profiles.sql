-- What the Base Sepolia explorer said about a seller wallet (chain.wallet_profile), cached for an hour so a payment
-- does not wait on the explorer every time: activity counts, when it was first seen, and whether it is flagged as a
-- scam. The payment rules block a flagged payee and hold one with no activity at all. Public chain data, kept server
-- side like everything else: RLS on with no policies and no grants, as in 004.

create table wallet_profiles (
    address text primary key,  -- lower case
    profile jsonb not null,
    checked_at timestamptz not null default now()
);

alter table wallet_profiles enable row level security;
revoke all on wallet_profiles from anon, authenticated;
