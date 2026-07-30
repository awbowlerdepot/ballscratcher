# Bowling ball scraper/database

Serverless (AWS SAM) pipeline that scrapes bowling ball manufacturer sites into
a normalized Postgres database, feeding a consumer informational site and
reconciliation against bowlerdepot.com's BigCommerce catalog. Full design
rationale, the schema, and the decisions behind it live in
`brunswick-scraper-architecture-review.md` (one level up from this repo) --
read that first if anything here seems under-explained.

## What's actually built right now

- `template.yaml` -- SAM template with all five functions from the
  architecture doc's build order: `UrlDiscoveryFunction` (scheduled),
  `ProductScraperFunction`, `PdfParserFunction`, `ImageProcessorFunction`
  (invoked manually pending an orchestration decision -- see each
  function's comment), and `AdminApiFunction` (a real HTTP API trigger --
  **no auth wired up**, see its comment in `template.yaml` before deploying
  this anywhere reachable). Also defines the public-read `ImageBucket` the
  image pipeline uploads normalized product photos to.
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
- `tests/` -- unit tests for all five functions above, run against **real**
  captured data where real data exists: a real sitemap sample, real field
  values from two actual product pages, and two real PDF Info Sheets
  fetched directly from Brunswick's CDN (verbatim extracted text, not
  reconstructions). The image pipeline and admin API are exceptions -- see
  "Why there's no live end-to-end test yet" below for why their tests run
  against synthetic images / a hand-rolled fake DB cursor instead. All
  tests across all five functions pass when run manually in-sandbox (see
  below for why "manually" rather than via `pytest` itself).

Every function from the architecture doc's original 5-function build order
now has a first pass built. What's left: orchestration wiring (SQS/Step
Functions to actually chain UrlDiscovery -> ProductScraper -> PdfParser ->
ImageProcessor instead of manual invocation), auth on the admin API, the
bowwwl.com and BowlerDepot cross-checks that are supposed to also write
into `review_queue` (currently only the PDF-vs-HTML check does), and the
consumer-facing site itself.

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

None of that substitutes for actually running this against AWS. Treat this as
"the logic is verified, the deployment isn't."

## Getting this running

1. **Provision Postgres** (RDS or otherwise) and run the migration:
   ```
   psql "$DATABASE_URL" -f db/migrations/001_init_schema.sql
   ```
2. **Seed the `brands` row** this deployment discovers URLs for, e.g.:
   ```sql
   insert into manufacturers (name) values ('Brunswick Bowling & Billiards') returning id;
   insert into brands (manufacturer_id, name, base_url, source_platform, sitemap_url)
   values ('<manufacturer-id>', 'Brunswick', 'https://brunswickbowling.com', 'craft_cms',
           'https://brunswickbowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml')
   returning id;
   ```
   Keep that `id` -- it's the `BrandId` parameter below.
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
5. **Run the tests** (once you have `pytest` available -- this session's
   sandbox couldn't install it, so it's unverified in the pytest runner
   itself, only via the manual equivalent described above):
   ```
   pip install pytest
   pytest tests/ -v
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
