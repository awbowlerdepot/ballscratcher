-- Runbook 6eg: Partner API access management in admin.
--
-- Al: "is the access management and keys something we can add to the
-- admin.bowleriq.com" -> "yes build it". Until now partners were managed
-- only with scripts/create_partner_api_key.py, and the one place they
-- lived -- the Secrets Manager secret the partner API authorizer reads,
-- {"keys": {"<partner>": "<sha256 of key>"}} -- has no room for who
-- created a key, when, which key a partner holds, or notes.
--
-- That secret stays the ONLY thing the authorizer reads (unchanged, so
-- drilld's and Virtue Bowling Supply's existing keys keep working with no
-- authorizer change). This table is the admin page's record ALONGSIDE it:
-- one row per partner name ever issued a key, written by admin_api's
-- partner functions in the same request that changes the secret.
-- "Active" is decided by the secret (a name in it), not by this table.
-- Never stores a key or its hash.

begin;

create table if not exists partner_api_partners (
    name         text primary key,       -- same name as the secret's key and the partner API log's partner=
    contact      text,                   -- who to talk to at the partner (free text)
    notes        text,
    key_last4    text,                   -- last 4 characters of the CURRENT key, to tell keys apart; null if unknown
    created_at   timestamptz not null default now(),
    created_by   text,
    rotated_at   timestamptz,
    rotated_by   text,
    revoked_at   timestamptz,            -- set on revoke, cleared if a key is issued again
    revoked_by   text,
    updated_at   timestamptz not null default now(),
    constraint partner_api_partners_name_format check (name ~ '^[A-Za-z0-9_-]{2,64}$')
);

comment on table partner_api_partners is
  'Runbook 6eg: admin record of Partner API partners. The authorizer reads only the Secrets Manager secret bowling-scraper-partner-api-keys; this table holds who/when/notes/last-4 alongside it. Never stores keys or hashes.';

-- The two partners that existed before this table (keys issued with the
-- script). Exact creation times and key last-4s weren't recorded, so
-- those stay approximate/null.
insert into partner_api_partners (name, created_at, created_by, notes)
values
  ('drilld', '2026-10-05', 'script (create_partner_api_key.py)', 'Drill sheet software. Key issued before admin tracking (runbook 6cj).'),
  ('Virtue_Bowling_Supply', '2026-10-21', 'script (create_partner_api_key.py)', 'Key issued before admin tracking (runbook 6ee).')
on conflict (name) do nothing;

commit;
