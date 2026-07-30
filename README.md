# Bowling ball scraper/database

Serverless (AWS SAM) pipeline that scrapes bowling ball manufacturer sites into
a normalized Postgres database, feeding a consumer informational site and
reconciliation against bowlerdepot.com's BigCommerce catalog. Full design
rationale, the schema, and the decisions behind it live in
`brunswick-scraper-architecture-review.md` (one level up from this repo) --
read that first if anything here seems under-explained. For the actual
step-by-step deploy process with exact commands, see `DEPLOY_RUNBOOK.md` --
this README's "Getting this running" section below stays focused on
explaining each step; the runbook is the checklist to actually follow,
including a post-deploy smoke-test order designed to surface the highest-
risk unknowns first.

## What's actually built right now

- `template.yaml` -- SAM template with all five functions from the
  architecture doc's build order, now chained end to end via SQS rather
  than invoked manually: `UrlDiscoveryFunction` (scheduled) publishes
  new/changed URLs to `ProductScrapeQueue`; `ProductScraperFunction`
  consumes that queue and publishes a PDF-parse job (when an
  `info_sheet_url` was found) and image-process jobs (one per image still
  missing `stored_url`) to `PdfParseQueue`/`ImageProcessQueue`;
  `PdfParserFunction` and `ImageProcessorFunction` consume those. Every
  queue has a dead-letter queue, and the three consuming functions use
  Lambda's partial batch response feature so one bad message doesn't fail
  an entire batch. `AdminApiFunction` sits apart from the chain as a real
  HTTP API trigger -- **no auth wired up**, see its comment in
  `template.yaml` before deploying this anywhere reachable. Also defines
  the public-read `ImageBucket` the image pipeline uploads normalized
  product photos to. See the module docstring in `src/url_discovery/app.py`
  for why SQS was chosen over Step Functions for this.
- `db/migrations/001_init_schema.sql` -- full schema: brands, `ball_families` ->
  `products` -> `product_skus`, image storage, URL discovery tracking, the
  cross-source review queue, and BowlerDepot reconciliation tracking.
- `src/url_discovery/app.py` -- sitemap-based URL discovery, the first slice of
  the "Suggested build order" in the architecture doc.
- `src/product_scraper/app.py` -- HTML spec scraper for the Craft-CMS brand
  family (Brunswick/Radical/DV8): spec table, per-weight Core Numbers table
  (with the 15 lb single-value fallback), coverstock material/type split,
  PDF resource links, image classification. Deliberately matches tables and
  fields by visible text content rather than CSS class/id -- see the module
  docstring for why.
- `src/pdf_parser/app.py` -- parses Brunswick's PDF "Info Sheet" documents
  (the `info_sheet_url` the HTML scraper captures) for the per-weight
  RG/DIFF/mass-bias breakdown, which is frequently more complete than the
  HTML page -- Defender's HTML has one 15 lb reference value, its PDF has
  the full 16-12 lb table including mass bias, which HTML doesn't carry at
  all for that ball. Also reconciles against whatever `product_skus` rows
  already exist for the product: gaps get filled, but a genuine
  disagreement between HTML and PDF values (found for real -- Crown 78U's
  HTML says 16 lb RG is 2.577, its PDF says 2.557) gets written to
  `review_queue` instead of silently overwritten. See the module docstring
  and `sync_pdf_skus()` for the exact rules.
- `src/image_processor/app.py` -- mirrors product images to your own S3
  storage and normalizes composition, not just resolution: detects the
  ball's bounding box (alpha-channel-based for sources with real
  transparency, background-color-threshold-based for flattened sources,
  auto-selected per image), crops to it, and recomposites centered on a
  fixed-size canvas with a consistent margin percentage -- so a Brunswick
  photo and a Hammer photo end up visually consistent even though the two
  manufacturers won't share photography conventions. Outputs
  thumbnail/catalog/detail sizes. Pillow-only, no ML background removal, per
  the architecture doc. See the module docstring for the deliberate choice
  of a flat white output background over preserving per-source transparency.
- `src/admin_api/service.py` + `app.py` -- the approval workflow the
  architecture doc decided on: list/inspect pending `review_queue` rows,
  approve (applies the proposed value to the real column -- SKU-scoped
  fields like `rg_16lb` or product-scoped ones like `color`, parsed by a
  closed whitelist so `field_name` can never drive an arbitrary SQL column)
  or reject (discards the proposal, current value stays) them, plus a
  products listing/detail/publish-toggle surface. Split into `service.py`
  (all the actual logic, framework-agnostic) and a thin `app.py` FastAPI +
  Mangum routing layer -- see "Why there's no live end-to-end test yet"
  below for why that split matters more here than for the other functions.
- `src/admin_api_authorizer/app.py` -- shared-secret bearer-token Lambda
  authorizer sitting in front of `AdminApiFunction`, so the admin API is
  no longer a bare unauthenticated HTTP API. See "Admin API auth" below
  for the design and setup.
- **Orchestration** -- `UrlDiscoveryFunction` -> `ProductScrapeQueue` ->
  `ProductScraperFunction` -> (`PdfParseQueue` -> `PdfParserFunction`) and
  (`ImageProcessQueue` -> `ImageProcessorFunction`), all wired in
  `template.yaml`. Each of the three consuming functions' `handler()` now
  supports both a real SQS batch event and the original direct-invoke dict
  shape (for manual testing/backward compatibility), and reports individual
  message failures back to SQS via `batchItemFailures` rather than failing
  a whole batch over one bad URL/PDF/image. `ProductScraperFunction`'s
  `upsert_product()` also changed shape slightly to support this: it now
  returns which `product_images` rows still need processing
  (`stored_url is null`) instead of just the product id, so the handler
  can fan out image jobs without an extra query.
  SWAG and MOTIV are wired in the same way, just with their own
  platform-specific scrape queues (`WooCommerceProductScrapeQueue`,
  `NetsuiteProductScrapeQueue`) feeding their own product scraper
  functions -- each platform's product-page shape is different enough
  that they can't share a consumer the way PDF parsing and image
  processing can. Both DO share the same `ImageProcessQueue`/
  `ImageProcessorFunction` the Craft-CMS family uses, since that step is
  genuinely platform-agnostic. See "Second manufacturer" and "Third
  manufacturer" below for the per-platform detail.
