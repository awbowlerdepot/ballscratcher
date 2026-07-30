-- 001_init_schema.sql
--
-- Initial schema for the bowling ball database, per the architecture review
-- (see /brunswick-scraper-architecture-review.md). Covers brands/manufacturers,
-- the ball_families -> products -> product_skus identity model, image storage,
-- URL discovery tracking, and the cross-source review queue (manufacturer
-- HTML vs PDF, vs bowwwl.com, vs BowlerDepot's BigCommerce catalog).
--
-- Bags/shoes/accessories/apparel are intentionally out of scope here --
-- add category extension tables when you actually onboard those categories
-- rather than guessing their shape now.

begin;

create extension if not exists "uuid-ossp";

-- ---------------------------------------------------------------------
-- Brands / manufacturers
-- ---------------------------------------------------------------------

create type source_platform as enum ('craft_cms', 'shopify', 'commercebuild', 'other');

create table manufacturers (
    id uuid primary key default uuid_generate_v4(),
    name text not null unique,          -- e.g. "Brunswick Bowling & Billiards"
    notes text,
    created_at timestamptz not null default now()
);

create table brands (
    id uuid primary key default uuid_generate_v4(),
    manufacturer_id uuid references manufacturers(id),
    name text not null unique,          -- e.g. "Brunswick", "Radical", "DV8", "Hammer"
    base_url text not null,
    source_platform source_platform not null,
    -- Craft-CMS family (Brunswick/Radical/DV8) and Shopify family
    -- (Hammer/Ebonite/Track/Powerhouse) each reuse one scraper template --
    -- this groups brands that share a template so a URL-discovery/scraper
    -- run can be parameterized by brand rather than duplicated per brand.
    sitemap_url text,
    created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Ball identity: family (optional grouping) -> product (color/cover variant,
-- the real sellable/browsable unit) -> SKU (weight)
-- ---------------------------------------------------------------------

create type core_type as enum ('symmetric', 'asymmetric');
create type coverstock_material as enum ('reactive_resin', 'urethane', 'polyester_plastic');
create type coverstock_type as enum ('solid', 'pearl', 'hybrid');
create type product_status as enum ('current', 'retired');
create type spec_source as enum ('html', 'pdf', 'manual');

create table ball_families (
    id uuid primary key default uuid_generate_v4(),
    brand_id uuid not null references brands(id),
    name text not null,                 -- e.g. "Fury"
    core_name text,
    core_type core_type,
    release_era text,
    created_at timestamptz not null default now(),
    unique (brand_id, name)
);

create table products (
    id uuid primary key default uuid_generate_v4(),
    family_id uuid references ball_families(id),
    brand_id uuid not null references brands(id),
    name text not null,                 -- e.g. "Fury Emerald/Black Hybrid"
    url text not null unique,
    color text,

    -- Coverstock: material x type, kept separate from factory_finish
    -- (the sanding/polish steps) -- see architecture doc for why these
    -- were split out from an earlier single-field design.
    coverstock_material coverstock_material,
    coverstock_type coverstock_type,          -- solid/pearl/hybrid; null for graphic balls (see has_custom_graphic)
    coverstock_name text,                     -- manufacturer's marketing name, e.g. "HK22 - Savvy Hook Hybrid"
    has_particle boolean not null default false,
    has_custom_graphic boolean not null default false,  -- Viz-A-Ball style printed graphics
    factory_finish text,                      -- e.g. "500, 1000 Siaair Micro Pad"

    part_number text,
    weights_available int4range,              -- e.g. '[12,16]' for 16-12 lb
    usbc_approval_date date,
    release_date date,
    description text,

    status product_status not null default 'current',
    source_platform source_platform not null,
    published boolean not null default false, -- gates what the consumer site / BowlerDepot sync can see

    primary_image_url text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index idx_products_brand on products(brand_id);
create index idx_products_family on products(family_id);
create index idx_products_published on products(published) where published;
create index idx_products_coverstock on products(coverstock_material, coverstock_type);

create table product_skus (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid not null references products(id) on delete cascade,
    weight_lbs int not null,
    rg numeric(5,3),
    differential numeric(5,3),
    mass_bias numeric(5,3),               -- null unless asymmetric core
    part_number text,                     -- only if it varies by weight
    source spec_source not null default 'html',
    needs_review boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (product_id, weight_lbs)
);

create index idx_product_skus_product on product_skus(product_id);
create index idx_product_skus_rg on product_skus(rg);
create index idx_product_skus_diff on product_skus(differential);

-- Convention from the architecture review: when a source gives only one
-- RG/DIFF value rather than a full per-weight breakdown, that value is the
-- 15 lb ball. Enforced at the application layer (the scraper should default
-- weight_lbs = 15 for single-value extractions), not as a DB constraint.

create table product_images (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid not null references products(id) on delete cascade,
    image_type text not null,             -- main | core_callout | front | back | other
    weight_lbs_context int,               -- for core_callout images tied to a weight range
    source_url text not null,             -- original manufacturer/CDN URL, kept for provenance
    stored_url text,                      -- your own S3 URL once mirrored + centered
    created_at timestamptz not null default now()
);

create index idx_product_images_product on product_images(product_id);

-- ---------------------------------------------------------------------
-- URL discovery tracking (sitemap diff)
-- ---------------------------------------------------------------------

create table discovered_urls (
    id uuid primary key default uuid_generate_v4(),
    brand_id uuid not null references brands(id),
    url text not null unique,
    status_path product_status,           -- inferred from URL (/current/ vs /retired/)
    sitemap_lastmod timestamptz,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    last_scraped_at timestamptz,
    scrape_status text not null default 'pending',  -- pending | scraped | error
    created_at timestamptz not null default now()
);

create index idx_discovered_urls_brand on discovered_urls(brand_id);
create index idx_discovered_urls_status on discovered_urls(scrape_status);

-- ---------------------------------------------------------------------
-- Review queue: covers all three mismatch sources from the architecture
-- doc (manufacturer HTML vs PDF, vs bowwwl.com cross-check, vs BowlerDepot's
-- BigCommerce catalog) plus the initial full-catalog review pass.
-- ---------------------------------------------------------------------

create type review_status as enum ('pending', 'approved', 'rejected');

create table review_queue (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid references products(id) on delete cascade,
    product_sku_id uuid references product_skus(id) on delete cascade,
    field_name text not null,
    current_value text,
    proposed_value text,
    source text not null,                 -- e.g. 'pdf_extraction', 'bowwwl_cross_check', 'bowlerdepot_reconciliation'
    reason text,
    status review_status not null default 'pending',
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    resolved_by text
);

create index idx_review_queue_status on review_queue(status) where status = 'pending';
create index idx_review_queue_product on review_queue(product_id);

-- ---------------------------------------------------------------------
-- BowlerDepot reconciliation (BigCommerce catalog match)
-- ---------------------------------------------------------------------

create type bowlerdepot_match_status as enum ('matched', 'unmatched', 'ambiguous');

create table bowlerdepot_products (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid references products(id),
    bigcommerce_product_id text not null,
    bigcommerce_sku text,
    match_status bowlerdepot_match_status not null default 'unmatched',
    last_synced_at timestamptz,
    created_at timestamptz not null default now(),
    unique (bigcommerce_product_id, bigcommerce_sku)
);

create index idx_bowlerdepot_products_product on bowlerdepot_products(product_id);

commit;
