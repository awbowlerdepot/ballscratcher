-- 009_normalize_coverstock_names.sql
--
-- Follow-up to migration 008. Al noticed real duplicates in the
-- coverstocks table: manufacturer pages add a trademark/registered/
-- copyright symbol to a coverstock name sometimes but not always for the
-- exact same coverstock (e.g. "R2S Solid Reactive" on one product's page,
-- "R2S(TM) Solid Reactive" on another) -- same intent, different text, so
-- 008's exact-text unique constraint on (brand_id, name) let both spellings
-- in as separate rows.
--
-- Safe to run whether or not any duplicates actually exist yet -- every
-- step below is a no-op on rows that are already clean. In particular:
-- safe to run even if 008 hasn't been applied yet on this database (the
-- table will just be empty), and safe to run more than once.
--
-- REAL INCIDENT #1, found on first run against the actual database: the
-- original version of this migration normalized coverstocks.name in
-- place FIRST, then merged/deleted whatever collided as a result. That
-- failed immediately -- `duplicate key value violates unique constraint
-- "coverstocks_brand_id_name_key" ... Key (brand_id, name)=(...,
-- Activator Plus) already exists.` -- because Postgres checks a plain
-- (non-deferred) unique constraint per row as it's written, not once at
-- the end of the statement. A single UPDATE that tries to rename both
-- "Activator Plus(TM)" and the already-existing "Activator Plus" onto the
-- same text hits the constraint the moment the second row is written,
-- long before any merge/delete step ever runs.
--
-- Fixed by reordering: identify duplicate groups and merge/delete them
-- FIRST, using each row's still-distinct original name (deleting a row
-- never conflicts with a unique constraint, no matter what its name is)
-- -- only THEN, once every (brand_id, normalized-name) group has exactly
-- one row left, rename that survivor to its normalized form. At that
-- point the rename can never collide with anything, because the group
-- it belongs to no longer has any other row.
--
-- REAL INCIDENT #2, found on the very next run: `function min(uuid) does
-- not exist`. `coverstocks.id` is uuid, and Postgres's built-in MIN/MAX
-- aggregates are only defined for a specific set of types (numeric,
-- string, date/time, a few others) -- uuid isn't one of them, even
-- though uuid fully supports ordering/comparison (it has a normal btree
-- operator class, so `<`, `>`, `order by` all work fine on it). Fixed by
-- picking the survivor via `first_value(id) over (... order by id)`
-- instead of `min(id) over (...)` -- first_value only needs an ORDER BY
-- (comparison), not the MIN aggregate specifically, so it works on any
-- orderable type including uuid.
--
-- Three parts:
--
-- 1. Group existing rows by (brand_id, normalized name) -- normalizing
--    means stripping TM (™), R (®), C (©) symbols and collapsing
--    whitespace, mirroring _normalize_coverstock_name() in every scraper
--    (see that function's docstring, added in the same pass as this
--    migration so future scrapes resolve straight to the normalized name
--    and never recreate the duplicate). Pick the lowest id per group as
--    the survivor -- purely a deterministic tiebreaker, no other
--    significance.
--
-- 2. Repoint every products.coverstock_id off a merged-away row onto its
--    group's survivor, backfill material/type onto the survivor from any
--    referencing product if the survivor is missing either (a product
--    that referenced the merged-away row may have a real value the
--    survivor never got), then delete the merged-away rows -- all while
--    every row's `name` is still untouched, so nothing here can ever hit
--    the unique constraint.
--
-- 3. Only now, with each (brand_id, normalized-name) group down to a
--    single row, rename that row to its normalized form.
--
-- Deliberately NOT touching products.coverstock_name/coverstock_material/
-- coverstock_type anywhere in this migration -- those stay exactly the
-- raw, as-scraped text/values they've always been (see migration 008's
-- own comment on why). Only the coverstocks table's canonical name is
-- normalized; coverstock_id is what gets repointed.

begin;

-- Step 1/2a: repoint products.coverstock_id off any row that's about to
-- be merged away, onto its (brand_id, normalized name) group's survivor.
-- Grouping is computed fresh here (not persisted) since coverstocks.name
-- hasn't been touched yet at this point.
with grouped as (
    select id, brand_id,
           trim(regexp_replace(regexp_replace(name, '[™®©]', '', 'g'), '\s+', ' ', 'g')) as norm_name
    from coverstocks
),
survivors as (
    select id, brand_id, norm_name,
           first_value(id) over (partition by brand_id, norm_name order by id) as survivor_id
    from grouped
)
update products p
set coverstock_id = s.survivor_id
from survivors s
where p.coverstock_id = s.id
  and s.id <> s.survivor_id;

-- Step 2b: backfill material/type onto each survivor from any product
-- still pointing at it (post-repoint) with a real value, in case the
-- survivor's own material/type is null but a merged-away duplicate's
-- wasn't -- same coalesce-never-overwrite spirit as
-- get_or_create_coverstock_id, just applied catalog-wide instead of
-- one row at a time.
with coverstock_specs as (
    select distinct on (coverstock_id) coverstock_id, coverstock_material, coverstock_type
    from products
    where coverstock_id is not null
      and (coverstock_material is not null or coverstock_type is not null)
    order by coverstock_id, updated_at desc
)
update coverstocks c
set material = coalesce(c.material, s.coverstock_material),
    type = coalesce(c.type, s.coverstock_type)
from coverstock_specs s
where c.id = s.coverstock_id
  and (c.material is null or c.type is null);

-- Step 2c: delete the merged-away rows. Still operating on each row's
-- original, un-normalized name here -- deleting a row can never violate
-- a unique constraint, so this is safe regardless of what any name looks
-- like.
with grouped as (
    select id, brand_id,
           trim(regexp_replace(regexp_replace(name, '[™®©]', '', 'g'), '\s+', ' ', 'g')) as norm_name
    from coverstocks
),
survivors as (
    select id,
           first_value(id) over (partition by brand_id, norm_name order by id) as survivor_id
    from grouped
)
delete from coverstocks c
using survivors s
where c.id = s.id
  and s.id <> s.survivor_id;

-- Step 3: NOW normalize the remaining (already duplicate-free) rows'
-- names in place. Every (brand_id, normalized-name) group has exactly
-- one row left at this point, by construction -- this rename can never
-- collide with another row's name.
update coverstocks
set name = trim(regexp_replace(regexp_replace(name, '[™®©]', '', 'g'), '\s+', ' ', 'g'))
where name <> trim(regexp_replace(regexp_replace(name, '[™®©]', '', 'g'), '\s+', ' ', 'g'));

commit;
