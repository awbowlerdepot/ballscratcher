# Bowling ball scraper/database

Serverless (AWS SAM) pipeline that scrapes bowling ball manufacturer sites into
a normalized Postgres database, feeding a consumer informational site and
reconciliation against bowlerdepot.com's BigCommerce catalog. Full design
rationale, the schema, and the decisions behind it live in
`brunswick-scraper-architecture-review.md` (one level up from this repo) --
read that first if anything here seems under-explained.

## What's actually built right now

- `template.yaml` -- SAM template with two functions: `UrlDiscoveryFunction`
  (scheduled, end to end) and `ProductScraperFunction` (built and tested,
  not yet wired to a trigger -- see the comment in `template.yaml`).
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
- `tests/` -- unit tests for both functions above, run against **real**
  captured data (a real sitemap sample, and real field values from two
  actual product pages covering the two different page patterns found
  during research). See the comment blocks in each fixture file under
  `tests/fixtures/` for exactly what's real vs. reconstructed and why.

Everything else in the architecture doc's build order (PDF mass-bias parser,
image mirroring/centering pipeline, FastAPI admin API) is intentionally not
stubbed out yet -- see the commented-out section at the bottom of
`template.yaml` for what's next and why it's not speculative boilerplate
today.

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
