# Bowling ball scraper/database

Serverless (AWS SAM) pipeline that scrapes bowling ball manufacturer sites into
a normalized Postgres database, feeding a consumer informational site and
reconciliation against bowlerdepot.com's BigCommerce catalog. Full design
rationale, the schema, and the decisions behind it live in
`brunswick-scraper-architecture-review.md` (one level up from this repo) --
read that first if anything here seems under-explained.

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
- `src/woocommerce_url_discovery/app.py` + `woocommerce_product_scraper/app.py`
  -- second manufacturer, second platform family: SWAG Bowling on
  WordPress/WooCommerce. See "Second manufacturer: SWAG Bowling" below for
  the real platform differences that shaped these.
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
now has a first pass built, and they're wired together end to end. What's
left: auth on the admin API, the bowwwl.com and BowlerDepot cross-checks
that are supposed to also write into `review_queue` (currently only the
PDF-vs-HTML check does), and the consumer-facing site itself.

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

Not yet done for SWAG, disclosed rather than guessed at: these two
functions aren't wired into the SQS orchestration chain
(`ProductScrapeQueue`/etc.) the Craft-CMS family uses -- they're
invoke-manually only, same starting point the Craft-CMS functions had
before that chain was built. The Info Sheet/Shelf Talker PDFs SWAG links
are hosted on Dropbox share links, and neither could actually be fetched
this session (the tool available returned nothing for both) -- so whether
`pdf_parser`'s Brunswick-specific layout assumptions apply to SWAG's PDFs
at all is unknown, though also not currently load-bearing since SWAG's
HTML already carries the core RG/DIFF/mass-bias data. And
`parse_mass_bias()`'s handling of a real (non-"N/A") value is unverified --
the one real product page inspected this session was a symmetric core;
check a real asymmetric SWAG ball's page before trusting mass bias data
for those.

## Third manufacturer (research done, not yet built): MOTIV Bowling

MOTIV Bowling (motivbowling.com) was investigated this session but not yet
scraped. It runs on what looks like NetSuite SuiteCommerce (identifiable
by the `/n_<id>` permalink pattern, which redirects to a canonical slug
URL). Real, confirmed findings, using the Claude in Chrome browser tool
(not just the non-browser fetch tool the rest of this research used):

