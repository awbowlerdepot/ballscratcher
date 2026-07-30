-- 002_add_woocommerce_netsuite_platforms.sql
--
-- Adds two new source_platform values, expanding scope beyond Brunswick/
-- Radical/DV8 (craft_cms), Hammer/Ebonite/Track/Powerhouse (shopify), and
-- Storm/RotoGrip/900Global/3G/Master (commercebuild):
--
--   woocommerce: SWAG Bowling (swagbowling.com). WordPress + WooCommerce,
--     fully server-rendered, confirmed live -- see src/woocommerce_*/app.py
--     module docstrings for the real product-page structure this was
--     built against.
--
--   netsuite: MOTIV Bowling (motivbowling.com). NetSuite SuiteCommerce
--     (identifiable by the /n_<id> permalink pattern). Added here even
--     though the scraper for it isn't built yet, alongside woocommerce,
--     to avoid a second one-line migration later -- confirmed real and
--     in-scope, just sequenced after SWAG. See the architecture doc /
--     session notes for the bot-detection caveat on its product pages
--     (server-rendered content, but non-browser requests to product URLs
--     specifically come back empty -- likely session-cookie-gated, not
--     yet proven to need a full headless browser).

begin;

alter type source_platform add value 'woocommerce';
alter type source_platform add value 'netsuite';

commit;
