-- 028_product_articles_bigcommerce_sync.sql
--
-- Al: "lets add a flag to each article that would sync them to bigcommerce
-- if on" -- follow-up to the "how do we get these articles onto
-- bowlerdepot.com" question. Same three-column shape 020_bowlerdepot_
-- video_sync.sql already established for product_videos, applied here to
-- product_articles instead: an admin-controlled boolean gate, plus the
-- sync job's own idempotency/bookkeeping columns.
--
-- sync_to_bigcommerce is a SEPARATE gate from status='approved' -- an
-- article being approved (safe to show on data.bowleriq.com via
-- public_api) does NOT by itself mean Al wants it published as a
-- bowlerdepot.com blog post too (different audience/SEO surface, and Al
-- may want to review the bowlerdepot.com framing/copy separately before
-- it goes out under the store's own name). Defaults to false: nothing
-- syncs to BigCommerce until an admin opts a specific article in, same
-- "off until explicitly turned on" default sync_to_bigcommerce's own
-- name implies. The eventual sync job's own "needs sync" query (see
-- src/bowlerdepot_article_sync -- not built yet, this migration only
-- adds the schema/toggle it will read) is expected to require BOTH
-- status='approved' AND sync_to_bigcommerce=true, same "public_api's own
-- approved bar, never looser" reasoning bowlerdepot_video_sync's
-- list_videos_needing_sync already applies.
--
-- bigcommerce_post_id/bowlerdepot_synced_at mirror product_videos.
-- bowlerdepot_video_id/bowlerdepot_synced_at exactly: the id BigCommerce's
-- API returns for whatever it creates (a Blog API post, per the current
-- design -- see this migration's sibling spec in DEPLOY_RUNBOOK.md
-- section 6u), and a synced-at timestamp so a re-run only pushes an
-- article once. Both null until the sync job actually runs.
--
-- Turning sync_to_bigcommerce off after a successful sync is deliberately
-- NOT wired to delete/unpublish the BigCommerce post automatically in
-- this migration -- that's a real design question (does "off" mean
-- "never synced this one" or "take it down"?) left to the sync job's own
-- spec rather than assumed here. bowlerdepot_synced_at staying non-null
-- after the flag is turned off is the deliberate signal that a post may
-- still be live on bowlerdepot.com even though the flag no longer says
-- to sync it.

begin;

alter table product_articles add column sync_to_bigcommerce boolean not null default false;
alter table product_articles add column bigcommerce_post_id text;
alter table product_articles add column bowlerdepot_synced_at timestamptz;

comment on column product_articles.sync_to_bigcommerce is 'Admin-controlled toggle (migration 028, admin-site Articles tab): when true, the eventual bowlerdepot_article_sync job includes this article in its "needs sync" query (alongside status=''approved'') to push it to bowlerdepot.com as a BigCommerce Blog post. Defaults false -- nothing syncs until explicitly turned on, independent of public_api approval.';
comment on column product_articles.bigcommerce_post_id is 'The id BigCommerce''s Blog API returned for the post created from this article (migration 028), needed to update/delete it later. Null until synced.';
comment on column product_articles.bowlerdepot_synced_at is 'When this article was last successfully pushed to BigCommerce (migration 028) -- null means never synced. Same idempotency convention as product_videos.bowlerdepot_synced_at (020_bowlerdepot_video_sync.sql): a re-run of the sync job only pushes a not-yet-synced row.';

commit;
