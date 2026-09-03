-- 020_bowlerdepot_video_sync.sql
--
-- Al's ask: "for the public side of things what is the best way to
-- include a section containing the data from this project on our
-- bigcommerce product pages... when you visit a ball it will pull in the
-- video section into the video section of the bigcommerce product page."
--
-- Investigated live against a real BowlerDepot product page
-- (bowlerdepot.com/brunswick-combat-solid/) before designing this: the
-- storefront theme (Supermarket) already has a native "Videos" tab
-- (#tab-videos, .videoGallery--inTab) wired up to BigCommerce's own
-- built-in Product Videos feature -- already populated with a
-- manufacturer video for this product. BigCommerce's own Catalog API
-- (POST /catalog/products/{id}/videos, confirmed via their docs: fields
-- title/description/sort_order/type/video_id, video_id required) writes
-- directly into that same native slot, which the theme already renders --
-- so the right integration is a small server-side sync pushing our
-- approved videos there, NOT a client-side widget trying to build a video
-- player from scratch. See src/bowlerdepot_video_sync/app.py's module
-- docstring for the full design.
--
-- These two columns are this sync's own idempotency/bookkeeping, same
-- shape as product_price_sources.last_checked_at: bowlerdepot_video_id
-- is the id BigCommerce's API returns for the video it created (needed if
-- this ever needs to update/delete a pushed video later), and
-- bowlerdepot_synced_at marks a row as already pushed so a re-run of the
-- sync doesn't re-POST (and duplicate) a video BigCommerce already has.
-- Both stay null for every video until the sync job actually pushes it;
-- a null bowlerdepot_synced_at is exactly what the sync's own "needs
-- sync" query selects on.

begin;

alter table product_videos add column bowlerdepot_video_id text;
alter table product_videos add column bowlerdepot_synced_at timestamptz;

comment on column product_videos.bowlerdepot_video_id is 'The id BigCommerce''s Catalog API returned for this video after src/bowlerdepot_video_sync pushed it into the product''s native Product Videos feature (migration 020). Null until synced.';
comment on column product_videos.bowlerdepot_synced_at is 'When this video was last successfully pushed to BigCommerce (migration 020) -- null means never synced, which is what the sync job''s "needs sync" query selects on.';

commit;
