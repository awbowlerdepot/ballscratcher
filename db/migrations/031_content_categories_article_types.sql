-- 031_content_categories_article_types.sql
--
-- Al, while picking a Learn-site theme: "i think having Categories with
-- one being Bowing balls and Ball review being a type of article. Just
-- to ensure future expansion." A real, queryable two-level taxonomy for
-- Learn content -- category (a content area, e.g. "Bowling Balls," later
-- "Bowling Bags"/"Bowling Shoes"/non-product areas like "News") containing
-- one or more article_types (the kind of write-up within it, e.g. "Ball
-- Review," later "Buying Guide"/"Comparison") -- not just a couple of
-- hardcoded frontend strings.
--
-- Deliberately its OWN lookup tables, not a reuse of products.product_type
-- (migration 019's ball/bag/shoe dispatch column). That column answers a
-- different question -- "what kind of physical item is this catalog row" --
-- and every scraper/pipeline branches on it directly; overloading it as
-- the Learn site's content taxonomy would couple the two together (e.g. a
-- future "News" or "Buying Guide" category has no product_type at all, and
-- a future non-review article type for balls shouldn't require a new
-- product_type value). categories.product_type below is an OPTIONAL loose
-- link back to that column (nullable) purely so today's single category
-- can be derived from/matched against existing ball rows -- it's a hint,
-- not a foreign key, since a category need not correspond to any
-- product_type at all.
--
-- Same structural shape as migration 008's coverstocks table (standalone
-- lookup table, uuid_generate_v4() id, backfillable from data that already
-- exists) rather than migration 019's single check-constraint column,
-- because Al asked for these to be real rows admin_api/public_api can list
-- and join against -- a text CHECK column can't hold a description or be
-- extended without a code deploy the way a table's rows can.
--
-- product_articles.category_id/article_type_id are added nullable, same
-- convention as products.core_id/coverstock_id (migrations 007/008) --
-- backfilled below for the one category/type that exists today, but never
-- forced NOT NULL, consistent with how this schema already treats every
-- other lookup FK.

begin;

create table categories (
    id uuid primary key default uuid_generate_v4(),
    slug text not null unique,                  -- e.g. "bowling-balls"
    name text not null,                         -- e.g. "Bowling Balls"
    description text,
    product_type text,                          -- optional loose hint back to products.product_type (migration 019); NOT a foreign key -- see header comment
    display_order int not null default 0,
    created_at timestamptz not null default now()
);

create table article_types (
    id uuid primary key default uuid_generate_v4(),
    category_id uuid not null references categories(id) on delete cascade,
    slug text not null,                         -- e.g. "ball-review"
    name text not null,                         -- e.g. "Ball Review"
    description text,
    display_order int not null default 0,
    created_at timestamptz not null default now(),
    unique (category_id, slug)
);

alter table product_articles add column category_id uuid references categories(id);
alter table product_articles add column article_type_id uuid references article_types(id);
create index idx_product_articles_category_id on product_articles(category_id);
create index idx_product_articles_article_type_id on product_articles(article_type_id);

comment on table categories is 'Learn-site content areas (migration 031) -- Al: "having Categories with one being Bowling balls... just to ensure future expansion." Decoupled from products.product_type on purpose -- see this migration''s header comment.';
comment on table article_types is 'The kind of write-up within a category (migration 031), e.g. "Ball Review" within "Bowling Balls" -- Al: "Ball review being a type of article." Scoped to one category (category_id not null); a future category with multiple write-up kinds just adds more rows here.';
comment on column categories.product_type is 'Optional, NOT a foreign key -- a loose hint for deriving/backfilling this category from products.product_type (migration 019) where one happens to exist. A category with no corresponding product_type (e.g. a future "News" section) simply leaves this null.';
comment on column product_articles.category_id is 'FK into categories (migration 031), nullable same as core_id/coverstock_id on products -- set at generation time in product_article_generator/app.py, derived from the product''s own product_type.';
comment on column product_articles.article_type_id is 'FK into article_types (migration 031), nullable same as category_id. Every article generated today is the single seeded "Ball Review" type; more types get their own row here, not a new column.';

insert into categories (slug, name, description, product_type, display_order)
values ('bowling-balls', 'Bowling Balls', 'Reviews and coverage of bowling balls.', 'ball', 0);

insert into article_types (category_id, slug, name, description, display_order)
select id, 'ball-review', 'Ball Review', 'A full review generated from a ball''s specs and its approved video coverage.', 0
from categories where slug = 'bowling-balls';

-- Backfill: every product_articles row generated so far is a ball review
-- (product_article_generator has only ever run against product_type='ball'
-- products -- see migration 019 and product_article_generator/app.py's
-- fetch_product_content). Loop-free single UPDATE, safe to re-run.
update product_articles pa
set category_id = c.id,
    article_type_id = at.id
from categories c
join article_types at on at.category_id = c.id and at.slug = 'ball-review'
where c.slug = 'bowling-balls'
  and pa.category_id is null;

commit;
