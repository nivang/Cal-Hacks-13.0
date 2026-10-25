-- Texting the agent (texting.py). A user links one phone (or iMessage email handle) by texting a code the account
-- page shows; from then on texts from that handle act for that user and nobody else. Codes are stored as HMACs
-- (TOKEN_HASH_SECRET, purpose "phone-link"), live 10 minutes and are spent once. "Sign out everywhere" deletes the
-- link. Server side only: RLS on with no policies and no grants, as in 004.

create table phone_links (
    handle text primary key,  -- normalized: +<digits> for a phone, lowercase for an email handle
    user_id uuid not null unique references users(id) on delete cascade,
    linked_at timestamptz not null default now()
);

create table phone_link_codes (
    code_hash text primary key,
    user_id uuid not null references users(id) on delete cascade,
    expires_at timestamptz not null,
    used_at timestamptz
);

create index phone_link_codes_user on phone_link_codes (user_id);

alter table phone_links enable row level security;
alter table phone_link_codes enable row level security;
revoke all on phone_links from anon, authenticated;
revoke all on phone_link_codes from anon, authenticated;