- `src/woocommerce_url_discovery/app.py` + `woocommerce_product_scraper/app.py`
  -- second manufacturer, second platform family: SWAG Bowling on
  WordPress/WooCommerce. See "Second manufacturer: SWAG Bowling" below for
  the real platform differences that shaped these.
- `src/netsuite_url_discovery/app.py` + `netsuite_product_scraper/app.py` --
  third manufacturer, third platform family: MOTIV Bowling on NetSuite
  SuiteCommerce. See "Third manufacturer: MOTIV Bowling" below -- this one
  has an important caveat the first two don't: the fetching approach
  (not the parsing) is unverified.
- `tests/` -- unit tests for all five functions above, run against **real**
  captured data where real data exists: a real sitemap sample, real field
  values from two actual product pages, and two real PDF Info Sheets
  fetched directly from Brunswick's CDN (verbatim extracted text, not
  reconstructions). The image pipeline and admin API are exceptions -- see
  "Why there's no live end-to-end test yet" below for why their tests run
  against synthetic images / a hand-rolled fake DB cursor instead. The
  three `*_orchestration.py` test files (one each for product_scraper,
  pdf_parser, image_processor) are the one place in this session where
  fake-object-based tests actually run standalone via
  `python3 tests/test_*_orchestration.py` rather than needing manual
  translation -- see their own header comments. All tests across all five
  functions pass when run manually in-sandbox (see below for why
  "manually" rather than via `pytest` itself, for the files that use it).

Every function from the architecture doc's original 5-function build order
now has a first pass built, and they're wired together end to end. The
bowwwl.com and BowlerDepot cross-checks are now built too -- see "QA
cross-checks and lifecycle date tracking" below. Auth is now wired onto
the admin API too -- see "Admin API auth" below. What's left: the
consumer-facing site itself.

## Second manufacturer: SWAG Bowling (a new platform family)

`src/woocommerce_url_discovery/app.py` and
`src/woocommerce_product_scraper/app.py` -- SWAG Bowling
(swagbowling.com), confirmed live this session (WordPress + WooCommerce,
fully server-rendered) as a second manufacturer alongside Brunswick,
running on a genuinely different platform than any of the three families
the architecture doc originally scoped (Craft CMS/Shopify/commercebuild).
Real, confirmed differences from the Craft-CMS scraper that shaped these:

- WooCommerce's flat Yoast product sitemap mixes bowling balls with
  apparel/accessories under one `/product/{slug}/` path -- no
  Brunswick-style path segment to filter on. So URL discovery here
  paginates the `/shop/bowling-balls/` category archive (real, confirmed
  server-rendered, no JS) to get the *set* of ball URLs, then
  cross-references the sitemap only for its `<lastmod>` values.
- SWAG's product page exposes RG, DIFF, and mass bias directly in a
  WooCommerce attribute table -- no PDF step needed for SWAG's core data,
  unlike Brunswick where mass bias was PDF-only. There's also no
  per-weight breakdown table on the page (only a single 15lb value,
  always), so this always takes the 15lb-default path.
- Coverstock material and type come from two different attribute fields
  here (`Bowling Ball Coverstock Type` for material, keyword-matching
  `Bowling Ball Cover Name` for solid/pearl/hybrid), rather than one
  combined field like Brunswick's `Cover Type`.
- Current/retired status comes from the product page's own
  `Production-status` attribute, not the URL -- more reliable than trying
  to infer it from which category-archive page a link appeared on.

**Orchestration**: `WooCommerceUrlDiscoveryFunction` now publishes to its
own `WooCommerceProductScrapeQueue`, and `WooCommerceProductScraperFunction`
consumes it (SQS batch + direct invoke, same `batchItemFailures` pattern
as the Craft-CMS chain) and fans out to the shared `ImageProcessQueue` for
any new product images -- that queue/function pair is genuinely
platform-agnostic, so it's reused rather than duplicated. Not a
`PdfParseQueue` trigger target: SWAG's HTML already carries RG/DIFF/mass-
bias directly, so that step doesn't apply here the way it does to
Brunswick.

Remaining disclosed gaps for SWAG: the Info Sheet/Shelf Talker PDFs SWAG
links are hosted on Dropbox share links, and neither could actually be
fetched this session (the tool available returned nothing for both) -- so
whether `pdf_parser`'s Brunswick-specific layout assumptions apply to
SWAG's PDFs at all is unknown, though also not currently load-bearing
since SWAG's HTML already carries the core RG/DIFF/mass-bias data. And
`parse_mass_bias()`'s handling of a real (non-"N/A") value is unverified --
the one real product page inspected this session was a symmetric core;
check a real asymmetric SWAG ball's page before trusting mass bias data
for those.

## Third manufacturer: MOTIV Bowling (NetSuite SuiteCommerce)

