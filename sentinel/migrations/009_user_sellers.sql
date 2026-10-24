-- Seller lists per user: the operator's list plus a personal list for each user, so a signed in user can approve
-- the sellers their own agent may pay from the account page.
--
-- sellers.agent_id: null means the operator's list, which applies to every agent (all the rows from before this
-- file). A value means that agent's personal list: it applies to that agent only, so one user can never open a
-- seller for anybody else. For one agent the applicable rows are agent_id is null or agent_id = that agent; any
-- applicable blocked row wins, then any applicable approved row, else the seller is not listed (attempts.load_history).
-- The old unique (pay_to, network, asset) becomes two partial unique indexes: operator rows stay unique, and each
-- agent lists an address once. Both compare the address and the asset case insensitively, the way the guard looks
-- sellers up, so 0xAbc and 0xabc are one seller. db.seed_sellers conflicts against the operator index.
-- RLS and grants are unchanged (004 and 005): the new column is covered by the table level select that dashboard
-- viewers already have on sellers, and anon and authenticated can still write nothing.

alter table sellers add column agent_id bigint references agents (id);  -- null: the operator's list

alter table sellers drop constraint if exists sellers_pay_to_network_asset_key;

create unique index sellers_operator_key on sellers (lower(pay_to), network, lower(asset)) where agent_id is null;
create unique index sellers_agent_key on sellers (agent_id, lower(pay_to), network, lower(asset))
    where agent_id is not null;
create index sellers_agent_idx on sellers (agent_id);
