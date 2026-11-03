-- 061_products_finish_type.sql
--
-- Runbook 6gh. The Bowler Depot theme showed Motiv Nebula ("5500 Grit
-- LSP") as "Finish Type: Satin". Two problems:
--
--   1. LSP is polished. Al: "LS is laser scan S=sanded P=polished". The
--      classifier didn't know the short form, so the 58 LSP balls fell
--      through to the grit rule (fixed in classify_factory_finish).
--   2. Satin was never a manufacturer's word. Al: "where did you get
--      satin? can we just do sanded or polished. if need 3 it needs to be
--      two variants of dull/sanded". Migration 035 invented the bucket
--      (fine sanding at 3000+ grit reads as a sheen); manufacturers say
--      sanded or polished, plus a grit.
--
-- So two new columns carry the answer Al asked for:
--   finish_type -- 'sanded' or 'polished' (035's dull and satin are both
--                  sanded). NULL = nothing to classify.
--   finish_grit -- the finest grit named in factory_finish (5500 for
--                  "5500 Grit LSP", 2000 for "500/1000/2000 Siaair Micro
--                  Pad"). It's the coarse/fine detail, kept as the number
--                  rather than as a third name.
--
-- finish_category loses 'satin' for every brand, not just Motiv (Al: "can
-- we update all to not have satin too not just these"): satin rows become
-- 'dull' (Al: "dull/sanded"), and the column is checked to dull/polished.
-- It stays, rather than being renamed, because the partner API's v1
-- contract names it (emitting fewer of its values is allowed; a new value
-- like 'sanded' would not be), and the plotter estimator reads it -- with
-- finish_grit, so a 3000+ grit sanded ball keeps the old satin level and
-- positions don't move. The partner API gains finish_type and finish_grit
-- as optional fields (allowed in v1).
--
-- Both are partner API fields, so they join the columns 057's
-- products_bump_content_changed_at watches: a ball whose finish_type is
-- filled in shows up in /v1/changes, even when its finish_category didn't
-- change (an already-polished ball). Otherwise the function is 057's.
--
-- No backfill here, same split as 035: POST /admin/backfill-finish-
-- categories (scripts/backfill_finish_categories.py) fills all three
-- columns and rewrites finish_category where the LSP fix changes it.

begin;

alter table products add column if not exists finish_type text
    check (finish_type in ('sanded', 'polished'));
alter table products add column if not exists finish_grit integer
    check (finish_grit > 0);

update products set finish_category = 'dull' where finish_category = 'satin';
alter table products drop constraint if exists products_finish_category_check;
alter table products add constraint products_finish_category_check
    check (finish_category in ('dull', 'polished'));

create or replace function products_bump_content_changed_at() returns trigger
language plpgsql as $$
begin
  if (new.name, new.url, new.color, new.brand_id, new.core_id, new.coverstock_id,
      new.coverstock_material, new.coverstock_type, new.coverstock_name, new.has_particle,
      new.factory_finish, new.finish_category, new.release_date, new.status, new.published,
      new.product_type, new.primary_image_url, new.oil_rating, new.motion_rating,
      new.oil_motion_source, new.fragrance, new.finish_type, new.finish_grit)
     is distinct from
     (old.name, old.url, old.color, old.brand_id, old.core_id, old.coverstock_id,
      old.coverstock_material, old.coverstock_type, old.coverstock_name, old.has_particle,
      old.factory_finish, old.finish_category, old.release_date, old.status, old.published,
      old.product_type, old.primary_image_url, old.oil_rating, old.motion_rating,
      old.oil_motion_source, old.fragrance, old.finish_type, old.finish_grit)
  then
    new.content_changed_at := now();
  end if;
  return new;
end $$;

comment on column products.finish_type is 'Factory finish as the manufacturer states it: sanded or polished (061_products_finish_type.sql, runbook 6gh). Derived from factory_finish by admin_api.classify_finish_type -- polished when the text names a polish/compound/shine/buff step or LSP (laser scan polished), else sanded when a grit is given. NULL = nothing to classify. Overridable through the review queue like finish_category.';
comment on column products.finish_grit is 'The finest (highest) grit number named in factory_finish (061, runbook 6gh), e.g. 5500 for "5500 Grit LSP". NULL when the text names no grit.';

commit;
