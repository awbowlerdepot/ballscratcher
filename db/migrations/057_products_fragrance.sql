-- 057_products_fragrance.sql -- the ball's scent, as the manufacturer
-- names it (DEPLOY_RUNBOOK.md 6fk).
--
-- Al: "do we pull in the scent/fragrance for the storm balls?" -- we
-- didn't: commercebuild_product_scraper already parsed Storm's and Roto
-- Grip's "Fragrance:" spec line (e.g. "Root Beer", "Apple Crisp", "Melon
-- Mint") into its label dict but never stored it. Then: "it should show
-- everywhere that we list specs. the partner API is the one i was
-- targeting to have it" and "if a ball doesn't have it set just hide the
-- entire key value pair in any UI. for the API just return an empty
-- field".
--
-- Nullable free text, null = the manufacturer doesn't list one (every
-- non-commercebuild brand today, and some Storm/Roto Grip pages).
--
-- It's a ball attribute the Partner API exposes, so it joins the columns
-- products_bump_content_changed_at (044) watches: a ball whose fragrance
-- is filled in (or changes) shows up in /v1/changes. Otherwise the
-- function is 044's, unchanged.

alter table products add column if not exists fragrance text;

create or replace function products_bump_content_changed_at() returns trigger
language plpgsql as $$
begin
  if (new.name, new.url, new.color, new.brand_id, new.core_id, new.coverstock_id,
      new.coverstock_material, new.coverstock_type, new.coverstock_name, new.has_particle,
      new.factory_finish, new.finish_category, new.release_date, new.status, new.published,
      new.product_type, new.primary_image_url, new.oil_rating, new.motion_rating,
      new.oil_motion_source, new.fragrance)
     is distinct from
     (old.name, old.url, old.color, old.brand_id, old.core_id, old.coverstock_id,
      old.coverstock_material, old.coverstock_type, old.coverstock_name, old.has_particle,
      old.factory_finish, old.finish_category, old.release_date, old.status, old.published,
      old.product_type, old.primary_image_url, old.oil_rating, old.motion_rating,
      old.oil_motion_source, old.fragrance)
  then
    new.content_changed_at := now();
  end if;
  return new;
end $$;
