-- Migration 044: products.content_changed_at -- change tracking for the
-- Partner API's sync feed (DEPLOY_RUNBOOK.md 6cj).
--
-- Al: "we are also working on a separate platform that i want to be a
-- consumer of the ball data in the project ... it could sync so that
-- isn't as noisy." The partner platform polls GET /v1/changes and should
-- only see a ball when something it can actually see changed.
--
-- Why not products.updated_at: every scraper sets updated_at = now() on
-- every upsert (it means "last touched"), and the admin/consumer "recently
-- updated" sorts rely on that. Columns like popularity_score, demand_score,
-- last_video_discovery_at and last_price_discovery_at also change daily
-- without anything partner-visible changing. A trigger on updated_at would
-- either change its meaning or make every ball "changed" every day.
--
-- So: a dedicated column, bumped ONLY when partner-visible content changes:
--   * products: the columns the v1 Ball model exposes (below)
--   * product_skus: insert/delete, or weight/rg/differential/mass_bias
--   * product_images: insert/delete, or url/visibility/thumbnail/order
--   * product_articles: insert/delete, or status/slug (the article link)
-- No-op upserts (a scraper re-writing identical values) don't bump it.
-- Brand/core/coverstock NAME edits don't bump it either (rare, admin-only;
-- see the runbook).

begin;

alter table products add column content_changed_at timestamptz not null default now();

create index products_content_changed_at_idx on products (content_changed_at, id);

-- products: bump when an exposed column changes. Otherwise leave
-- NEW.content_changed_at as given, so the child-table triggers below can
-- set it explicitly.
create or replace function products_bump_content_changed_at() returns trigger
language plpgsql as $$
begin
  if (new.name, new.url, new.color, new.brand_id, new.core_id, new.coverstock_id,
      new.coverstock_material, new.coverstock_type, new.coverstock_name, new.has_particle,
      new.factory_finish, new.finish_category, new.release_date, new.status, new.published,
      new.product_type, new.primary_image_url, new.oil_rating, new.motion_rating,
      new.oil_motion_source)
     is distinct from
     (old.name, old.url, old.color, old.brand_id, old.core_id, old.coverstock_id,
      old.coverstock_material, old.coverstock_type, old.coverstock_name, old.has_particle,
      old.factory_finish, old.finish_category, old.release_date, old.status, old.published,
      old.product_type, old.primary_image_url, old.oil_rating, old.motion_rating,
      old.oil_motion_source)
  then
    new.content_changed_at := now();
  end if;
  return new;
end $$;

create trigger products_content_changed_at
  before update on products
  for each row execute function products_bump_content_changed_at();

-- Child tables: bump the parent product.
create or replace function bump_parent_product_content_changed_at() returns trigger
language plpgsql as $$
declare
  pid uuid;
begin
  if tg_op = 'DELETE' then
    pid := old.product_id;
  else
    pid := new.product_id;
  end if;
  if pid is not null then
    update products set content_changed_at = now() where id = pid;
  end if;
  -- An UPDATE that moved a row to another product touches both.
  if tg_op = 'UPDATE' and old.product_id is distinct from new.product_id and old.product_id is not null then
    update products set content_changed_at = now() where id = old.product_id;
  end if;
  return null;
end $$;

-- Insert/delete always count. Updates count only when a watched value
-- really changed: an "UPDATE OF col" trigger alone also fires for no-op
-- writes (a scraper's ON CONFLICT DO UPDATE re-setting the same value),
-- so each update trigger carries a WHEN guard. ON CONFLICT DO UPDATE runs
-- the UPDATE triggers, not AFTER INSERT, so a re-scrape of an existing SKU
-- or image only bumps when its values differ.
create trigger product_skus_content_changed_at_ins_del
  after insert or delete on product_skus
  for each row execute function bump_parent_product_content_changed_at();
create trigger product_skus_content_changed_at_upd
  after update on product_skus
  for each row
  when ((old.product_id, old.weight_lbs, old.rg, old.differential, old.mass_bias)
        is distinct from (new.product_id, new.weight_lbs, new.rg, new.differential, new.mass_bias))
  execute function bump_parent_product_content_changed_at();

create trigger product_images_content_changed_at_ins_del
  after insert or delete on product_images
  for each row execute function bump_parent_product_content_changed_at();
create trigger product_images_content_changed_at_upd
  after update on product_images
  for each row
  when ((old.product_id, old.stored_url, old.source_url, old.is_visible, old.is_thumbnail, old.display_order)
        is distinct from (new.product_id, new.stored_url, new.source_url, new.is_visible, new.is_thumbnail, new.display_order))
  execute function bump_parent_product_content_changed_at();

create trigger product_articles_content_changed_at_ins_del
  after insert or delete on product_articles
  for each row execute function bump_parent_product_content_changed_at();
create trigger product_articles_content_changed_at_upd
  after update on product_articles
  for each row
  when ((old.product_id, old.status, old.slug) is distinct from (new.product_id, new.status, new.slug))
  execute function bump_parent_product_content_changed_at();

comment on column products.content_changed_at is
  'When partner-visible content last changed (exposed product columns, SKU specs, images, article link). Drives the Partner API /v1/changes sync feed (migration 044). Not the same as updated_at, which every scrape touches.';

commit;