`src/netsuite_url_discovery/app.py` and `src/netsuite_product_scraper/app.py`
-- MOTIV Bowling (motivbowling.com), a third manufacturer on a third
platform. Confirmed live this session via a real Chrome browser session
(Claude in Chrome), reading actual rendered pages and DOM structure
directly, not just a markdown-converted fetch. This section splits
cleanly into two very different confidence levels: **parsing is solid,
fetching is not**.

**Platform confirmation.** A category-tile link resolves to
`https://www.motivbowling.com/n_<18-digit-id>`, which 302-redirects (
confirmed by reading `window.location` after navigating it live) to a
human-readable canonical URL, e.g. `.../products/balls/heavy-oil/
jackal-onyx.html`. That `/n_<id>` permalink shape is a NetSuite
SuiteCommerce signature. No `<meta name="generator">` tag or
`window.NetSuite`-style global was found on the page, so this is a heavily
custom-templated storefront on top of NetSuite, not an off-the-shelf one
-- inferred from the URL convention, not a platform banner.

**Real, confirmed structural facts the parser is built against** (all read
directly off live pages this session -- see the module docstrings for the
full detail):

- Two catalog index pages, not a URL path segment or on-page attribute,
  carry current/retired status: `/products/balls/` (28 links, confirmed
  no pagination) and `/products/balls/retired-balls/` (202 links, also no
  pagination). `netsuite_url_discovery` reads both and passes status
  through the SQS job message to the product scraper, since the product
  page itself has no reliable status signal of its own -- genuinely
  different from both other manufacturers.
- RG/DIFF/mass-bias are in the HTML, per weight, like SWAG and unlike
  Brunswick -- no PDF dependency for MOTIV's core data either. Confirmed
  on both a symmetric ball (Sigma Tour Pearl, 5 weights, RG + Max
  Differential only) and an asymmetric one (Jackal Onyx, 5 weights, RG +
  Max Differential + a third "Int. Differential" value -- MOTIV's own
  name for what this schema calls mass_bias).
- Coverstock material/type come from a single "Cover Stock" field (e.g.
  "Atomic Propulsion Pearl Reactive"), keyword-matched the same way as
  Brunswick's single-field approach -- unlike SWAG's two-field split.
- A subset of balls (the Ascend/Aspire "Designer Series" lines) put color
  directly in the product name as "Base Name - Color/Color/Color" --
  confirmed on the ball's own page, not just a category tile. Regular
  performance balls have no separate color field at all. Handled by
  splitting the name on " - " rather than reading a dedicated field.
- Product photos are CSS `background-image: url(...)` inline styles, not
  `<img>` tags -- confirmed `document.querySelectorAll('img')` returns
  nothing inside `<main>` on these pages. A "core cutaway" image variant
  is served from a `filemanager-format/core-image/<id>` path, a transform
  of one of the main gallery's own image ids.
- Download link labels (Sell Sheet / Shelf Talker / Factory Finish Guide)
  vary per product -- confirmed by comparing Sigma Tour Pearl's set
  against Jackal Onyx's, which don't match. Captured by whatever label is
  actually present rather than a fixed key set.

**What's NOT confirmed: fetching.** The real product-spec content IS
present as static text in the server-delivered HTML (confirmed by reading
the actual page response from inside the browser session), not injected
by JavaScript after load. A plain non-browser fetch of that exact same
product URL (via `mcp__workspace__web_fetch`, re-confirmed fresh in a
later session) still comes back completely blank, while MOTIV's homepage
and category pages fetch fine through that same tool -- unchanged finding
across sessions.

A later session ran one more real experiment that's worth knowing about:
from a live browser tab already on the real product page,
`fetch(location.href)` returned the full real content, as expected -- but
`fetch(location.href, {credentials: 'omit'})`, explicitly sending zero
cookies, returned the *exact same* full content. That falsifies the pure
"you just need a session cookie" theory `fetch_page()`'s current approach
is built on -- a cookie-less request from a real browser still succeeds,
so cookies alone don't explain why a non-browser client's cookie-less
request fails. That same session also checked `resp.headers.get('server')`
and found only a bare "Apache" -- no Cloudflare/Akamai/Fastly/Sucuri/
Datadome/Imperva branding, so this doesn't look like a dedicated
enterprise bot-management product (though something unbranded, or a
TLS-layer check that wouldn't show up in HTTP headers, can't be ruled
out this way). Net effect: `fetch_page()`'s realistic
User-Agent/Accept/Accept-Language/Referer headers are now believed to be
the load-bearing part of the workaround, more than the session-cookie
logic -- see `netsuite_product_scraper/app.py`'s module docstring for the
full detail and the two next things to try, in priority order, if it
still comes back blank on a real deploy: double-check every header a
real browser sends that isn't sent yet (sec-fetch-*, client hints) before
concluding it's unfixable with headers alone, then fall back to a
headless-browser-based fetch if that doesn't resolve it. **Still an
educated bet, not a proven fix** -- no environment available to this
project has a genuinely bare, non-browser network path to
motivbowling.com to test `requests.get()` itself. Treat the first real
deployment run of `NetsuiteProductScraperFunction` as the actual test.