- The real product-spec content (RG/DIFF per weight, core, coverstock,
  finish) IS present as static text in the server-delivered HTML -- not
  injected by JavaScript after page load. Confirmed by fetching the raw
  page via `fetch()` from inside an actual browser session and finding
  the real values (a ball's exact RG numbers, core name, coverstock name)
  present in the raw response text itself. No separate API call for this
  data was visible in the page's network log either.
- BUT: fetching that exact same product URL from this session's
  non-browser fetch tool -- the same style of plain HTTP request a Lambda
  scraper would make -- came back completely empty, while MOTIV's
  homepage fetched fine through that same tool. Product pages specifically
  are rejecting or short-circuiting non-browser requests somehow.
- Checked the cookies MOTIV's site actually sets: only ordinary analytics/
  marketing trackers (Google Analytics, AdRoll, Bing UET) plus one short
  session cookie (`s`, typical of NetSuite) -- no Cloudflare/Akamai/
  PerimeterX cookies, i.e. no sign of enterprise bot-management.

Net conclusion: a headless browser is likely NOT required just to render
MOTIV's content, but a plain `requests.get()` almost certainly won't work
as-is either. The most promising unbuilt approach: a `requests.Session()`
that visits the homepage first (to pick up the `s` session cookie), reuses
that cookie on the product-page request, and sends realistic browser-style
headers. This is a reasoned bet based on real evidence (the cookie
inventory, the absence of bot-management signatures), not a confirmed
working approach -- this sandbox has no path to actually test an outbound
`requests` call against motivbowling.com to prove it out. Build and test
this against the real site (or report back if it doesn't work) before
assuming it's solved.

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
- `src/product_scraper/app.py` has the same "logic verified, deployment
  isn't" status, plus one more specific caveat worth knowing: the research
  tooling used to study brunswickbowling.com this session only ever
  returned a markdown-converted view of pages, never raw HTML. So the two
  HTML test fixtures (`tests/fixtures/crown_78u.html`, `defender.html`) are
  reconstructions using real, verified field values -- not literal saved
  copies of the site's actual markup. The parser is built to match table
  rows and fields by visible text content rather than any specific
  CSS class or id, specifically because that real markup was never
  inspected -- but that also means it hasn't been proven against it either.
  **Run it against a real live page before trusting it in production** --
  if Brunswick's actual table structure differs meaningfully from the
  fixtures (e.g. nested tables, a table that doesn't put the row label in
  the first cell), the content-matching heuristics may need adjusting.
- `src/pdf_parser/app.py` is on firmer footing than the HTML scraper on the
  "is this real" question: `mcp__workspace__web_fetch` turned out to be
  able to fetch and extract text from PDF URLs directly, so both fixture
  files are verbatim real extracted text from actual Brunswick Info Sheet
  PDFs, not reconstructions. `parse_info_sheet()`, `parse_weight_table()`,
  and `find_mismatches()` all ran against that real text in this session
  and passed (7/7). What's still unverified: `fetch_pdf()` (the actual
  network call) and `extract_pdf_text()`'s use of `pdfplumber` against raw
  PDF bytes -- I only ever had the already-extracted text, never the PDF
  bytes themselves, so pdfplumber's extraction step specifically hasn't
  been exercised. If its output format differs from what
  `mcp__workspace__web_fetch` returned (e.g. different whitespace/line
  handling), `parse_weight_table()`'s line-prefix matching may need minor
  adjustment -- worth a quick manual check against one real PDF before
  trusting this in production.
- `src/image_processor/app.py` is the weakest-verified of the scraping/
  processing functions, honestly: this sandbox never had network access to Brunswick's
  image CDN at all (same outbound allowlist block as everything else), so
  there was no real product photo to test against, not even as extracted
  text the way the PDFs worked. `bbox_from_alpha()`, `bbox_from_background()`,
  `detect_bbox()`, and `normalize_composition()` are all tested against
  synthetic images generated with Pillow itself (a circle with exactly-known
  pixel geometry, on both a transparent and a flattened background) and the
  math checks out precisely against that known geometry. What that proves:
  the cropping/scaling/centering logic is correct. What it doesn't prove:
  that Brunswick's (or any other manufacturer's) real photos actually match
  either the "clean alpha cutout" or "uniform background" pattern the
  detection logic assumes -- real product photography sometimes has soft
  shadows, reflections, or gradient backgrounds that would confuse a hard
  alpha/color threshold. **This is exactly the risk the architecture doc
  itself flags** ("should be verified per source platform with a handful of
  real samples before assuming one approach covers all three template
  families") -- download a handful of real images per source and eyeball
  the bbox detection before trusting this in production.
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
  The `AdminApiFunction` also has no authorizer wired up in `template.yaml`
  on purpose -- that's a real security decision for you to make, not a
  default worth guessing at.
- **SQS orchestration** is tested at the code level -- the message-building
  functions (`build_scrape_messages`, `build_pdf_parse_message`,
  `build_image_process_messages`), the SQS-batch-vs-direct-invoke shape
  detection (`_extract_jobs`), and the full per-job flow including the
  partial-batch-failure path all run against fake SQS/DB objects in the
  three `test_*_orchestration.py` files and pass. What's NOT tested,
  because this sandbox has no AWS access at all: the actual
  `template.yaml` wiring -- queue ARNs resolving correctly, IAM policies
  actually granting the right permissions (`SQSSendMessagePolicy` /
  the SQS event source's implicit poller permissions), `VisibilityTimeout`
  being long enough in practice, `ReportBatchItemFailures` behaving the way
  the AWS docs say it does. `sam validate` and a real deploy are the only
  way to find out if the YAML itself is wrong in a way `yaml.safe_load`
  (used to spot-check syntax this session) wouldn't catch.
- `src/woocommerce_url_discovery/app.py` and `woocommerce_product_scraper/app.py`
  are on similar footing to the Craft-CMS pair: real field values, real
  URLs, real sitemap/category-page structure, all confirmed via direct
  fetches this session, but reconstructed fixtures rather than saved raw
  HTML (same markdown-conversion limitation as Brunswick's). 22/22 tests
  pass. Specific unconfirmed pieces, disclosed rather than assumed away:
  `parse_mass_bias()` on a real non-"N/A" (asymmetric-ball) value, and
  whether `pdf_parser`'s Brunswick-specific PDF layout applies to SWAG's
  Dropbox-hosted PDFs at all (neither PDF could actually be fetched this
  session to check).

None of that substitutes for actually running this against AWS. Treat this as
"the logic is verified, the deployment isn't."

## Getting this running

1. **Provision Postgres** (RDS or otherwise) and run both migrations, in order:
   ```
   psql "$DATABASE_URL" -f db/migrations/001_init_schema.sql
   psql "$DATABASE_URL" -f db/migrations/002_add_woocommerce_netsuite_platforms.sql
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
   enabling SWAG, do the same for it (`source_platform` = `'woocommerce'`,
   `sitemap_url` = the product sitemap) and keep that id for `SwagBrandId`.
3. **Store DB credentials in Secrets Manager** as a JSON secret shaped
   `{"host", "port", "dbname", "username", "password"}`. Note its ARN.
4. **Deploy**:
   ```
   sam build
   sam deploy --guided
   ```
   You'll be prompted for `DbSecretArn` and `BrandId`; `SitemapUrl` and
   `UrlPathPattern` default to Brunswick's values and don't need to be set
   unless you're deploying a second stack for Radical or DV8 (same Craft CMS
   template -- see the parameter descriptions in `template.yaml`).
   `SwagCategoryUrl`/`SwagSitemapUrl` default to SWAG's real values;
   `SwagBrandId` defaults to blank and only needs setting once you've seeded
   SWAG's `brands` row per step 2.
5. **Run the tests** (once you have `pytest` available -- this session's
   sandbox couldn't install it, so it's unverified in the pytest runner
   itself for the files that use it; the newer `*_orchestration.py` and
   `woocommerce_*` test files don't need pytest at all, run them directly):
   ```
   pip install pytest
   pytest tests/ -v
   # or, for the pytest-free files:
   python3 tests/test_woocommerce_url_discovery.py
   python3 tests/test_woocommerce_product_scraper.py
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
