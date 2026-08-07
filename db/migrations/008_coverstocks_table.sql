-- 008_coverstocks_table.sql
--
-- Al's ask, directly parallel to migration 007's cores table: "can we do
-- the same thing we did for cores for covers, those are also shared
-- across many balls" -- confirmed the shared field would be
-- coverstock_name (products' existing free-text marketing name column,
-- e.g. MOTIV's "Atomic Propulsion Pearl Reactive", Brunswick's "HK22 -
-- Savvy Hook Hybrid"). Same real shape as cores: one named coverstock
-- formulation, scoped to a brand, reused across many differently-named
-- products, invisible as a many-to-one relationship while it only lives
-- as a repeated free-text column on every product row.
--
-- One real difference from migration 007, worth calling out: ball_families
-- (renamed to cores) existed in the schema from day one but was NEVER
-- wired into any scraper's upsert logic, so family_id was null on every
-- products row -- migration 007 could only backfill core_id via a live
-- rescrape of every product page (scripts/backfill_core_ids.py), because
-- there was no core data sitting in `products` to derive it from.
-- coverstock_name/coverstock_material/coverstock_type, by contrast, have
-- been real, populated columns on every products row since
-- 001_init_schema.sql, written by every one of the five scrapers
-- (product_scraper, commercebuild_product_scraper,
-- woocommerce_product_scraper, netsuite_product_scraper,
-- shopify_product_scraper) on every scrape. That means coverstock_id can
-- be fully backfilled for every already-scraped product right here, in
-- this migration, from data that already exists -- no rescrape, no
-- backfill script, no admin_api endpoint needed for the one-time catch-up
-- the way cores required.
--
-- products.coverstock_name/coverstock_material/coverstock_type are left
-- in place, unchanged, and still written by every scraper on every
-- scrape -- they're real per-product data (this specific product's own
-- observed coverstock), not just a foreign key's shadow, and other code
-- already depends on them directly (bowlerdepot_reconciliation's field
-- mapping, pdf_parser, bowwwl_cross_check). coverstock_id is additive: a
-- normalized join for "which other products share this exact coverstock"
-- (get_or_create_coverstock_id, wired into each scraper's upsert_product
-- next), the same relationship cores already gives for core_name.

begin;

create table coverstocks (
    id uuid primary key default uuid_generate_v4(),
    brand_id uuid not null references brands(id),
    name text not null,                        -- e.g. "Atomic Propulsion Pearl Reactive"
    material coverstock_material,
    type coverstock_type,
    created_at timestamptz not null default now(),
    unique (brand_id, name)
);

alter table products add column coverstock_id uuid references coverstocks(id);
create index idx_products_coverstock_id on products(coverstock_id);

comment on table coverstocks is 'One row per named coverstock formulation, scoped to a brand -- multiple differently-named products can share one (see migration 007''s cores table for the identical shape/reasoning, just for core rather than coverstock).';
comment on column products.coverstock_id is 'FK into coverstocks, backfilled below from this row''s own pre-existing coverstock_name/coverstock_material/coverstock_type (unlike core_id, which needed a live rescrape -- see this migration''s header comment). Kept in sync going forward by each scraper''s get_or_create_coverstock_id, same pattern as core_id.';

-- One-time backfill: every distinct (brand_id, coverstock_name) pair
-- already sitting in products becomes a coverstocks row, then every
-- product with a matching name gets pointed at it. material/type are
-- carried along from whichever product row's data happens to be picked
-- (DISTINCT ON, ordered by updated_at desc so the most-recently-scraped
-- values win over stale ones) -- immaterial in practice, since every
-- scraper derives material/type from the same coverstock_name via its own
-- deterministic parse_coverstock() keyword matching, so two products
-- sharing a coverstock_name should already agree on material/type.
insert into coverstocks (brand_id, name, material, type)
select distinct on (brand_id, coverstock_name)
    brand_id, coverstock_name, coverstock_material, coverstock_type
from products
where coverstock_name is not null
order by brand_id, coverstock_name, updated_at desc
on conflict (brand_id, name) do nothing;

update products p
set coverstock_id = cs.id
from coverstocks cs
where cs.brand_id = p.brand_id
  and cs.name = p.coverstock_name
  and p.coverstock_id is null;

commit;
