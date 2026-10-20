-- Runbook 6ea: content fingerprint for Shopify discovery.
--
-- After the 6dx lastmod fix, Brunswick/Radical/DV8/Swag dropped to 0
-- "changed" a day, but Hammer/Track/Ebonite still reported every product
-- changed, every day (Hammer 224, Track 171, Ebonite 195). That wasn't the
-- string-vs-datetime bug: those Shopify stores genuinely re-stamp every
-- product's updated_at at once, several times a day (2026-10-19: every
-- sampled Hammer product read 16:21:08), from inventory syncs and apps
-- touching products. updated_at is no signal for "the ball's page
-- changed". Al: "yes build the shopify fingerprint fix".
--
-- shopify_url_discovery now stores a sha256 of only what
-- shopify_product_scraper actually reads (title, body_html, product_type,
-- tags, and the image list's position/src) and compares that instead.
-- Nullable: the other platforms don't use it, and a Shopify row's first
-- fingerprint is recorded as a baseline, not treated as a change (the
-- Sunday full re-scrape, 6dx, covers anything that changed before it).
alter table discovered_urls add column if not exists content_fingerprint text;

comment on column discovered_urls.content_fingerprint is
  'Shopify discovery only (runbook 6ea): sha256 of the products.json fields shopify_product_scraper reads (title, body_html, product_type, tags, image position/src). Changed fingerprint => re-scrape. Shopify''s own updated_at churns store-wide several times a day, so it is not used for change detection. NULL for other platforms.';
