-- 022_product_articles.sql
--
-- Al: "Do you think we generate ball review article like the one here:
-- [bowling.com's Storm Equinox Hybrid review]... We could use all the
-- video transcripts to create a FAQ that is meaningful dynamicly based
-- on what is being talked about in the videos. We also have alot of
-- content already accumulated from all the sources... We would want to
-- use this on bowlerdepot.com and could have another project that is
-- the front end but this could be the backend that pulls together all
-- the creative and content for the frontend. We could use the same
-- video filter that we are using for the existing embed."
--
-- Scoped via follow-up questions: full bowling.com-shaped article (not
-- just the FAQ piece), requires admin review before it's exposed via
-- public_api (same reasoning product_videos/product_price_sources
-- already use for AI/discovery-produced content -- see those tables'
-- own review-queue precedent), and only ever generated for a product
-- that already clears the SAME bar video_reviews_summary uses: at least
-- one status='approved', summary is not null video, additionally
-- excluding any video whose channel is in blocked_video_channels
-- (021_blocked_video_channels.sql -- Al's explicit ask to reuse that
-- filter here too). A product with no qualifying video content gets no
-- article yet rather than a thin one written purely from manufacturer
-- copy.
--
-- One row per product (unique on product_id) -- same "regenerate
-- overwrites in place" convention as products.video_reviews_summary,
-- not a versioned history table. status starts 'pending' on every
-- (re)generation, same review_status shape product_videos already uses,
-- so a regenerate of a previously-approved article goes back through
-- review rather than silently replacing live content.
--
-- Structured fields (not a single HTML/markdown blob): Al's own framing
-- was "this could be the backend that pulls together all the creative
-- and content for the frontend" -- a separate frontend project needs to
-- style/lay out each section itself, not parse HTML back apart. Spec
-- values themselves (RG, differential, core, coverstock, release date,
-- etc.) are deliberately NOT duplicated into this table -- the
-- comparison_table/spec-highlight portions of an article are meant to
-- be joined against the live products/product_skus/cores/coverstocks
-- data at READ time (see the eventual public_api endpoint), so a spec
-- correction never requires regenerating the article to stay accurate.
--
-- faq/comparison_table/who_should_buy/who_should_skip/pros/cons/
-- sibling_product_ids/source_video_ids are jsonb rather than normalized
-- child tables -- same "this is generated, reviewed, and replaced as a
-- whole unit, never edited field-by-field" reasoning product_videos'
-- own summary/transcript columns already lean on (no per-sentence
-- editing UI exists for those either). sibling_product_ids in
-- particular is a HEURISTIC inference (same brand + a normalized
-- "line name" derived from stripping cover/finish qualifier words off
-- the product name, e.g. "Equinox Solid"/"Equinox Hybrid" -> "Equinox")
-- -- there is no real product-line/family grouping concept anywhere in
-- this schema today (the `cores` table is a physical-core dedup, not a
-- marketing line). The heuristic will sometimes be wrong; it's surfaced
-- for admin review alongside everything else in the article rather than
-- treated as ground truth.

begin;

create table product_articles (
    id uuid primary key default uuid_generate_v4(),
    product_id uuid not null unique references products(id) on delete cascade,
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),

    title text,
    hook text,                                  -- short narrative-anecdote opening, e.g. bowling.com's "You know the night..." paragraph
    performance_summary text,                   -- the long-form "how does it actually roll" synthesis across video transcripts/summaries
    who_should_buy jsonb not null default '[]'::jsonb,   -- array of bullet strings
    who_should_skip jsonb not null default '[]'::jsonb,  -- array of bullet strings
    pros jsonb not null default '[]'::jsonb,     -- array of strings
    cons jsonb not null default '[]'::jsonb,     -- array of strings
    buying_tips text,                            -- maintenance/drilling/finish guidance paragraph
    verdict text,                                -- final-verdict paragraph
    faq jsonb not null default '[]'::jsonb,       -- array of {question, answer} objects -- the dynamic part: grounded in what THIS product's videos actually discuss, not a fixed template of questions asked on every product
    comparison_table jsonb not null default '[]'::jsonb,  -- array of {product_id, product_name, note fields...} rows for inferred siblings -- see sibling_product_ids
    sibling_product_ids jsonb not null default '[]'::jsonb,  -- heuristically inferred related products in the same marketing line -- see header comment
    source_video_ids jsonb not null default '[]'::jsonb,     -- product_videos ids actually fed into this generation, for traceability/debugging a bad article back to its source content

    generated_at timestamptz,
    reviewed_at timestamptz,
    resolved_by text,
    created_at timestamptz not null default now()
);

create index product_articles_status_idx on product_articles (status);

comment on table product_articles is 'AI-generated ball-review-style article content per product (022_product_articles.sql) -- Al: "this could be the backend that pulls together all the creative and content for the frontend." Generated by src/product_article_generator from approved, non-blocked-channel video summaries/transcripts plus product specs/description; reviewed (approved/rejected) via admin_api before public_api ever exposes it.';
comment on column product_articles.faq is 'Array of {question, answer} objects. Deliberately dynamic per product (grounded in what that product''s own videos actually discuss) rather than a fixed template of the same questions on every page -- see this migration''s header comment for the full reasoning against bowling.com''s own templated FAQ.';
comment on column product_articles.sibling_product_ids is 'Heuristically inferred (same brand + normalized line name) related products in the same marketing line, e.g. Equinox / Equinox Solid / Equinox Hybrid -- NOT backed by any real product-line grouping table (none exists in this schema). Wrong or missing entries are expected sometimes; surfaced for admin review, not treated as ground truth.';

commit;
