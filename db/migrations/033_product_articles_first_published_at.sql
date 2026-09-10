-- 033_product_articles_first_published_at.sql
--
-- Al: "can we add published dates and last updated dates to the
-- articles." reviewed_at (022_product_articles.sql) already looked
-- like it answered this -- it's stamped on every approve_article call
-- -- but it's the WRONG source for a "published" date specifically,
-- because it's re-stamped on EVERY approval, including a re-approval
-- after a regenerate_text/regenerate_images run puts the row back to
-- 'pending' (see product_article_generator/app.py's regenerate paths).
-- So today, regenerating and re-approving an article silently erases
-- its true original publish date -- reviewed_at conflates "first went
-- live" and "most recently touched" into one column with no way to
-- tell them apart after the first regeneration.
--
-- first_published_at fixes that: set exactly once, on an article's
-- FIRST-EVER approval, and never touched again by any later
-- regenerate/re-approve cycle. reviewed_at keeps doing exactly what it
-- already does (stamped fresh on every approval) and becomes the
-- honest "last updated" signal, now clearly distinct from this new
-- column. See admin_api/service.py's approve_article for the
-- coalesce()-based "only ever set once" update.
--
-- Backfill: every article that's ALREADY approved has already lost its
-- true original publish date (reviewed_at has been overwritten at
-- least once for any of those that were ever regenerated, and there's
-- no way to recover the real value from history). reviewed_at is the
-- best available estimate for those rows -- same "known-imperfect but
-- honest fallback" reasoning products.first_seen_at gets used for
-- release_date sort ordering elsewhere in this schema -- so the
-- backfill below sets first_published_at = reviewed_at for every
-- already-approved row rather than leaving it null (which would make
-- every pre-migration article's Learn page show no Published date at
-- all until it happens to get regenerated again).

begin;

alter table product_articles add column first_published_at timestamptz;

update product_articles
set first_published_at = reviewed_at
where status = 'approved' and reviewed_at is not null;

comment on column product_articles.first_published_at is 'Set exactly once, on this article''s first-ever approval (033_product_articles_first_published_at.sql) -- never overwritten by a later regenerate+re-approve cycle, unlike reviewed_at (which IS re-stamped every approval and should be read as "last updated"). Backfilled to reviewed_at for rows that were already approved before this migration -- their true original publish date isn''t recoverable, reviewed_at is the best honest estimate.';

commit;