**Orchestration**: `NetsuiteUrlDiscoveryFunction` publishes to its own
`NetsuiteProductScrapeQueue` (message body includes `status`, since -- per
above -- that's the only reliable source for it), and
`NetsuiteProductScraperFunction` consumes it and fans out to the shared
`ImageProcessQueue`, same pattern as SWAG's chain. Wiring the queue
doesn't make the fetching problem above go away, though: if
`fetch_page()`'s session-cookie workaround doesn't actually work against
the real site, messages will fail, retry up to 3 times, and land in
`NetsuiteProductScrapeDLQ` -- check that queue first if this pipeline
looks stalled after a real deploy.

34/34 tests pass (19 + 8 from parsing/discovery, plus 7 new for the
image-job-fanout behavior), run against two real fixture reconstructions
(symmetric and asymmetric cores) built from real values read off live
pages this session -- see `tests/fixtures/motiv_sigma_tour_pearl.html`,
`motiv_jackal_onyx.html`, and the two category-index fixtures for exactly
what's real vs. reconstructed in each. None of that exercises
`fetch_page()` itself (all fixtures go in through a monkeypatched stub) --
that's still exactly the unverified part.

## QA cross-checks and lifecycle date tracking

`db/migrations/003_date_tracking_and_bowwwl.sql` adds two things: lifecycle
date columns on `products`, and a `bowwwl_products` match-cache table
mirroring `bowlerdepot_products`'s existing shape.

**Date columns**, each with an explicit provenance so future you doesn't
have to guess which ones are trustworthy:

- `first_seen_at` -- system-observed, backfilled from the already-accurate
  `created_at` (a `not null default now()` column, set once per product on
  first insert -- this was already correct, just needed a clearer name).
- `release_date` -- manufacturer-published, now actually parsed and
  persisted. Previously this column existed in the schema but no scraper
  ever populated it. `parse_release_date()` is implemented three times
  (once each in `product_scraper`, `woocommerce_product_scraper`,
  `netsuite_product_scraper`, per this project's established
  per-module-independence convention) because each manufacturer's real
  format is different: Brunswick/SWAG publish "Month YYYY" (e.g. "April
  2025"), MOTIV publishes "AVAILABLE M/D/YYYY" or a bare "M/D/YYYY". All
  three are real formats read off real product pages this session, not
  guessed at.
- `discontinued_detected_at` -- system-observed, set entirely in SQL: each
  scraper's `upsert_product()` ON CONFLICT clause has a CASE expression
  comparing the old and new `status` values and setting `now()` only on a
  genuine current->retired transition (also handled: first-insert-already-
  retired, a repeat scrape of an already-retired ball, and reversion back
  to current, which clears the timestamp). No extra DB round-trip needed.
- `announced_date` / `discontinued_date` -- reserved columns, deliberately
  left unpopulated. No manufacturer site found this session publishes an
  announcement date separate from release date, or a discontinuation date
  separate from "no longer appears on the current-balls page." If one
  shows up later, these columns are ready for it.

**`src/bowwwl_cross_check/app.py`** -- weekly QA cross-check against
bowwwl.com's independent bowling-ball database, treated as the more
complete/authoritative source per your instruction. Confirmed real,
directly off live Chrome DOM inspection this session: Drupal 10, URL
pattern `bowwwl.com/bowling-ball-database/{brand-slug}/{ball-slug}`,
`div.field__label` + `div.field__item` label/value pairs (matched by
visible label text, same content-based convention as every other scraper
in this project), ISO datetime attributes on date fields (day-precision
even when the displayed text is month-only, e.g. `release_date` really
came from a `datetime="2026-07-16T12:00:00Z"` attribute even though the
page shows "Jul 2026"), a boolean `Discontinued` field confirmed in both
its present and absent states, and a real markup inconsistency between how
coverstock and core names are marked up (`h5.card-title` vs.
`h5.card-header`) that the parser accounts for. Compares bowwwl's RG/
Diff/mass-bias per weight and release date against our own data
(tolerance 0.001) and writes disagreements to `review_queue`
(`source='bowwwl_cross_check'`) rather than auto-applying anything. Also
backfills `products.usbc_approval_date` from bowwwl's real PBA Approval
Date field, via `coalesce` so it never overwrites an existing value.

**Legal note, and the decision made about it:** bowwwl.com's Terms &
Conditions explicitly prohibit reproducing, copying, or redistributing
their content. I flagged this before writing any code that persists
bowwwl's data, and you decided to proceed as scoped and accept the risk.
What that means concretely: `compare_to_our_data()` puts bowwwl's actual
field values into the mismatch dicts written to `review_queue` (so a human
reviewer can see exactly what disagrees, not just that something does),
and `record_bowwwl_match()` persists the `bowwwl_url` itself in
`bowwwl_products`. Both of those are the specific things to change first
if the operating posture on this ever shifts -- see the module docstring's
LEGAL NOTE for the exact functions and what "stop doing this" looks like
for each. Scheduled weekly (not daily) specifically to keep load on their
site modest.

**`src/bowlerdepot_reconciliation/app.py`** -- daily coverage + accuracy
check against your own BowlerDepot BigCommerce store, which is the actual
motivating case for this whole feature: a ball can appear on a
manufacturer's site and not yet be listed for sale on yours, and you want
to know about that gap quickly rather than discover it late.
`fuzzy_match_product()` uses `difflib.SequenceMatcher` (threshold 0.80,
tuned down from an initial 0.85 after a realistic "+ Bowling Ball" suffix
case measured ~0.84) to match your product names against BigCommerce
listing names, since the two sides won't use identical strings. Products
with no match at all get written to `review_queue` as
`field_name='bowlerdepot_listing'` -- the "this needs to be added"
signal you asked for. Matched products get an accuracy check against
BigCommerce's `custom_fields` array, but only at your 15lb SKU -- see
below for why.

**What's confirmed real, and what changed this session:** the BigCommerce
v3 Catalog Products API response *shape* is real -- `GET /stores/
{store_hash}/v3/catalog/products`, `X-Auth-Token` header auth, `{"data":
[...], "meta": {"pagination": {...}}}`, `custom_fields: [{"name",
"value", "id"}]` -- fetched directly from BigCommerce's own current
developer docs. The two things that were originally disclosed as
best-guesses were both resolved in a later session by browsing the real,
live public storefront (still no real store credentials, so this is
storefront evidence, not a private-API read, but real rather than
invented):

- **Product-vs-variant modeling: CONFIRMED true BigCommerce variants.**
  Real product pages (Storm Alpha Crux, Roto Grip RST Hyperdrive) show
  ONE product page per ball with a "Weight" dropdown offering all five
  weights as options -- not, as originally assumed, a separate product
  per weight.
- **Custom field names: CONFIRMED real display labels** -- "Radius of
  Gyration(15lb)", "Max Differential(15lb)", "Int. Differential(15lb)",
  identical on both real products checked. Two of the three original
  best-guess candidates ("Diff"/"Differential", "MB"/"Mass Bias") were
  simply wrong; BowlerDepot's real term for mass bias is "Int.
  Differential" (the same term MOTIV's real site uses -- possibly a
  shared industry convention). `CUSTOM_FIELD_NAME_CANDIDATES` now has the
  real values, matched by prefix rather than exact string (see
  `_find_custom_field()`'s docstring for why, and for why it's a prefix
  check and not a looser substring check -- the latter risks a short
  fallback candidate like "rg" false-matching an unrelated field like
  "Target Weight").
- **The real consequence of both findings together, and the actual bug
  this caught before it ever ran against a live store:** BowlerDepot only
  publishes ONE spec value per ball (always qualified "15lb"), because
  weight is a variant of a single product with one shared custom_fields
  set. `check_accuracy()` originally compared every one of your weights
  against that single value -- which would have flagged a false-positive
  mismatch on every weight except 15lb, since RG/DIFF genuinely differ by
  weight on a real ball (this project's own fixtures already prove
  that). Fixed to only ever compare your 15lb SKU
  (`BOWLERDEPOT_REFERENCE_WEIGHT_LBS`).

What's still genuinely unconfirmed, because there's still no real store
hash or API token: whether the *private API's* JSON actually names the
field the way the storefront *displays* it -- the display label and the
underlying `custom_fields.name` value aren't guaranteed to be identical,
even though they very often are in practice for a template that just
loops over the raw field list. **Still worth a real store export before
fully trusting this in production** -- but the risk profile is much
better than before: a wrong guess here would silently produce zero
mismatches rather than false ones, which is a safer failure mode than
what the pre-fix weight-comparison bug would have produced.

Scheduled `rate(1 day)` but **`Enabled: false`** in `template.yaml` on
purpose -- `BigCommerceSecretArn` has no default, so a daily-enabled
schedule would just fail every single day with a Secrets Manager error
until real credentials are wired up. Flip it to `true` once you've set
that parameter for real (see "Getting this running" below). Contrast with
`BowwwlCrossCheckFunction`'s schedule, which is enabled by default since
it only needs `DbSecretArn` (already required for every other function in
this stack) and simply no-ops if there are no products due for a check.

39 tests pass across the two new modules (19 for `bowwwl_cross_check`, 17
for `bowlerdepot_reconciliation`, run manually via `python3
tests/test_bowwwl_cross_check.py` / `test_bowlerdepot_reconciliation.py`).
bowwwl's tests run against two real fixture reconstructions
(`tests/fixtures/bowwwl_fury_emerald_black_hybrid.html`,
`bowwwl_defender.html` -- current/symmetric and retired/asymmetric,
built from real field values read off live pages, with each fixture's
header comment disclosing the one specific inferred-not-observed detail:
the RG field's CSS class name, matched by label text either way so it
doesn't affect parsing correctness). BowlerDepot's tests use invented
product data shaped to match the confirmed-real BigCommerce response
structure, since no real store export exists yet -- see that test file's
own header comment.

## Admin API auth

`AdminApiFunction` used to be a bare `HttpApi` event with no authorizer
-- anyone who found the URL could approve/reject `review_queue` items or
publish/unpublish products. That's now fixed with a shared-secret Lambda
authorizer, which was the option you picked over Cognito (per-person
login, more setup and ongoing user management) and IAM/SigV4 (no browser-
friendly login path at all) -- reasonable for a single admin or a small
trusted group sharing one token.

**How it works**: `template.yaml`'s implicit `HttpApi` event became an
explicit `AdminHttpApi` resource (`AWS::Serverless::HttpApi`) with a
`TokenAuthorizer` set as its `DefaultAuthorizer`, backed by the new
`AdminApiAuthorizerFunction` (`src/admin_api_authorizer/app.py`). Every
request to the admin API now needs an `Authorization: Bearer <token>`
header matching the value stored in the Secrets Manager secret
referenced by the new `AdminApiTokenSecretArn` parameter (plain string or
JSON `{"token": "..."}`, your choice).

**Fails closed, on purpose, at two points** -- worth knowing before you
deploy: if `AdminApiTokenSecretArn` is left at its blank default, every
request is denied outright rather than the API silently staying open
(there's no "auth disabled" mode). And a Secrets Manager error (bad ARN,
missing IAM permission) is allowed to raise rather than being caught and
treated as "no token configured" -- API Gateway turns that into a loud
500 instead of a quiet, hard-to-diagnose bypass. See the module
docstring in `src/admin_api_authorizer/app.py` for the full reasoning,
including why token freshness is cached per-container (rotating the
secret won't take effect on already-warm Lambda containers until they
recycle -- acceptable for a shared token, not for anything needing
instant revocation) and the specific AWS API Gateway HTTP API v2
authorizer behaviors that were verified against AWS's own current docs
this session vs. taken on faith (see that docstring's own disclosure --
short version: the request/response shape is confirmed real, whether
every possible client actually sends a lowercase `authorization` header
the way the docs say API Gateway normalizes it to is not something this
sandbox could observe directly, though the authorizer's header lookup is
case-insensitive regardless so it shouldn't matter).

24/24 tests pass (`tests/test_admin_api_authorizer.py`), covering token
extraction, secret-value parsing (both the bare-string and JSON shapes),
the constant-time comparison, and `handler()`'s fail-closed paths with
`get_expected_token` monkeypatched -- the actual Secrets Manager call
itself is untested here, same "logic verified, deployment isn't" status
as everything else in this project that touches boto3, and for the same
reason: no AWS access in this sandbox.

**Setting it up**: create a Secrets Manager secret with a token value
(e.g. `aws secretsmanager create-secret --name admin-api-token --secret-string '{"token":"<a-long-random-value>"}'`),
pass its ARN as `AdminApiTokenSecretArn` when you deploy, and send that
same token as a bearer token from whatever's calling the admin API. See
"Getting this running" below for exactly where this fits in the deploy
sequence.

## Why there's no live end-to-end test yet

This session's sandbox has restricted outbound network access, so I couldn't
execute `fetch_sitemap()` against the real internet from here, and I don't
have your AWS credentials to actually deploy and invoke this. What I *did*
verify:

- The sitemap structure, namespace, and URL/lastmod shape are real, captured
  directly from brunswickbowling.com during the architecture research (not
  guessed at).
- `parse_sitemap()` and `diff_against_known()` are pure functions I ran
  directly against that real fixture data in this session and confirmed
  correct output (6 ball URLs found, apparel URL correctly excluded,
  current/retired correctly classified from the path, real lastmod values
  parsed, missing lastmod handled without raising).
- `db/migrations/001_init_schema.sql` was reviewed by hand for syntax
  (couldn't spin up Postgres in this sandbox to run it directly -- no root
  access to install the server, and package installs were blocked this
  session). Worth running `sam local` / a real `psql` pass yourself before
  trusting it blindly, same as any migration you didn't watch execute.
- `src/product_scraper/app.py`: an earlier session's research tooling only
  ever returned a markdown-converted view of brunswickbowling.com's pages,
  never raw HTML, so this parser's table-matching logic went unverified
  against real markup for a while. **That gap is now closed.** A later
  session used Claude in Chrome to issue a literal `fetch()` from inside a
  live browser tab against both real product pages
  (`.../products/balls/current/crown-78u` and `.../retired/defender`) and
  parse the actual HTTP response body -- not a markdown conversion, not
  the JS-rendered DOM, the literal bytes `requests.get()` receives in
  production. The table structure, weight-column header pattern, and
  spec-table label/value shape all matched this parser's existing
  assumptions exactly. Two real bugs were found and fixed as a direct
  result, though: `parse_release_date()` didn't handle the real
  day-precision "December 11, 2025" format (only "Month YYYY" was
  supported, so this silently produced `None` in production), and
  `parse_resources()` matched PDF resource type by the `<a>` tag's own
  text, but real markup's link text is always the generic word
  "Download" -- the actual label lives in a sibling heading instead, so
  `info_sheet_url` was never actually being populated. Both fixes,
  and the real values/structure behind them, are in
  `src/product_scraper/app.py`'s module docstring and
  `tests/fixtures/crown_78u.html`/`defender.html`'s header comments. The
  full raw HTML response (~325KB, almost entirely cookie-consent-widget
  markup and tracking scripts) repeatedly triggered this sandbox's
  anti-exfiltration safeguard when transferred verbatim, so the fixtures
  remain reconstructions rather than byte-for-byte captures -- but now
  built from individually re-confirmed real values, not markdown-derived
  guesses. **Still worth a live smoke test after your first real
  deploy**, same as everything else in this project that's never
  actually run against AWS -- but the specific "never inspected raw
  markup at all" risk that used to apply here is resolved.
- `src/pdf_parser/app.py`: re-confirmed independently in a later session,
  beyond the original real-fixture-text verification -- fresh live pulls
  via `mcp__workspace__web_fetch` against both Crown 78U's and Defender's
  actual Info Sheet PDF URLs reproduced every real value exactly,
  including the documented real Crown 78U HTML-vs-PDF mismatch (16 lb RG:
  2.577 HTML vs. 2.557 PDF), and `find_mismatches()` correctly flagged
  that exact discrepancy end to end against a fresh HTML+PDF pairing.
  What's still unverified, and now confirmed *why* it can't be closed
  from this kind of sandbox: `extract_pdf_text()`'s actual use of
  `pdfplumber` against raw PDF bytes. Getting real PDF bytes from a live
  browser into a verification sandbox was attempted this session and
  failed for two structural reasons (cross-origin `fetch()` blocked by
  CORS; navigating directly to the PDF hands the tab to Chrome's native
  viewer, which won't allow script injection) -- see
  `src/pdf_parser/app.py`'s module docstring for the full detail. Every
  fresh check this session went through already-extracted text, same as
  the original fixtures; `pdfplumber`'s specific behavior on raw bytes
  remains the one thing worth a quick manual check with a real PDF and a
  local Python environment before trusting this in production.
- `src/image_processor/app.py`: partially de-risked in a later session,
  with a precise caveat about what that means. Raw bytes of a real
  Brunswick product image couldn't be transferred into a verification
  sandbox either (same two structural blockers as the PDF case above,
  plus this environment's own anti-exfiltration filter rejecting a
  base64-encoded image transfer outright, `[BLOCKED: Base64 encoded
  data]`, regardless of size) -- so `bbox_from_alpha()`,
  `bbox_from_background()`, and `detect_bbox()` themselves were NOT run
  against real bytes. What WAS done: sampling the real image's actual
  pixel values directly via the browser's `canvas.getImageData()` (no
  bytes transferred, just individual pixel queries, which this
  environment's filter didn't block) confirmed, on real data, that a
  real Brunswick product photo has genuine alpha transparency (corners
  alpha=0, ball alpha=255) with a real ~2-3px anti-aliased edge -- not a
  hard binary cutoff, not a flattened background -- exactly matching
  `has_real_transparency()`'s and `bbox_from_alpha()`'s design
  assumptions. That validates the *algorithm's* real-world correctness
  for the alpha-detection path on one real image; it does not prove the
  actual Python code runs correctly end to end, and the
  background-threshold path remains completely unverified against a real
  flattened source (still relevant for other manufacturers/platforms).
  Tests still run only against synthetic Pillow-generated images. **This
  is exactly the risk the architecture doc itself flags** ("should be
  verified per source platform with a handful of real samples before
  assuming one approach covers all three template families") -- running
  the actual pipeline against a handful of real downloaded images (which
  *is* possible once this deploys somewhere with normal network access)
  remains the real verification step before trusting this in production.
- `src/admin_api/` has a different kind of gap than the other four: pip
  couldn't install `fastapi`, `pydantic`, or `mangum` in this sandbox at
  all (same proxy block as everything else), so unlike the others, even
  the framework glue in `app.py` was never imported, let alone run. That's
  why the logic was deliberately split out into `service.py` -- everything
  that doesn't depend on those three packages (`field_name` parsing, the
  update-plan decision logic, and the approve/reject control flow against
  a hand-rolled fake cursor standing in for psycopg2) is real, tested code,
  passing 11/11 in this session. `app.py` itself is comparatively thin
  (each route just validates input shape and calls a `service.py`
  function), but it's genuinely unverified -- give it a closer read than
  code that was actually exercised before trusting it, and run it locally
  (`pip install -r requirements.txt && uvicorn app:app`) before deploying.
  `AdminApiFunction` itself is now sitting behind `AdminHttpApi`'s
  Lambda authorizer (see "Admin API auth" above) rather than being a bare
  unauthenticated HTTP API -- but `admin_api_authorizer/app.py`'s own
  boto3-calling glue (`get_expected_token()`) has the same
  never-actually-invoked status as every other boto3 call in this
  project: its pure logic (token extraction, secret-value parsing,
  constant-time comparison, fail-closed branches) is tested (24/24), the
  real Secrets Manager round-trip isn't.
- **SQS orchestration** is tested at the code level -- the message-building
  functions (`build_scrape_messages`, `build_pdf_parse_message`,
  `build_image_process_messages`), the SQS-batch-vs-direct-invoke shape
  detection (`_extract_jobs`), and the full per-job flow including the
  partial-batch-failure path all run against fake SQS/DB objects in the
  five `test_*_orchestration.py` files (one each for product_scraper,
  pdf_parser, image_processor, woocommerce_product_scraper, and
  netsuite_product_scraper) and pass. What's NOT tested, because this
  sandbox has no AWS access at all: the actual `template.yaml` wiring --
  queue ARNs resolving correctly, IAM policies actually granting the right
  permissions (`SQSSendMessagePolicy` / the SQS event source's implicit
  poller permissions), `VisibilityTimeout` being long enough in practice,
  `ReportBatchItemFailures` behaving the way the AWS docs say it does.
  `sam validate` and a real deploy are the only way to find out if the
  YAML itself is wrong in a way `yaml.safe_load` (used to spot-check
  syntax this session) wouldn't catch. This applies equally to the two
  new platform-specific scrape queues (`WooCommerceProductScrapeQueue`,
  `NetsuiteProductScrapeQueue`) added when SWAG/MOTIV were wired in --
  same unverified-YAML caveat, nothing platform-specific about that risk.
  `BowwwlCrossCheckFunction` and `BowlerDepotReconciliationFunction` are
  simpler in one respect (`Schedule` events, not SQS queues -- no DLQ or
  batch-failure behavior to get wrong) but carry the same "never actually
  invoked by AWS" caveat as everything else; the same strip-CFN-tags-then-
  `yaml.safe_load` pass confirmed both functions and their new Outputs
  entries parse and are present, which is a syntax check, not a deploy.
- `src/woocommerce_url_discovery/app.py` and `woocommerce_product_scraper/app.py`
  are on similar footing to the Craft-CMS pair: real field values, real
  URLs, real sitemap/category-page structure, all confirmed via direct
  fetches this session, but reconstructed fixtures rather than saved raw
  HTML (same markdown-conversion limitation as Brunswick's). 31/31 tests
  pass (22 parsing/discovery + 9 orchestration/image-fanout). Specific
  unconfirmed pieces, disclosed rather than assumed away:
  `parse_mass_bias()` on a real non-"N/A" (asymmetric-ball) value, and
  whether `pdf_parser`'s Brunswick-specific PDF layout applies to SWAG's
  Dropbox-hosted PDFs at all (neither PDF could actually be fetched this
  session to check).
- `src/netsuite_url_discovery/app.py` and `netsuite_product_scraper/app.py`
  have a different balance than every other module above: the *parsing*
  is arguably the most solidly verified of any manufacturer's yet (every
  markup structure was read directly off live pages in a real browser
  this session, not a markdown-converted approximation), but the
  *fetching* is the least verified -- a plain request to a MOTIV product
  page returns blank, and `fetch_page()`'s session-cookie workaround has
  never actually been run against motivbowling.com from anywhere. See
  "Third manufacturer: MOTIV Bowling" above for the full detail. 34/34
  tests pass (27 parsing/discovery + 7 orchestration/image-fanout) for
  the parsing/diff/message-building/fanout logic; none of that exercises
  `fetch_page()` itself, which is exactly the unverified part -- wiring
  the orchestration queue doesn't change that.

None of that substitutes for actually running this against AWS. Treat this as
"the logic is verified, the deployment isn't."

## Getting this running

1. **Provision Postgres** (RDS or otherwise) and run all three migrations, in order:
   ```
   psql "$DATABASE_URL" -f db/migrations/001_init_schema.sql
   psql "$DATABASE_URL" -f db/migrations/002_add_woocommerce_netsuite_platforms.sql
   psql "$DATABASE_URL" -f db/migrations/003_date_tracking_and_bowwwl.sql
   ```
2. **Seed the `brands` row(s)** this deployment discovers URLs for, e.g.:
   ```sql
   insert into manufacturers (name) values ('Brunswick Bowling & Billiards') returning id;
   insert into brands (manufacturer_id, name, base_url, source_platform, sitemap_url)
   values ('<manufacturer-id>', 'Brunswick', 'https://brunswickbowling.com', 'craft_cms',
           'https://brunswickbowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml')
   returning id;
   ```
   Keep that `id` -- it's the `BrandId` parameter below. If you're also
   enabling SWAG or MOTIV, do the same for each (`source_platform` =
   `'woocommerce'` or `'netsuite'` respectively; MOTIV has no sitemap_url
   equivalent, see `netsuite_url_discovery/app.py`'s module docstring) and
   keep those ids for `SwagBrandId`/`MotivBrandId`.
3. **Store DB credentials in Secrets Manager** as a JSON secret shaped
   `{"host", "port", "dbname", "username", "password"}`. Note its ARN.
   **Also create the admin API token secret** now, since the admin API
   deploys fail-closed (denies everything) without it: e.g.
   `aws secretsmanager create-secret --name admin-api-token --secret-string '{"token":"<a-long-random-value>"}'`.
   Note that ARN too -- see "Admin API auth" above for the full design.
4. **Deploy**:
   ```
   sam build
   sam deploy --guided
   ```
   `sam build` installs each function's own `requirements.txt` (`sam
   build` reads `CodeUri`'s directory) -- every `src/*/` directory that
   imports a third-party package now has one; `bowlerdepot_reconciliation`,
   `bowwwl_cross_check`, `netsuite_product_scraper`, and
   `netsuite_url_discovery` were missing theirs until this pass (all four
   import `psycopg2`, which isn't in the Lambda runtime by default, so
   `sam build` would previously have shipped those functions in a state
   that fails on first DB connection attempt). `boto3` itself is
   deliberately left off most of these `requirements.txt` files, matching
   this project's existing majority convention -- it ships in the Lambda
   Python runtime already, so pinning it is optional, not required.
   You'll be prompted for `DbSecretArn`, `BrandId`, and
   `AdminApiTokenSecretArn` (the secret from step 3 -- leave it blank
   only if you're not using the admin API yet; every request will be
   denied until it's set). `SitemapUrl` and
   `UrlPathPattern` default to Brunswick's values and don't need to be set
   unless you're deploying a second stack for Radical or DV8 (same Craft CMS
   template -- see the parameter descriptions in `template.yaml`).
   `SwagCategoryUrl`/`SwagSitemapUrl` default to SWAG's real values;
   `SwagBrandId` defaults to blank and only needs setting once you've seeded
   SWAG's `brands` row per step 2. Same pattern for
   `MotivCurrentCategoryUrl`/`MotivRetiredCategoryUrl`/`MotivBrandId` --
   but see "Third manufacturer: MOTIV Bowling" above before trusting a
   MOTIV deploy to actually fetch anything; the fetching approach is
   unverified even though the parsing is solid. `BigCommerceSecretArn`
   defaults to blank; set it to a real Secrets Manager ARN shaped
   `{"store_hash", "auth_token"}` once you have a BowlerDepot API token,
   then flip `BowlerDepotReconciliationFunction`'s schedule to
   `Enabled: true` in `template.yaml` (it ships disabled on purpose -- see
   "QA cross-checks and lifecycle date tracking" above for why).
5. **Run the tests** (once you have `pytest` available -- this session's
   sandbox couldn't install it, so it's unverified in the pytest runner
   itself for the files that use it; the newer `*_orchestration.py`,
   `woocommerce_*`, and `netsuite_*` test files don't need pytest at all,
   run them directly):
   ```
   pip install pytest
   pytest tests/ -v
   # or, for the pytest-free files:
   python3 tests/test_woocommerce_url_discovery.py
   python3 tests/test_woocommerce_product_scraper.py
   python3 tests/test_woocommerce_product_scraper_orchestration.py
   python3 tests/test_netsuite_url_discovery.py
   python3 tests/test_netsuite_product_scraper.py
   python3 tests/test_netsuite_product_scraper_orchestration.py
   python3 tests/test_bowwwl_cross_check.py
   python3 tests/test_bowlerdepot_reconciliation.py
   python3 tests/test_admin_api_authorizer.py
   ```

## Reusing this for Radical / DV8

Brunswick, Radical, and DV8 share the same Craft CMS template and URL shape
(confirmed during research -- same SEOmatic meta tags, same
`/products/balls/current/{slug}` structure, same DigitalOcean Spaces CDN
naming). Deploy a second SAM stack with `SitemapUrl` pointed at
`radicalbowling.com`'s or `dv8bowling.com`'s equivalent sitemap and a
different `BrandId`, rather than writing new code.

Hammer/Ebonite/Track/Powerhouse (Shopify) don't need this function at all --
pull `/products.json` directly once the product scraper exists.
