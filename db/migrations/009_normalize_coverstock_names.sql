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
-- Two parts:
--
-- 1. Normalize every coverstocks.name in place: strip TM (™), R
--    (®), C (©) symbols and collapse whitespace. This mirrors
--    _normalize_coverstock_name() in every scraper (see that function's
--    docstring) -- added there in the same pass as this migration, so
--    future scrapes resolve straight to the normalized name and never
--    recreate the duplicate. This migration is the one-time catch-up for
--    whatever migration 008's backfill (or any scrape before this fix)
--    already created from the raw, un-normalized text.
--
-- 2. Normalizing can make two previously-distinct rows collide onto the
--    same (brand_id, name) -- that's the whole point, it's exactly the
--    duplicate this migration exists to fix. Merge those: pick the
--    lowest id per (brand_id, name) group as the survivor, repoint every
--    products.coverstock_id from a merged-away row to the survivor, carry
--    over material/type onto the survivor if it's missing either (a
--    products row that referenced the merged-away row may have a real
--    value the survivor never got), then delete the merged-away rows.
--
-- Deliberately NOT touching products.coverstock_name/coverstock_material/
-- coverstock_type anywhere in this migration -- those stay exactly the
-- raw, as-scraped text/values they've always been (see migration 008's
-- own comment on why). Only the coverstocks table's canonical name is
-- normalized; coverstock_id is what gets repointed.

begin;

-- Step 1: normalize in place.
update coverstocks
set name = trim(regexp_replace(regexp_replace(name, '[™®©]', '', 'g'), '\s+', ' ', 'g'))
where name <> trim(regexp_replace(regexp_replace(name, '[™®©]', '', 'g'), '\s+', ' ', 'g'));

-- Step 2a: repoint products.coverstock_id off any row that's about to be
-- merged away, onto the (brand_id, name) group's survivor.
with grouped as (
    select id, brand_id, name,
           min(id) over (partition by brand_id, name) as survivor_id
    from coverstocks
)
update products p
set coverstock_id = g.survivor_id
from grouped g
where p.coverstock_id = g.id
  and g.id <> g.survivor_id;

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

-- Step 2c: delete the now-unreferenced merged-away rows.
with grouped as (
    select id, brand_id, name,
           min(id) over (partition by brand_id, name) as survivor_id
    from coverstocks
)
delete from coverstocks c
using grouped g
where c.id = g.id
  and g.id <> g.survivor_id;

commit;
