# Deploy runbook

Concrete, ordered steps to get this stack running for real, with exact
commands. This complements README.md rather than replacing it -- README
explains *what* was built and *why*, and discloses what's verified vs.
still unproven; this doc is just the *how*, plus a smoke-test order
designed to catch the highest-risk unknowns first rather than deploying
everything at once and hoping.

Nothing in this repo can deploy itself: none of the sandbox that built it
has AWS credentials. Every step below runs from your own machine (or CI)
with your own AWS account.

## 0. Prerequisites

- AWS CLI v2, configured with credentials that can create RDS/Lambda/SQS/
  Secrets Manager/API Gateway resources (`aws sts get-caller-identity` to
  confirm you're pointed at the right account).
- AWS SAM CLI (`sam --version`).
- `psql` (or any Postgres client) able to reach whatever instance you
  provision in step 1.
- Python 3.13 locally isn't required -- `sam build` uses a Lambda-
  compatible build image via Docker when `--use-container` is set, which
  matters if your local Python version differs (this repo's own
  `samconfig.toml` sets `use_container = true` under
  `[default.build.parameters]`, so plain `sam build` already does this
  without needing the flag typed out each time). Have Docker running
  either way, simplest path.

## 1. Provision Postgres

Any Postgres 13+ instance works (RDS is the obvious choice, but this
repo doesn't assume it). Note the connection details -- you'll need them
for step 3's `DbSecretArn` secret and to run migrations directly.

## 2. Run the ten migrations, in order

```bash
psql "$DATABASE_URL" -f db/migrations/001_init_schema.sql
psql "$DATABASE_URL" -f db/migrations/002_add_woocommerce_netsuite_platforms.sql
psql "$DATABASE_URL" -f db/migrations/003_date_tracking_and_bowwwl.sql
psql "$DATABASE_URL" -f db/migrations/004_product_videos.sql
psql "$DATABASE_URL" -f db/migrations/005_products_last_video_discovery_at.sql
psql "$DATABASE_URL" -f db/migrations/006_products_video_reviews_summary.sql
psql "$DATABASE_URL" -f db/migrations/007_cores_table.sql
psql "$DATABASE_URL" -f db/migrations/008_coverstocks_table.sql
psql "$DATABASE_URL" -f db/migrations/009_normalize_coverstock_names.sql
psql "$DATABASE_URL" -f db/migrations/010_product_images_ordering_thumbnail_visibility.sql
```

(If you already ran an earlier subset in a prior deploy, just run whatever
you're missing against the same database -- each migration is additive,
adding columns/tables only.)

These were reviewed by hand for syntax but never executed against a real
Postgres instance (no server available in the sandbox that wrote them) --
this is genuinely the first real test of them. Run in a throwaway/staging
database first if you want a safety margin before pointing at anything
that matters.

## 3. Create the Secrets Manager secrets

Three possible secrets. The first two are needed for a functioning
deploy; the third only matters once you're ready to enable BowlerDepot
reconciliation.

**DB credentials (required):**

```bash
aws secretsmanager create-secret \
  --name bowling-scraper-db \
  --secret-string '{"host":"<your-rds-endpoint>","port":5432,"dbname":"<dbname>","username":"<user>","password":"<password>"}'
```

Note the returned ARN -- this is `DbSecretArn`.

**Admin API token (required if you want the admin API reachable --
without it, every request is denied by design, see README's "Admin API
auth" section):**

```bash
TOKEN=$(openssl rand -hex 32)
aws secretsmanager create-secret \
  --name bowling-scraper-admin-token \
  --secret-string "{\"token\":\"$TOKEN\"}"
echo "Save this token somewhere safe, you'll send it as a bearer token: $TOKEN"
```

Note the returned ARN -- this is `AdminApiTokenSecretArn`. Save `$TOKEN`
itself too (not just the ARN) -- you'll need it in step 6's smoke tests
and for any real client calling the admin API later.

**BigCommerce credentials (optional, only for BowlerDepot reconciliation
-- skip this until you actually have a BowlerDepot store API token; the
function that needs it ships with its schedule disabled by default for
exactly this reason):**

```bash
aws secretsmanager create-secret \
  --name bowling-scraper-bigcommerce \
  --secret-string '{"store_hash":"<store-hash>","auth_token":"<api-token>"}'
```

Note the returned ARN -- this is `BigCommerceSecretArn`, only needed once
you're ready to flip `BowlerDepotReconciliationFunction`'s schedule on
(step 7).

**YouTube Data API v3 key (optional, only for the video-enrichment feature
-- skip until you're ready to try it):**

This one you have to get yourself (Google Cloud console -> APIs & Services
-> Credentials -> Create API key, then enable the "YouTube Data API v3" on
that project). There's no way for this project to obtain it for you.

```bash
aws secretsmanager create-secret \
  --name bowling-scraper-youtube-api-key \
  --secret-string '{"api_key":"<your-youtube-api-key>"}'
```

Note the returned ARN -- this is `YouTubeApiKeySecretArn`. Remember the
real, hard quota this key is subject to: this project's search.list quota
is CONFIRMED (checked directly in Google Cloud console, not just inferred
from unit math) at exactly 100 searches/day, not adjustable from this
template. `MAX_SEARCHES_PER_INVOCATION` defaults to 70 (not 90) to leave
same-day headroom for retries -- see src/video_discovery/app.py's module
docstring, REAL INCIDENT #3, for the full story of how this was confirmed.

Separately, `video_summarizer` calls Bedrock, and this needs one real,
confirmed fact accounted for: Claude Haiku 4.5 (the `BedrockModelId`
default) has **no in-Region (on-demand) support in `us-west-1`** -- only
Geographic and Global cross-Region inference are available there,
confirmed via Bedrock's own model-card "Regional availability" table (each
model's page under Bedrock's [models at a
glance](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html)
console/docs). That's why `BedrockModelId` defaults to
`global.anthropic.claude-haiku-4-5-20251001-v1:0` (an inference profile ID)
rather than the bare model ID, and why `template.yaml` has a second
`BedrockBaseModelId` parameter and a 3-statement IAM policy on
`VideoSummarizerFunction` instead of one -- see that parameter's
description in `template.yaml` for the full reasoning and AWS's own
documented policy shape this follows. Practically, this means:

1. AWS's old "Model access" console page is retired -- serverless models
   auto-enable on first invoke now, but Anthropic models still need a
   one-time per-account use-case form. Submit it via the [Model
   catalog](https://console.aws.amazon.com/bedrock/home#/model-catalog),
   which will prompt for it the first time you open an Anthropic model.
2. Confirm access with a direct CLI check rather than guessing from the
   console UI:
   ```bash
   aws bedrock get-foundation-model-availability \
     --model-id anthropic.claude-haiku-4-5-20251001-v1:0 \
     --region us-west-1
   ```
   Look for `"agreementAvailability": {"status": "AVAILABLE"}` and
   `"authorizationStatus": "AUTHORIZED"`.
3. If you ever change `BedrockModelId` to a different model, check that
   model's own Regional availability table first -- don't assume it has
   in-Region support just because Haiku 4.5 didn't. If it does have
   in-Region support in your stack's Region, you can simplify back to a
   bare model ID and collapse the 3-statement IAM policy back to one
   (update `BedrockBaseModelId` accordingly either way, they must stay in
   sync).

## 4. Seed the `brands` rows

Only Brunswick is required for a minimal working deploy.

```sql
insert into manufacturers (name) values ('Brunswick Bowling & Billiards') returning id;
insert into brands (manufacturer_id, name, base_url, source_platform, sitemap_url)
values ('<manufacturer-id>', 'Brunswick', 'https://brunswickbowling.com', 'craft_cms',
        'https://brunswickbowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml')
returning id;
```

Save that returned id -- it's `BrandId` in step 5.

If you also want Radical and/or DV8 enabled -- same manufacturer, same
Craft CMS platform as Brunswick, confirmed live this session (identical
SEOmatic sitemap shape and product-page structure):

```sql
insert into manufacturers (name) values ('Brunswick Bowling & Billiards') returning id;
-- (skip the insert above and reuse Brunswick's manufacturer_id if you already have it)

insert into brands (manufacturer_id, name, base_url, source_platform, sitemap_url)
values ('<manufacturer-id>', 'Radical', 'https://radicalbowling.com', 'craft_cms',
        'https://radicalbowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml')
returning id;

insert into brands (manufacturer_id, name, base_url, source_platform, sitemap_url)
values ('<manufacturer-id>', 'DV8', 'https://dv8bowling.com', 'craft_cms',
        'https://dv8bowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml')
returning id;
```

Save those ids as `RadicalBrandId`/`Dv8BrandId`. Both are new
`RadicalUrlDiscoveryFunction`/`Dv8UrlDiscoveryFunction` resources in this
same stack, reusing `src/url_discovery/`'s existing code -- they publish
onto the same `ProductScrapeQueue` that `ProductScraperFunction` already
consumes, so no new scraper function was needed (that function already
takes `{url, brand_id}` as generic pass-through parameters). Can also be
added later via a stack update, same as SWAG/MOTIV below.

If you also want SWAG and/or MOTIV enabled at deploy time, repeat with
`source_platform = 'woocommerce'` (SWAG) or `'netsuite'` (MOTIV; no
`sitemap_url` for MOTIV, see `netsuite_url_discovery/app.py`'s module
docstring), and save those ids as `SwagBrandId`/`MotivBrandId`. Both can
also be added later via a stack update -- nothing about the initial
deploy locks you out of enabling them afterward.

If you also want Storm/Roto Grip/900 Global enabled, they're three
separate `brands` rows (one commercebuild site, three brands, same
one-manufacturer/multiple-brands shape as Brunswick/Radical/DV8) with
`source_platform = 'commercebuild'` and no `sitemap_url` (there's no
per-brand sitemap -- `CommercebuildCategoryUrl` in step 5 covers all
three via its facet filter):

```sql
insert into manufacturers (name) values ('Storm Products, Inc.') returning id;
insert into brands (manufacturer_id, name, base_url, source_platform)
values ('<manufacturer-id>', 'Storm', 'https://www.stormbowling.com', 'commercebuild')
returning id;
insert into brands (manufacturer_id, name, base_url, source_platform)
values ('<manufacturer-id>', 'Roto Grip', 'https://www.stormbowling.com', 'commercebuild')
returning id;
insert into brands (manufacturer_id, name, base_url, source_platform)
values ('<manufacturer-id>', '900 Global', 'https://www.stormbowling.com', 'commercebuild')
returning id;
```

Save those three ids as `StormBrandId`/`RotoGripBrandId`/`Global900BrandId`.

If you also want Hammer enabled, it's a fourth platform
(`source_platform = 'shopify'`) -- no `sitemap_url` (discovery is
collection-JSON-based, not sitemap-based, see
`src/shopify_url_discovery/app.py`'s module docstring):

```sql
insert into manufacturers (name) values ('Brunswick Bowling Products, LLC') returning id;
-- (skip if you already have a manufacturer row covering the Brunswick/
-- Radical/DV8/Hammer corporate family -- confirmed live this session that
-- Hammer's footer cross-links dv8bowling.com/ebonite.com/hammerbowling.com/
-- radicalbowling.com/trackbowling.com/powerhousebowling.com, same family
-- as Brunswick's own brands. "Brunswick Bowling Products, LLC" per a web
-- search after this session: Brunswick's bowling *equipment* line
-- (bowling balls, pinsetters, etc., owned by BlueArc Capital Management
-- since 2015) is a completely separate business from Brunswick
-- *Billiards*, which Escalade Sports acquired in 2022 -- an earlier draft
-- of this doc wrongly named Escalade Sports here, conflating the two.
-- Hammer itself came into this family via Brunswick's 2019 acquisition of
-- Ebonite International, Hammer's prior owner since 2002.)
insert into brands (manufacturer_id, name, base_url, source_platform)
values ('<manufacturer-id>', 'Hammer', 'https://hammerbowling.com', 'shopify')
returning id;
```

Save that id as `HammerBrandId`. Track and Ebonite share this same
Shopify platform -- confirmed live (trackbowling.com/ebonite.com:
collections.json, a real separate retired-balls collection, working
/products/{handle}.json), and both are wired up in `template.yaml` as
`TrackUrlDiscoveryFunction`/`EboniteUrlDiscoveryFunction`:

```sql
insert into brands (manufacturer_id, name, base_url, source_platform)
values ('<manufacturer-id>', 'Track', 'https://trackbowling.com', 'shopify')
returning id;
insert into brands (manufacturer_id, name, base_url, source_platform)
values ('<manufacturer-id>', 'Ebonite', 'https://ebonite.com', 'shopify')
returning id;
```

Save those ids as `TrackBrandId`/`EboniteBrandId`. Both reuse the same
manufacturer_id as Hammer/Brunswick -- Track and Ebonite are also part of
the Brunswick-owned equipment family (see the note above this block).

One real gotcha worth knowing before you enable either: Track's and
Ebonite's product pages use a completely different BALL SPECS/RG-DIFF
markup than Hammer's -- an HTML `<table>` instead of Hammer's `<ul><li>`
list (confirmed live against both sites; see
`src/shopify_product_scraper/app.py`'s module docstring and
`parse_ball_specs_table`/`parse_rg_diff_table`'s docstrings for the full
details). This was caught and handled before either brand was wired up
here, not after -- `ShopifyProductScraperFunction` (shared by all three
brands) now dispatches to whichever parser matches the section it
actually finds, so no separate per-brand scraper was needed.

## 5. Deploy

```bash
cd brunswick-scraper
sam build
sam deploy --guided
```

`sam build` installs each function's own `requirements.txt` -- every
function directory that needs a third-party package has one as of this
repo's current state.

`sam deploy --guided` will prompt for every parameter in `template.yaml`.
Here's what to give it:

| Parameter | Required? | What to pass |
|---|---|---|
| `DbSecretArn` | Yes | ARN from step 3 |
| `BrandId` | Yes | Brunswick's id from step 4 |
| `AdminApiTokenSecretArn` | Yes* | ARN from step 3 (*technically optional, but the admin API is unusable without it -- see README) |
| `SitemapUrl` | No | Defaults to Brunswick's real sitemap |
| `UrlPathPattern` | No | Defaults to Brunswick's real path pattern; shared by RadicalUrlDiscoveryFunction/Dv8UrlDiscoveryFunction too (same URL shape confirmed live) |
| `RadicalSitemapUrl` / `Dv8SitemapUrl` | No | Default to Radical's/DV8's real sitemaps (confirmed live this session) |
| `RadicalBrandId` / `Dv8BrandId` | Only if enabling Radical/DV8 | Their ids from step 4, else leave blank (blank means that brand's discovery function runs against BRAND_ID="" -- harmless since it isn't scheduled yet, but don't invoke it manually until set for real) |
| `SwagCategoryUrl` / `SwagSitemapUrl` | No | Default to SWAG's real values |
| `SwagBrandId` | Only if enabling SWAG | SWAG's id from step 4, else leave blank |
| `MotivCurrentCategoryUrl` / `MotivRetiredCategoryUrl` | No | Default to MOTIV's real values |
| `MotivBrandId` | Only if enabling MOTIV | MOTIV's id from step 4, else leave blank |
| `CommercebuildCategoryUrl` | No | Defaults to the real stormbowling.com bowling-balls category URL |
| `StormBrandId` / `RotoGripBrandId` / `Global900BrandId` | Only if enabling commercebuild | The three ids from step 4, else leave blank (a blank id makes `CommercebuildUrlDiscoveryFunction` skip that brand entirely, see its module docstring -- you can enable them individually, not all-or-nothing) |
| `HammerStoreDomain` / `HammerCollectionHandles` | No | Default to Hammer's real domain/collection handles (confirmed live) |
| `HammerBrandId` | Only if enabling Hammer | Hammer's id from step 4, else leave blank |
| `TrackStoreDomain` / `TrackCollectionHandles` | No | Default to Track's real domain/collection handles (confirmed live this session -- note the set differs from Hammer's, no lower-mid-performance tier) |
| `TrackBrandId` | Only if enabling Track | Track's id from step 4, else leave blank |
| `EboniteStoreDomain` / `EboniteCollectionHandles` | No | Default to Ebonite's real domain/collection handles (confirmed live this session -- differs from both Hammer and Track, has a pro-performance tier but no upper-mid-performance) |
| `EboniteBrandId` | Only if enabling Ebonite | Ebonite's id from step 4, else leave blank |
| `BigCommerceSecretArn` | No | Leave blank until step 7's BowlerDepot rollout |

Accept the SAM CLI's other prompts (stack name, region, confirm changes,
allow IAM role creation) as appropriate for your environment. Once it
finishes, note the `AdminApiUrl` output -- you'll need it for the smoke
tests below.

## 6. Post-deploy smoke tests, in de-risked order

Don't test everything at once. Go in this order so a failure tells you
something specific, starting with what's most likely to already work and
ending with what's most likely to need a second look.

### 6a. Admin API auth (lowest risk, pure logic, 24 tests already pass)

```bash
API_URL=$(aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='AdminApiUrl'].OutputValue" --output text)

# Should be 401/403 -- no token
curl -i "${API_URL}health"

# Should be 200
curl -i -H "Authorization: Bearer $TOKEN" "${API_URL}health"
```

If the unauthenticated request isn't rejected, stop and check
`AdminApiTokenSecretArn` actually resolved to a real secret (an empty
string still fails closed per the authorizer's design, so this would
point at something more structurally wrong -- check
`AdminApiAuthorizerFunction`'s CloudWatch logs first).

### 6b. Brunswick pipeline (most-verified scraper this session)

`UrlDiscoveryFunction` runs on its own daily schedule, but don't wait a
day -- invoke it directly:

```bash
aws lambda invoke --function-name bowling-scraper-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
```

Then check it actually queued work:

```bash
QUEUE_URL=$(aws sqs get-queue-url --queue-name bowling-scraper-product-scrape --query QueueUrl --output text)
aws sqs get-queue-attributes --queue-url "$QUEUE_URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

`ProductScraperFunction` will pick those up automatically (SQS-triggered).
Watch its logs for the first real run:

```bash
aws logs tail /aws/lambda/bowling-scraper-product-scraper --follow
```

This is the piece with the most direct evidence behind it -- this
session verified its parsing logic against two real live pages' actual
HTTP responses (see README's "Why there's no live end-to-end test yet" ->
product_scraper entry) and fixed two real bugs as a result. If this
still fails, check the DLQ first:

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name bowling-scraper-product-scrape-dlq --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages
```

### 6b.5. Radical / DV8 (if `RadicalBrandId`/`Dv8BrandId` were set)

Same platform as Brunswick, so this reuses `ProductScraperFunction` and
`ProductScrapeQueue` as-is -- only the discovery step is brand-specific.
No schedule wired up yet (same reasoning as SWAG/MOTIV below: a schedule
against a blank `BrandId` isn't useful), so invoke manually:

```bash
aws lambda invoke --function-name bowling-scraper-radical-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json

aws lambda invoke --function-name bowling-scraper-dv8-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
```

Both publish onto the same `bowling-scraper-product-scrape` queue 6b
checks, so watch `ProductScraperFunction`'s logs the same way. Confirmed
live this session: both domains' sitemaps
(`https://radicalbowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml`,
`https://dv8bowling.com/sitemaps-1-section-bowlerProducts-1-sitemap.xml`)
return real SEOmatic-generated XML with the identical
`/products/balls/(current|retired)/<slug>` URL shape as Brunswick's own
sitemap, including non-ball URLs (accessories/bags/apparel) mixed in that
`UrlPathPattern` correctly filters out -- see `tests/fixtures/
radical_sitemap_sample.xml` / `dv8_sitemap_sample.xml` and the new tests
in `tests/test_url_discovery.py`. Not yet confirmed: the product pages
themselves parsing correctly through `product_scraper` (only the sitemap
step was verified live this session) -- treat the first real invoke as
that test, same as 6b's own first run was.

### 6c. PDF parser and image processor (chained off 6b, unverified against real bytes/photos)

These fire automatically once 6b produces a product with an
`info_sheet_url` and images. Watch their logs the same way:

```bash
aws logs tail /aws/lambda/bowling-scraper-pdf-parser --follow
aws logs tail /aws/lambda/bowling-scraper-image-processor --follow
```

`pdf_parser`'s `extract_pdf_text()` (pdfplumber against raw PDF bytes)
and `image_processor`'s bbox-detection logic were both only tested
against synthetic data in the sandbox that built them -- this is their
actual first real test. If image processing looks wrong, download a
couple of the mirrored images from `ImageBucket` and eyeball them before
assuming the bug is elsewhere.

**Real bug found and fixed (retired/older balls' Info Sheets):**
`pdf_parser` originally assumed every Info Sheet PDF used the modern
layout (a bare `RG `/`DIFF `/`ASY ` line, one final number per weight).
Older sheets -- confirmed live against Mastermind Strategy's actual PDF,
a retired asymmetric-core ball -- use a 5-row layout instead (`RG MAX`/
`RG INT`/`RG Min`/`RG Diff`/`RG ASY`, exposing the raw measurements
rather than collapsing them), and ALL-CAPS field labels (`PART NUMBER`
vs modern `Part Number`). Both silently broke: every one of those five
rows starts with `RG `, so the old code kept overwriting `rg_values`
with whichever row came last (with the sub-label word itself parsed as
a bogus number, shifting everything by one position), while
`diff`/`mass_bias` stayed `None` entirely since this layout has no bare
`DIFF `/`ASY ` line at all; and case-sensitive field-label matching
missed every field since the labels were the wrong case. Fixed in both
`parse_weight_table()` (prefers the `RG Min`/`RG Diff`/`RG ASY` rows
when present -- `RG Min` is what's publicly reported as "RG", confirmed
against this exact ball's own spec table) and `parse_fields()`
(case-insensitive label matching). See `tests/fixtures/
mastermind_strategy_info_sheet.txt` for the real captured text and
`tests/test_pdf_parser.py`'s three new tests for the exact before/after
values. No migration or backfill needed -- this only affects parsing of
PDFs not yet successfully synced; `sync_pdf_skus`'s existing
insert/coalesce/review_queue logic (see that function's docstring)
handles re-running `pdf_parser` against an already-partially-synced
product the same as any other re-scrape.

**Core name now captured (migration 007):** Al noticed core name was parsed
by every one of the four product scrapers but silently dropped -- nothing
ever wrote it anywhere. Multiple named products can share one physical
core (his example: DV8's Collision core, used by six differently-named
balls), so this needed a real many-products-to-one-core relationship
rather than a repeated free-text column. Rather than add a new table, this
repurposes `ball_families` (migration 001 already had exactly this shape --
brand-scoped name + core_name + core_type -- but it was never wired into
any scraper, so `family_id` was null on every products row): renamed to
`cores`, `products.family_id` renamed to `core_id`. Each scraper now has a
`get_or_create_core_id()` that upserts on `(brand_id, name)`, so repeated
scrapes of different products sharing a core resolve to the same row
instead of duplicating it. `core_type` (symmetric/asymmetric) is only
actually populated by `commercebuild_product_scraper` today (the only
platform with a dedicated "Symmetry" field) -- the other three pass `None`
until/unless that gets parsed too.

**This only takes effect on the next scrape of each product -- there is now
a backfill for that** (there wasn't when this was first written; added
right after). `core_id` only gets set the next time `upsert_product` runs
for that URL, and nothing else re-triggers a scrape for an
already-scraped, unchanged product on its own. Three ways to trigger it:

1. `POST /products/{id}/rescrape` -- republishes that one product's
   `{url, brand_id}` onto whichever platform's scrape queue it belongs to
   (see `service.queue_rescrape`/`resolve_scrape_queue_env_var`, keyed by
   `source_platform`, not brand -- so this works for Hammer/Track/Ebonite
   alike as long as `brands.source_platform = 'shopify'` for that row, see
   6f.5/6f.6). Returns `{"queued": true, ...}` on success, or
   `{"queued": false, "reason": ...}` (not an error) for a product on a
   platform with no scraper deployed at all yet, or a misconfigured queue
   env var.
2. `scripts/backfill_core_ids.py` -- same `ADMIN_API_URL`/`ADMIN_API_TOKEN`
   env var setup as `scripts/backfill_video_review_rollups.py` (see 6i).
   Paginates `GET /products?missing_core=true` and calls the rescrape
   endpoint for each. Only enqueues -- doesn't wait for the actual scrape,
   so re-run it later (or just re-check the count) to see what's left.
   ```bash
   export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
   export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
   python3 scripts/backfill_core_ids.py
   ```
3. `admin-site/index.html`'s Products tab has a "missing core" filter
   checkbox and a Core column, plus a per-product "Rescrape" button in the
   detail view; the Batch Jobs tab has a "Backfill missing core info"
   panel that does the same list-then-loop as the script, in the browser.

**Real incident: 503s partway through a whole-catalog `backfill_core_ids.py`
run.** Running it against every brand at once (not just Hammer -- the
first time this had ever been run catalog-wide) started throwing bare
`503 Service Unavailable` from `ADMIN_API_URL` partway through, with
**nothing** in `bowling-scraper-admin-api`'s own CloudWatch log group for
the failed requests -- meaning the Lambda invocation never even started.
Confirmed root cause via `aws lambda get-account-settings` (
`UnreservedConcurrentExecutions: 10` -- AWS's default low tier for a
new/unverified account, not the usual 1000) and CloudWatch's `Throttles`
metric on `AdminApiFunction` (nonzero during the run): every rescrape call
this function issues publishes onto one of five different platforms'
scrape queues, each with its own SQS-triggered Lambda that fires
immediately -- a catalog-wide backfill can have all five running
concurrently, saturating the account's tiny 10-slot pool and leaving
nothing for `AdminApiFunction`'s own (single-threaded) invocation to grab.
HTTP API v2 surfaces that specific Lambda-service throttle as a plain 503,
which is why nothing showed up in the app-level logs -- the request never
reached application code.

Two mitigations were attempted; only one is actually in effect:

1. `scripts/backfill_core_ids.py`'s `list_products_missing_core`/
   `rescrape_product` now retry on 429/500/502/503/504 with exponential
   backoff (`get_requests_session()`, 5 attempts, 1s/2s/4s/8s/16s) rather
   than counting a transient throttle as a hard failure needing a manual
   re-run. This is a client-side/script-level change, not a deploy --
   already in effect.
2. `AdminApiFunction` setting `ReservedConcurrentExecutions: 2` in
   `template.yaml` was tried and **reverted after a real, confirmed deploy
   failure**: `"Specified ReservedConcurrentExecutions for function
   decreases account's UnreservedConcurrentExecution below its minimum
   value of [10]."` AWS enforces a hard floor of 10 concurrent executions
   that must always remain in the account's *unreserved* pool -- since
   this account's entire limit IS 10, there's nothing above that floor to
   carve a reservation out of. Reserving even 1 slot for any function is
   rejected outright until the account's total limit is raised above 10.
   The stack rolled back cleanly (`UPDATE_ROLLBACK_COMPLETE`) -- nothing
   was left broken, this mitigation just isn't available yet.

**Retries alone are a workaround for the underlying scarcity, not a fix
for it.** Request a Lambda concurrency quota increase for this account
via AWS Service Quotas (service code `lambda`, quota "Concurrent
executions") before running a catalog-wide backfill like this again --
once the account limit is meaningfully above 10, revisit adding
`ReservedConcurrentExecutions` back to `AdminApiFunction`.

**Cores tab (the "other direction" view):** Everything above (the Products
tab's Core column, the missing-core filter/backfill) shows core info
one product at a time -- the many-products-to-one-core relationship
migration 007 exists for (Al's example: DV8's Collision core, used by six
differently-named balls) was otherwise only noticeable by spotting the
same core name repeated across several Products-tab rows, one page load
at a time. Added `GET /cores` (paginated, `brand_id`/`search` filters,
one row per core with a `product_count` rolled up via a left join +
`count`/`group by`, ordered by `product_count desc` so the actually-
shared cores surface first) and `GET /cores/{id}` (that core's row plus
the full list of products currently pointing at it -- id/name/url/status/
published/updated_at, enough to link straight into the Products tab's own
detail view for any one of them). No `template.yaml` change needed for
either -- `AdminApiFunction`'s routes are a `/{proxy+}` catch-all per HTTP
method (see the CORS-preflight comment on that function's `Events` block),
so a new path under an already-wired method just works.

`admin-site/index.html` gets a new Cores tab: Brand/Core Name/Type/
Products-count/Created table, same filter+pager shape as every other tab,
with a "Products" detail-row button per core (like Products tab's
"Detail" and Video Candidates tab's "Detail") that lists every product
using that core via `GET /cores/{id}`. A core showing 0 products is
flagged directly in the UI as "likely an orphaned row" rather than just
an empty table -- exactly the shape the Hammer `"E "`-prefix incident
above left behind in production (219 correctly-scraped products, plus a
batch of now-unreferenced corrupted `cores` rows) before they were
manually cleaned up via direct SQL; this tab is what would have made that
visible without needing to already know to go looking for it.

This required new plumbing on `AdminApiFunction`: `PRODUCT_SCRAPE_QUEUE_URL`
/ `WOOCOMMERCE_PRODUCT_SCRAPE_QUEUE_URL` / `NETSUITE_PRODUCT_SCRAPE_QUEUE_URL`
/ `COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL` env vars and matching
`SQSSendMessagePolicy` grants, so the admin API can publish onto any of the
four platforms' scrape queues rather than just the two it already talked to
(`VideoSummarizeQueue`/`VideoTranscriptResultQueue`). No new scraper-side
code was needed -- every one of the four scrapers already accepts a direct
`{"url", "brand_id"}` invocation (see each `*_product_scraper/app.py`'s
`_extract_jobs`), this just republishes onto the queue that feeds it.

`admin_api`'s `PRODUCT_UPDATABLE_FIELDS` used to list `core_name` as a
directly-editable products column, which was never actually true (only
`ball_families`/now-`cores` ever had that column) -- a latent bug that
would have 500'd if a review_queue row had ever carried
`field_name="core_name"`. Confirmed via grep that nothing ever wrote one
(`bowwwl_cross_check` explicitly excludes core from its comparable
fields), so nothing was actually broken by this in practice. Removed the
dead entry; `get_product()` now left-joins `cores` so the admin API
returns `core_name`/`core_type` for the detail view, and `admin-site/
index.html`'s product detail panel shows it.

**Coverstock normalized too (migration 008), same shape as cores.** Al's
direct follow-up ask: "can we do the same thing we did for cores for
covers, those are also shared across many balls" -- confirmed the shared
field would be `coverstock_name`, products' existing free-text marketing
name column (e.g. MOTIV's "Atomic Propulsion Pearl Reactive", Brunswick's
"HK22 - Savvy Hook Hybrid"). Same real shape as cores: one named
coverstock formulation, scoped to a brand, reused across many
differently-named products.

**One real difference from the cores rollout, worth calling out because
it changes the deploy story:** `ball_families`/`cores` existed in the
schema from day one but was never wired into any scraper, so `core_id`
needed a live rescrape of every product to backfill (see above --
`scripts/backfill_core_ids.py`, the whole `POST /rescrape` mechanism,
the concurrency-throttle incident). `coverstock_name`/`coverstock_material`/
`coverstock_type`, by contrast, have been real, populated columns on
every `products` row since `001_init_schema.sql`, written by every one
of the five scrapers (`product_scraper`, `commercebuild_product_scraper`,
`woocommerce_product_scraper`, `netsuite_product_scraper`,
`shopify_product_scraper`) on every scrape. That means migration 008
backfills `coverstock_id` for every already-scraped product **in the
migration itself**, from data that already exists -- one `insert ...
select distinct` plus one `update ... from`, no rescrape, no backfill
script, no admin_api endpoint needed for the one-time catch-up. There is
deliberately no "Backfill missing coverstock info" Batch Jobs panel to
match the cores one -- there's nothing to backfill that the migration
didn't already handle.

`products.coverstock_name`/`coverstock_material`/`coverstock_type` are
left in place, unchanged, still written by every scraper on every scrape
-- they're real per-product data other code already depends on directly
(`bowlerdepot_reconciliation`'s field mapping, `pdf_parser`,
`bowwwl_cross_check`), not just a foreign key's shadow. `coverstock_id`
is additive: each scraper's `upsert_product` now also calls a duplicated
`get_or_create_coverstock_id()` (same "own the whole package" convention
as `get_or_create_core_id`, and the same coalesce-never-overwrite
handling for `material`/`type`), and `list_products` now also selects
`p.coverstock_id`/`p.coverstock_name` and accepts a `missing_coverstock`
filter -- ongoing data-quality visibility for the (expected to be rare)
case of a page that genuinely never exposed a parseable coverstock, not
a backlog waiting on anything.

**Coverstocks tab**, exact copy of the Cores tab's shape one migration
later: `GET /coverstocks` (paginated, `brand_id`/`search`, one row per
coverstock with a `product_count`, ordered `product_count desc` so
heavily-reused coverstocks surface first) and `GET /coverstocks/{id}`
(that coverstock's row plus every product currently pointing at it).
Same "no `template.yaml` change needed" reasoning as `/cores` -- rides
`AdminApiFunction`'s existing `/{proxy+}` catch-all (confirmed via the
CFN-tolerant YAML parser: still 45 resources). `admin-site/index.html`
gets a new Coverstocks tab (Brand/Coverstock Name/Material/Type/
Products-count/Created, "Products" detail-row button per coverstock via
`GET /coverstocks/{id}`), and the Products tab gains a Coverstock column
and a "missing coverstock" filter checkbox alongside the existing Core
column/"missing core" checkbox.

**Real duplicate-data bug found immediately after 008, fixed in migration
009:** Al reported the coverstocks table already had real duplicates --
manufacturer pages add a trademark/registered/copyright symbol to a
coverstock name sometimes but not always for the exact same coverstock
(e.g. "R2S Solid Reactive" on one product's page, "R2S™ Solid Reactive"
on another), and 008's exact-text `unique (brand_id, name)` constraint
let both spellings in as separate rows -- same intent, different text.

Two-part fix, same "raw data on the product, validated/normalized on the
shared record" split Al specifically asked to keep (see the discussion
in this session): each scraper's `get_or_create_coverstock_id` now runs
the lookup/create key through a new `_normalize_coverstock_name()`
(strips ™/®/©, collapses whitespace) before touching the `coverstocks`
table -- duplicated per scraper, same convention as every other helper
in this project. `products.coverstock_name` is completely untouched by
this -- it keeps storing exactly what the page said, TM symbol or not.

**Migration 009** (`db/migrations/009_normalize_coverstock_names.sql`)
is the one-time catch-up for whatever migration 008's backfill (or any
scrape before this fix shipped) already created from the raw,
un-normalized text: merges rows that collide once normalized -- repoints
every `products.coverstock_id` from a merged-away row onto the survivor,
backfills `material`/`type` onto the survivor from any referencing
product that has a real value it's missing, deletes the merged-away
rows, then renames the sole survivor to its normalized form. Written to
be safe to run whether or not 008 has actually been applied yet on a
given database, and safe to run more than once (every step is a no-op on
already-clean data) -- see the migration's own header comment for the
full reasoning.

**Two real incidents, both found by actually running this against the
real database** (nothing in this sandbox can execute SQL against a live
Postgres, so this migration's syntax was reviewed by hand until Al ran
it for real -- see step 2's own caveat about that):

1. The first version normalized every name FIRST, then merged whatever
   collided -- and hit `duplicate key value violates unique constraint
   "coverstocks_brand_id_name_key" ... Key (brand_id, name)=(...,
   Activator Plus) already exists` immediately, confirming real
   TM-suffix duplicates existed. Root cause: Postgres checks a plain
   (non-deferred) unique constraint per row as it's written, not once at
   the end of the statement -- a single `UPDATE` that tries to rename
   both "Activator Plus™" and the already-existing "Activator Plus" onto
   the same text trips the constraint the moment the second row is
   written, well before any merge/delete step runs. Fixed by reordering:
   merge and delete duplicates FIRST (using each row's still-distinct
   original name -- deleting a row can never violate a unique
   constraint, no matter what it's named), and only rename the sole
   remaining survivor per group afterward, once nothing else in that
   group is left to collide with.
2. The very next run then hit `function min(uuid) does not exist` --
   `coverstocks.id` is `uuid`, and Postgres's built-in `MIN`/`MAX`
   aggregates are only defined for a specific set of types (numeric,
   string, date/time, a few others); `uuid` isn't one of them, even
   though it fully supports ordering/comparison via its normal btree
   operator class. Fixed by picking the survivor with
   `first_value(id) over (partition by ... order by id)` instead of
   `min(id) over (partition by ...)` -- `first_value` only needs an
   `ORDER BY` (comparison), not the `MIN` aggregate specifically, so it
   works on any orderable type including `uuid`.

```bash
psql "$DATABASE_URL" -f db/migrations/009_normalize_coverstock_names.sql
```

Requires the same Lambda redeploy as the rest of this section (the
`_normalize_coverstock_name` fix lives in scraper code, not just the
migration) before new scrapes stop recreating the duplicate going
forward.

**`scripts/backfill_coverstock_ids.py`**, exact mirror of
`backfill_core_ids.py`: real bug Al found (2026-08-07) against a live
Hammer product, `Raw Hammer - Black / Grey` -- missing its own
`coverstock_id`, and the `coverstocks` row it should map to ("Juiced
Solid") had a null `material`. Investigation confirmed today's live page
parses cleanly end-to-end (`coverstock_name="Juiced Solid"`,
`material=reactive_resin`, `type=solid` via `parse_coverstock`) -- no
scraper bug. The product's own `updated_at` from Shopify was today's
date, meaning the page was edited recently and this product simply
hasn't been rescraped since (nothing else re-triggers a scrape for an
already-known product on its own). Unlike migration 008's own backfill
(which covered every product that already had `coverstock_name` data at
migration time), a product missing `coverstock_id` *now* needs an actual
rescrape to fix -- there's no free SQL-only backfill for data that was
never captured in the first place. `scripts/backfill_coverstock_ids.py`
queues one via `GET /products?missing_coverstock=true` +
`POST /products/{id}/rescrape`, same shape as the cores version. A
rescrape fixes both halves of Al's report in one pass: the product's own
`coverstock_id` gets set, and -- for free, via
`get_or_create_coverstock_id`'s existing
`coalesce(coverstocks.material, excluded.material)` -- the shared
`coverstocks` row's `material`/`type` gets backfilled too if it was
missing. Same "Backfill missing coverstock info" panel added to the
Batch Jobs tab as the cores version (`missing_coverstock` filter param,
same queued/reason result shape).

```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/backfill_coverstock_ids.py
```

**Stale-image DELETE + S3 orphan cleanup ported from MOTIV (6e.6/6e.7).**
Al: "brunswick needs an image cleanup like motiv did." Confirmed
`product_scraper/app.py` (Brunswick) had the exact same gap
`netsuite_product_scraper` (MOTIV) had before 6e.6: `upsert_product`'s
image upsert (`INSERT ... ON CONFLICT (product_id, source_url) DO
UPDATE`) only ever inserts a row for a `source_url` still present on the
page or updates one that already matches -- it never deletes a row for a
photo the current parse no longer found. A rescrape after a page's photos
changed (or after any future scraper fix that stops collecting a wrong
photo, same shape as 6e.7's `core_callout` removal) would just add
whatever the current parse found alongside whatever stale rows were
already sitting there.

Ported both halves of MOTIV's fix (see 6e.6 for the full original
incident writeup) exactly:

1. `upsert_product` now deletes any `product_images` row for the product
   whose `source_url` isn't in the current parse's set (or, if the parse
   found zero images this time, every existing row), returning the
   deleted `(id, stored_url)` pairs so a rescrape genuinely REPLACES the
   image list instead of just extending it.
2. `delete_orphaned_image_objects()` (same function, ported verbatim --
   own key-listing/delete logic, not shared/imported, matching this
   project's "each Lambda owns its whole package" convention) removes the
   matching `product-images/<id>/*` S3 objects for any deleted row that
   had already been mirrored by `image_processor`, called from
   `_process_one`. `ProductScraperFunction` gained the same `IMAGE_BUCKET`
   env var and scoped `s3:ListBucket`/`s3:DeleteObject` policy
   `NetsuiteProductScraperFunction` has -- soft-fails (logs a warning,
   skips cleanup) if `IMAGE_BUCKET` isn't set on a given deployment, same
   optional-config convention as `IMAGE_PROCESS_QUEUE_URL`; the DB-side
   delete always works regardless.

Also fixed, as a side effect of actually running
`tests/test_product_scraper_orchestration.py` end to end to verify this:
that file's `FakeCursor` was missing `insert into cores`/`insert into
coverstocks` support, a pre-existing gap (present since the cores/
coverstocks features shipped, not caused by this change) that made every
DB-touching test in that file raise `NotImplementedError` before ever
reaching the query it meant to exercise. Fixed by porting the same
`FakeCursor` branches `test_netsuite_product_scraper_orchestration.py`
already has. `test_woocommerce_product_scraper_orchestration.py` has the
identical gap and is still unfixed -- out of scope for this change, left
as a known pre-existing issue same as before.

**Requires a Lambda redeploy** (`sam build && sam deploy`) --
`ProductScraperFunction` code and `template.yaml` both changed. No
migration needed -- this only changes what a future rescrape does, not
any existing data.

**`scripts/rescrape_brunswick_products.py`**, exact mirror of 6e.6's
`scripts/rescrape_netsuite_products.py`, added right after so a
catalog-wide sweep is actually available rather than one-product-at-a-
time via the admin UI. Scoped by `GET /products?source_platform=craft_cms`
rather than a Brunswick-specific filter -- `craft_cms` already covers
Brunswick/Radical/DV8 together, since all three share one
`ProductScraperFunction`/`ProductScrapeQueue` (see
`service.py`'s `SCRAPE_QUEUE_ENV_VAR_BY_PLATFORM` comment). Only enqueues
rescrapes; watch `ProductScraperFunction`'s logs/DLQ while it drains,
then spot-check a few product detail pages in the admin UI.

```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/rescrape_brunswick_products.py
```

**Image ordering/thumbnail/visibility (migration 010).** Al, looking
ahead: "once we actually have a customer facing site we will want to
order the images, set a thumbnail image and control visibility." Not
platform-specific -- applies to `product_images` regardless of which
scraper populated a row, so it's documented here (image handling, same
neighborhood as `image_processor` above) rather than under any one
brand's section.

Three additive columns: `display_order` (int, backfilled to each
product's existing/insertion order so nothing visibly reshuffles the
moment this migration runs), `is_thumbnail` (bool, backfilled true on one
`main`-typed image per product where one exists -- `main` is already the
highest-signal `image_type` every scraper produces -- so every existing
product starts with a sensible thumbnail rather than requiring a manual
catalog-wide pass; enforced to at most one true per `product_id` via a
partial unique index), `is_visible` (bool, defaults true -- nothing
about current behavior changes until an admin explicitly hides a row).
None of the five scrapers touch any of these three fields -- purely an
admin-curated layer on top of the raw scraped data, same "raw vs.
curated" split as `coverstock_name` vs. `coverstocks.name` (008/009).
Distinct from the existing `products.published` flag/`PATCH
/products/{id}/published` endpoint: that controls whether the whole
PRODUCT is visible to the consumer site; `is_visible` here controls
whether one specific IMAGE is shown once the product itself is visible.

`admin_api` gained two endpoints, both scoped to `(product_id, image_id)`
so a caller can never mutate a different product's image by passing a
mismatched pair:

- `PATCH /products/{id}/images/{image_id}` -- body `{"is_visible": bool}`
  and/or `{"is_thumbnail": bool}`, either or both. Setting
  `is_thumbnail: true` is handled as an atomic "unset every other image
  on this product, then set this one" operation (two `UPDATE`s in one
  transaction) rather than a bare column write, since the partial unique
  index would reject a second `true` row otherwise.
- `POST /products/{id}/images/reorder` -- body `{"image_ids": [...]}`,
  rewrites `display_order` to match each id's position in the list.
  Whole-list resubmit rather than incremental swap endpoints, so two
  concurrent partial edits can't race each other -- the admin-site "Up"/
  "Down" buttons just reorder the just-loaded array client-side and
  resend the full list.

`admin-site`'s product detail image grid gained per-image "Up"/"Down"/
"Set thumbnail"/"Hide"/"Show" controls (image cards changed from `<a>` to
`<div>` to avoid nesting buttons inside a link -- the thumbnail image
itself is still wrapped in its own `<a>` for "view full size"), a
"thumbnail" badge, and dimmed styling for a hidden image.
`service.get_product`'s image query now orders by `display_order` so the
grid renders in the admin-curated order.

No `template.yaml` change needed -- both new routes ride
`AdminApiFunction`'s existing `/{proxy+}` catch-all (confirmed via the
CFN-tolerant YAML parser: still 45 resources).

```bash
psql "$DATABASE_URL" -f db/migrations/010_product_images_ordering_thumbnail_visibility.sql
```

### 6d. SWAG (if `SwagBrandId` was set)

No schedule wired up for `WooCommerceUrlDiscoveryFunction` yet -- invoke
manually:

```bash
aws lambda invoke --function-name bowling-scraper-woocommerce-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
aws logs tail /aws/lambda/bowling-scraper-woocommerce-product-scraper --follow
```

Confirmed real category-page/attribute-table structure this session
(see README's "Second manufacturer" section) -- expect this to work, but
it's never actually run against AWS before.

### 6e. MOTIV (if `MotivBrandId` was set) -- CONFIRMED working

```bash
aws lambda invoke --function-name bowling-scraper-netsuite-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
aws logs tail /aws/lambda/bowling-scraper-netsuite-product-scraper --follow
```

`netsuite_product_scraper.fetch_page()`'s session-cookie workaround for
MOTIV's product pages (which reject plain non-browser requests) was
flagged as an actual bet -- untestable from the sandbox that wrote it, no
outbound path to motivbowling.com. **Confirmed working via a real live
run this session**: after fixing the dot-relative-href URL discovery bug
(see netsuite_url_discovery's docstring), a full batch of 202 real
product URLs was scraped end-to-end with zero errors -- every
`Scraping <url>` log line was immediately followed by a successful
`Upserted product <id> (N SKUs)`, no 404s, no DLQ hits. The session-cookie
approach holds up in production; the URL bug was the only real issue.
If it ever does fail, check the DLQ:

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name bowling-scraper-netsuite-product-scrape-dlq --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages
```

and see README's "Third manufacturer: MOTIV Bowling" section for the
next things to try if the cookie-session approach doesn't hold up.

### 6e.5. MOTIV status-clobber bug + fix (real incident, found via a live data-quality pass)

**Requires a Lambda redeploy** (`sam build && sam deploy`) -- this fix is
backend/`netsuite_product_scraper` code, not admin-site-only.

Al noticed every product on the admin site's MOTIV catalog showed as
"current", which looked wrong given how many retired balls MOTIV has.
Confirmed via SQL that `netsuite_url_discovery` had correctly classified
all 434 MOTIV URLs (60 current, 374 retired), but every one of the 202
products actually scraped into `products` showed `status = 'current'`.
Root cause: `netsuite_product_scraper._process_one` used to blindly
default a missing `job["status"]` to `"current"`. That default is only
ever wrong for jobs `admin_api`'s `queue_rescrape` publishes (the generic
"Rescrape" button, and `scripts/backfill_core_ids.py`'s catalog-wide core
backfill) -- those publish `{"url", "brand_id"}` with no status key at
all, since Brunswick/SWAG infer status from the page/URL itself and never
needed one. NetSuite is the one platform that both has no on-page status
signal AND was blindly defaulting instead of looking one up. Worse,
`upsert_product`'s `status = excluded.status` unconditionally overwrites
(no coalesce-preserve-existing fallback the way `release_date`/
`description`/`core_id` have), so every status-less rescrape permanently
clobbered a retired MOTIV product back to `"current"` -- most likely
explanation for how all 202 ended up this way, given this catalog went
through exactly that catalog-wide core backfill in an earlier session.
See `src/netsuite_product_scraper/app.py`'s module docstring "REAL
INCIDENT" section for the full writeup.

**The fix** (already in this codebase as of this commit): added
`get_status_for_url()` to `netsuite_product_scraper`, mirroring
`shopify_product_scraper`'s function of the same name -- falls back to
`discovered_urls.status_path` (looked up by URL) for any job that omits
`status`, instead of defaulting. This only fixes it going forward for a
fresh scrape; it does nothing for the 202 rows that already went wrong.

**One-off correction for the already-wrong rows**, after redeploying:

```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/backfill_netsuite_status.py
```

Calls the new `POST /admin/backfill-netsuite-status` endpoint once (see
`service.backfill_netsuite_status`'s docstring) -- a single bulk
`UPDATE ... FROM discovered_urls` that corrects every netsuite-platform
product whose `status` disagrees with its `discovered_urls.status_path`,
matched by URL. Idempotent and safe to re-run: a second run reports
`products_corrected: 0` once everything's caught up. No template.yaml
changes needed -- reuses `AdminApiFunction`'s existing catch-all proxy
route, same as `/cores` and `/admin/backfill-last-video-discovery-at`
before it (confirmed via the CFN-tolerant YAML parser: still 45
resources).

**Confirmed run against the real catalog**: `products_corrected: 172`
(fewer than the original 202-product count -- expected, since some had
already been naturally re-corrected by an ordinary rescrape, e.g. this
session's own smoke tests, between the original diagnostic query and the
backfill actually running).

### 6e.6. MOTIV image-scraping over-collection bug + fix (real incident)

**Requires a Lambda redeploy** (`sam build && sam deploy`) -- backend
`netsuite_product_scraper` code, not admin-site-only.

Al noticed the scraper was pulling in images unrelated to the product
being scraped -- the 3 real per-ball photos, plus "a bunch that are just
on all product pages." Root cause: `parse_images()` used to run its
`IMAGE_RE` regex against the ENTIRE raw page HTML with no DOM scoping,
only requiring `userfiles/filemanager` to appear in the `url(...)`. That
path is MOTIV's general photo CDN, used for every product's photos
site-wide -- not something scoped to just the page being scraped. Any
other product's photo linked from elsewhere on the page (most likely a
cross-sell/"related products" strip that appears on every product page,
though the exact real markup wasn't captured this session) matched just
as readily as the actual product's own gallery.

Fixed by scoping `parse_images()` to two specific, real containers
instead of the whole page -- confirmed live via Al inspecting the DOM and
providing the exact selector for the main gallery:
`body > main > section.product > div > div > div > div.image-scroll-wrapper
> ul`. `parse_images()` anchors on the class-named segment
(`div.image-scroll-wrapper`) rather than the full child-index chain, so
it isn't fragile to the anonymous wrapper divs around it shifting depth.
The core-cutaway shot (a separate, already-handled case -- see the
sibling section in `netsuite_product_scraper/app.py`'s module docstring
point 7) was, at the time of this fix, scoped to
`div.product-specifications-by-weight`, the per-weight carousel it
actually lived in -- **later removed entirely, see 6e.7 below**.
Anything with a background-image style outside both containers is no
longer even looked at, regardless of what path it uses.

`parse_images()`'s signature changed from `(html: str, base_url: str)` to
`(soup: BeautifulSoup, base_url: str)` -- it now needs real DOM structure
to scope against, not just a string to regex-sweep. `parse_product_page`
was updated to pass its already-built `soup` instead of the raw `html`
string; no other caller exists. Both test fixtures
(`tests/fixtures/motiv_sigma_tour_pearl.html` and `motiv_jackal_onyx.html`)
were corrected from a previously-guessed `div.product-images` wrapper
class to the real `div.image-scroll-wrapper`, and each gained a
reconstructed (not captured -- see each fixture's own header comment)
"related products" section with other balls' thumbnails under
`userfiles/filemanager`, specifically to regression-test that the new
scoping actually excludes something the old unscoped regex would have
wrongly swept in.

**One-off cleanup for already-scraped products**, after redeploying:

```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/rescrape_netsuite_products.py
```

Unlike the status-clobber fix (6e.5), there's no targeted SQL correction
possible here -- a wrongly-attached image row looks like an ordinary
`product_images` row; nothing in the data itself distinguishes it from a
real one after the fact. The only fix is a fresh scrape under the
corrected parser.

Al asked directly whether this script would actually REMOVE the
already-wrong rows, not just add correct ones alongside them -- a fair
question, and the honest answer at first was no: `upsert_product`'s image
step was a plain insert-on-conflict-update (`ON CONFLICT (product_id,
source_url) DO UPDATE`), which only ever inserts a row for a source_url
still present or updates one that already matches -- it never deletes a
row for a source_url no longer part of what got parsed. Fixed by adding a
delete step to `upsert_product` (see its own docstring) that removes any
`product_images` row for the product whose `source_url` isn't in the
current parse's set, so a rescrape now genuinely REPLACES the image list
instead of just extending it.

Al's immediate next question: does that also clean up the S3 objects a
deleted row may have already had mirrored by `image_processor`? At first,
no -- the DB delete alone left those objects orphaned. Fixed by having
`upsert_product`'s DELETE return the removed rows (`id`, `stored_url`)
instead of deleting blind, and adding `delete_orphaned_image_objects()`
(called from `_process_one`, see its own docstring) to remove the
matching S3 objects -- `product-images/<id>/*` -- for any deleted row
that actually had a `stored_url` set. `NetsuiteProductScraperFunction`
now has an `IMAGE_BUCKET` env var (same bucket `ImageProcessorFunction`
already writes to) plus scoped `s3:ListBucket`/`s3:DeleteObject`
permissions (narrower than `ImageProcessorFunction`'s full `S3CrudPolicy`
-- this function only ever cleans up, never writes). Soft-fail if
`IMAGE_BUCKET` isn't set on a given deployment (logs a warning, skips
cleanup for that run) -- the DB-side fix works either way, same
optional-config convention as `IMAGE_PROCESS_QUEUE_URL`. This script
lists every `source_platform = 'netsuite'` product via the new
`GET /products?source_platform=netsuite` filter (added to
`service.list_products` for this) and calls `POST /products/{id}/rescrape`
for each, same paginate-and-republish shape as `scripts/backfill_core_ids.py`
(including its retry/backoff for the same Lambda-concurrency-throttle
risk -- see that script's docstring). It only enqueues; watch
`NetsuiteProductScraperFunction`'s logs/DLQ (6e) while it drains, then
spot-check a few product detail pages in the admin UI. No template.yaml
changes needed -- reuses `AdminApiFunction`'s existing catch-all proxy
route (confirmed via the CFN-tolerant YAML parser: still 45 resources).

### 6e.7. MOTIV core_callout image removed entirely (real incident)

**Requires a Lambda redeploy** (`sam build && sam deploy`) -- backend
`netsuite_product_scraper` code, not admin-site-only.

Same thread as 6e.6, immediate follow-up. Al asked directly why the
core-cutaway shot was even being captured, and pointed out it's
redundant: "that is already in the main section just low res and
redundant because it is below the fold and you wouldn't see it once you
have scrolled." He's right, confirmed by this module's own docstring
(point 7): the core-image path was always documented as "a transform of
one of the main gallery's own image ids" -- the same photo already
captured in `div.image-scroll-wrapper`, just at a lower-resolution CDN
format, sitting in a per-weight carousel below the fold nobody scrolls
back up to see rendered differently.

Fixed by removing the second `_extract_background_images()` scan of
`div.product-specifications-by-weight` from `parse_images()` entirely --
it now only scans `div.image-scroll-wrapper`. `core_callout` is no
longer a possible `image_type` this scraper produces, and the
empty-path-placeholder bug 6e.6 documented for that container is now
moot for the same reason (nothing looks at that container's markup
anymore, empty or not).

**No separate backfill/migration needed for already-scraped products.**
The rescrape script from 6e.6 (`scripts/rescrape_netsuite_products.py`)
already cleans up any previously-stored `core_callout` rows for free:
the stale-image DELETE added to `upsert_product` in 6e.6 treats any
`source_url` no longer present in the current parse as stale, and
`core_callout` rows will simply never appear in that set again --
including triggering the S3 orphan cleanup from 6e.6 for any that had
already been mirrored by `image_processor`. If 6e.6's rescrape has
already been run against the full catalog, running it again after this
redeploy is sufficient to finish cleaning these up too.

### 6f. commercebuild (Storm/Roto Grip/900 Global) -- if any of the three brand ids were set

No schedule wired up for `CommercebuildUrlDiscoveryFunction` yet, same
as SWAG/MOTIV -- invoke manually. Its one invocation covers whichever of
the three brands got a real id (see its module docstring -- a brand with
no id in `BRAND_IDS_JSON` is skipped, logged, not a hard failure):

```bash
aws lambda invoke --function-name bowling-scraper-commercebuild-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
aws logs tail /aws/lambda/bowling-scraper-commercebuild-product-scraper --follow
```

This platform got the most real-data verification of any manufacturer
added this session (see COMMERCEBUILD_SCOPING.md) -- template uniformity
across all three brands, the Tech Data PDF's table structure, and the
image markup were all confirmed via direct curl/pdfplumber against real
pages, not inferred. The one genuinely untested piece is the live
end-to-end run itself (no outbound path from the sandbox that built it),
so watch for two specific things on first run:

- `review_queue` entries with `source = 'commercebuild_html_vs_pdf'` --
  expected occasionally (real, disclosed HTML-vs-PDF disagreements are
  possible), but a mismatch on *every* product would suggest
  `parse_product_page()`'s field-shape assumptions don't hold for a
  brand/product beyond the three checked this session.
- the DLQ, if scraping fails outright:

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name bowling-scraper-commercebuild-product-scrape-dlq --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages
```

**Now also covers archived/retired products**, added in a later session
(see COMMERCEBUILD_SCOPING.md's "RESOLVED, later session" addendum) --
`CommercebuildUrlDiscoveryFunction` unions each brand's category-listing
URLs (current only) with `sitemap_products.xml` (current + archived +
non-ball merchandise, all sharing the same brand-prefixed flat URL
shape). `CommercebuildProductScraperFunction` classifies each URL by its
own page's breadcrumb trail at scrape time and skips non-ball products
gracefully (`"skipped": "non_ball_product"` in the result, no DB write,
no DLQ retry). This is genuinely untested end to end -- the sitemap
fetch, the brand-prefix bucketing, and the breadcrumb-based
current/retired/non-ball classification were all built and unit-tested
against real captured HTML this session, but never run against AWS.
Watch for on first run:

- Archived products landing with `status = 'retired'` and an empty
  `product_skus` (expected -- confirmed real this session that archived
  pages have no Tech Data PDF and no RG/Diff/PSA data anywhere in raw
  HTML, a genuine platform limitation, not a bug -- see
  COMMERCEBUILD_SCOPING.md).
- A meaningful number of `"skipped": "non_ball_product"` results (bags,
  apparel, accessories all share commercebuild's brand-prefixed URL
  shape) -- expected, not an error.
- If archived products come back with EMPTY name/coverstock/color fields
  instead of populated ones, that would mean the SPEC_LABEL_RE whitespace
  fix (see commercebuild_product_scraper/app.py) doesn't hold on some
  product beyond the three checked this session -- worth a real curl
  check before assuming it's the same bug recurring differently.

### 6f.5. Hammer (Shopify) -- if `HammerBrandId` was set

No schedule wired up for `ShopifyUrlDiscoveryFunction` yet, same as every
other non-Brunswick family -- invoke manually:

```bash
aws lambda invoke --function-name bowling-scraper-shopify-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
aws logs tail /aws/lambda/bowling-scraper-shopify-product-scraper --follow
```

Real, confirmed this session (live fetches against hammerbowling.com, not
inferred): the six collection JSON endpoints
(`/collections/{handle}/products.json`) all return real product lists, the
individual `{product_url}.json` endpoint returns the same `body_html`
field the collection listing's item omits, and four real product pages'
worth of `body_html` (Black Widow 3.0 Dynasty, Fallout, and the older
retired 3-D Offset/Absolut Curve listings, spanning three eras of markup)
were parsed correctly against `tests/fixtures/hammer_*.json` -- see
`src/shopify_product_scraper/app.py`'s module docstring for exactly which
formatting quirks each fixture covers. What's **not** yet confirmed: an
actual live Lambda invocation against AWS (no outbound path from the
sandbox that built this), so watch for on first run:

- `discovered_urls` rows getting a real `status_path` of `current` or
  `retired` per product -- if everything lands `current` regardless of
  which collection it came from, check that `HammerCollectionHandles`
  actually includes `retired-balls` (it does by default).
- The DLQ, if scraping fails outright:

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name bowling-scraper-shopify-product-scrape-dlq --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages
```

- A product with no `core_id` resolved despite having a real "CORE" field
  in `BALL SPECS` -- would mean `parse_ball_specs`'s no-whitespace-between-
  tags handling (see that function's docstring) doesn't hold on a real
  product beyond the ones checked this session; worth a direct
  `curl https://hammerbowling.com/products/<slug>.json` check before
  assuming it's the same bug recurring differently.

**Real incident, first live run:** deployed clean, all 219 products scraped
and upserted without error -- but every single one landed with a corrupted
`cores.name` (Al caught it: "all of the cores begin with 'E '", e.g.
`"Scandal"` stored as `"E Scandal"`). Root cause was in
`parse_ball_specs()`: it located each `BALL SPECS` field's value by slicing
`li.get_text()` at `len(strong.get_text())`, assuming the label's own text
length told you exactly where the value started. Real Hammer markup is
pretty-printed with a newline between `<li>` and `<strong>` (confirmed live
against `https://hammerbowling.com/products/scandal.json`:
`<li>\n<strong>CORE</strong><span> </span>Scandal</li>`) -- `li.get_text()`
includes that leading `"\n"`, `strong.get_text()` doesn't, so the slice
started one character early and landed on the label's own last letter.
Every `BALL_SPEC_LABEL_MAP` field was affected the same way, not just
`CORE` -- `COLOR` would have landed on its trailing `"R"` -- it just showed
up first via `cores.name` since that's the field Al happened to spot-check.
None of the four hand-built fixtures this feature originally shipped with
reproduced it (they were written without the leading newline), so the
23/23 passing test suite gave false confidence. Fixed by reading
`strong.next_siblings` instead of slicing by character count -- see
`parse_ball_specs`'s docstring in `src/shopify_product_scraper/app.py` for
the full explanation, and `tests/fixtures/hammer_scandal.json` (real
fetched `body_html`, not hand-built) plus its five regression tests in
`tests/test_shopify_product_scraper.py` for the exact before/after values.

Cleanup needed for the 219 already-corrupted rows after redeploying the
fix -- `scripts/backfill_core_ids.py` **won't** catch these, since it only
targets `GET /products?missing_core=true` and every one of these products
already has a (wrong) `core_id` set. Instead, force a rescrape of every
Hammer product regardless of its current core status:

```bash
psql "$DATABASE_URL" -Atc \
  "select id from products where brand_id = '<HammerBrandId>'" \
  | while read -r id; do
      curl -s -X POST "$ADMIN_API_URL/products/$id/rescrape" \
        -H "Authorization: Bearer $ADMIN_API_TOKEN" >/dev/null
      sleep 0.1
    done
```

That republishes each product onto `ShopifyProductScrapeQueue`; the fixed
Lambda will re-`upsert_product()`, and `core_id = coalesce(excluded.core_id,
products.core_id)` means the newly-resolved (correct) core id overwrites
the old wrong one. Once every product's re-scraped, the old `"E "`-prefixed
`cores` rows are orphaned (no `products.core_id` pointing at them anymore)
and safe to delete:

```sql
delete from cores
where brand_id = '<HammerBrandId>'
  and id not in (select core_id from products where core_id is not null);
```

### 6f.6. Track + Ebonite (Shopify) -- if `TrackBrandId`/`EboniteBrandId` were set

Same platform as Hammer, same shared `ShopifyProductScraperFunction`, but
each brand has its own discovery function -- no schedule wired up for
either yet, same reasoning as Hammer's:

```bash
aws lambda invoke --function-name bowling-scraper-track-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json

aws lambda invoke --function-name bowling-scraper-ebonite-url-discovery \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json

aws logs tail /aws/lambda/bowling-scraper-shopify-product-scraper --follow
```

Real, confirmed this session (live fetches against trackbowling.com and
ebonite.com, not inferred): both stores' collections.json/products.json/
{handle}.json endpoints behave identically to Hammer's, and each brand's
own collection-handle set was read directly from its real collections.json
rather than assumed (see `TrackCollectionHandles`/
`EboniteCollectionHandles`'s Parameters descriptions -- neither one is the
same set as Hammer's or each other's).

**The one real difference that mattered:** Track's and Ebonite's product
pages use an HTML `<table>` for BALL SPECS/RG-DIFF instead of Hammer's
`<ul><li>` list -- confirmed against six real live product fetches across
both brands (three modern, two older/retired, one very old novelty
listing with no structured specs section at all). Reusing Hammer's parser
unchanged against these would not have errored -- it would have silently
inserted every Track/Ebonite product with empty core/coverstock/RG-DIFF
data, a worse failure mode than the Hammer `"E "`-prefix bug (see 6f.5's
incident writeup below) because nothing about a "successful" 200 response
would have looked wrong. This was caught before wiring up either brand
(not after) and handled via `parse_ball_specs_table`/`parse_rg_diff_table`
in `src/shopify_product_scraper/app.py`, with real-fixture regression
tests in `tests/test_shopify_product_scraper.py`
(`track_theorem_delta.json`, `track_100p.json`, `ebonite_spartan_pearl.json`,
`ebonite_game_breaker_5_hybrid.json`, `ebonite_angry_birds.json`) -- see
that module's docstring for the full before/after story. What's **not**
yet confirmed: an actual live Lambda invocation against AWS for either
brand (no outbound path from the sandbox that built this) -- watch for on
first run:

- A Track or Ebonite product landing with populated `core_id`/
  `coverstock_name`/`skus` (not empty) -- confirms the table parser is
  actually matching real production markup, not just the fixtures.
- `discovered_urls.status_path` correctly split `current`/`retired` per
  product for both brands, same check as Hammer's.
- The DLQ, same command as Hammer's 6f.5 above (swap the queue name to
  `bowling-scraper-shopify-product-scrape-dlq` -- it's the SAME shared DLQ
  for all three brands, since `ShopifyProductScraperFunction` itself is
  shared).

### 6g. bowwwl.com cross-check

Runs weekly on its own schedule once there are `published = true`,
`status = 'current'` products in the DB for it to check (won't do
anything useful until 6b has run at least once and you've published a
product via the admin API's `PATCH /products/{id}/published`). Don't
wait a week to find out if it works -- invoke manually:

```bash
aws lambda invoke --function-name bowling-scraper-bowwwl-cross-check \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
```

Remember the ToS decision behind this function (see README's "QA
cross-checks" section) before pointing it at a large product catalog --
it's scheduled weekly specifically to keep load modest.

### 6h. BowlerDepot reconciliation -- only after step 3's BigCommerce secret exists

Ships with its daily schedule `Enabled: false` on purpose. Once you have
real BowlerDepot API credentials in Secrets Manager:

1. Update the stack parameter: `sam deploy --guided` again (or
   `--parameter-overrides BigCommerceSecretArn=<arn>` non-interactively),
   keeping every other parameter the same.
2. Flip `Enabled: false` to `Enabled: true` on
   `BowlerDepotReconciliationFunction`'s `DailySchedule` event in
   `template.yaml`, then `sam build && sam deploy` again.
3. Before trusting its accuracy-check output, verify
   `CUSTOM_FIELD_NAME_CANDIDATES` in
   `src/bowlerdepot_reconciliation/app.py` actually matches your real
   store's `custom_fields` names (pull one real product via the
   BigCommerce API and check) -- this was a disclosed best-guess mapping,
   never checked against a real store. A wrong guess won't error, it'll
   just silently report zero mismatches, which looks like "all clean"
   when it's actually "not checking anything." See README's "QA
   cross-checks" section for the full caveat.

### 6i. Video enrichment (YouTube + Bedrock) -- only after `YouTubeApiKeySecretArn` is set and the Bedrock model is granted access

**Transcript fetching is now exclusively the home browser cron (6k), not
`VideoTranscriptFetcherFunction`.** Real, live-tested evidence this project
(see src/video_transcript_fetcher/app.py's module docstring) confirmed
that Lambda's plain-HTTP fetch is blocked by YouTube's PoToken/BotGuard
requirement regardless of network path (VPC or non-VPC) -- it's not a
"maybe fix this later" gap, it's a dead end. Because of that,
`approve_video_candidate` no longer publishes to `VideoSummarizeQueue` on
approval (see its docstring in src/admin_api/service.py for the full
reasoning): the old behavior would have raced the confirmed-working
browser fetcher, since the broken Lambda always writes *some*
`transcript_note` on completion (even a failure one), and the browser
cron's `needs_transcript` filter treats any existing note as "already
checked, don't retry." `VideoTranscriptFetcherFunction` and
`VideoSummarizeQueue` are still deployed (harmless, just never invoked
now) -- tearing them out is a separate cleanup, not required for any of
this to work. Approving a candidate now just marks it `approved` and
leaves `transcript_note` untouched, so 6k's cron is free to pick it up
whenever it next runs.

1. Find a real `product_id` (or a few) via `GET /products?search=...` on
   the admin API (see 6a for the auth header shape).
2. Discover candidates. Each product costs one YouTube search.list call
   against a confirmed 100/day quota, capped per-invocation at 70 (see
   src/video_discovery/app.py's module docstring) -- test on a small,
   explicit list first:
   ```bash
   aws lambda invoke --function-name bowling-scraper-video-discovery \
     --payload '{"product_ids": ["<product-id>"]}' \
     --cli-binary-format raw-in-base64-out /tmp/out.json
   cat /tmp/out.json
   ```
   To cover a whole catalog, run it with `{}` (all 'current', non-retired
   products, regardless of `published` -- see app.py's module docstring:
   a real check against this catalog found 142 'current' products but only
   1 with `published = true`, so requiring `published` here would have
   meant this basically never ran; discovery is meant to run ahead of
   publishing, so candidates are ready by the time a product goes live)
   once a day until it's caught up -- there's no schedule wired up for
   this function on purpose (see the "no automated schedule" section
   further down), so each day's invoke is a manual/cron call you make
   yourself:
   ```bash
   aws lambda invoke --function-name bowling-scraper-video-discovery \
     --cli-read-timeout 300 --cli-connect-timeout 10 \
     --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json
   cat /tmp/out.json
   ```
   **Always pass `--cli-read-timeout` above this function's own Timeout
   (280s, see the resource's own comment in template.yaml) for this
   particular invoke.** Real, confirmed incident: the AWS CLI's default
   read timeout is 60s, well under how long a retry-heavy invocation can
   legitimately take -- the CLI gives up and reports a client-side error
   while the Lambda keeps running server-side regardless, and if the CLI
   (or you, seeing the error) then re-invokes, you can end up with two
   overlapping product-search bursts hammering the same per-minute YouTube
   quota at once, which is worse than either burst alone. The result body
   now also includes `circuit_breaker_tripped`/`products_skipped` --
   real, confirmed second incident: a sustained-throttled batch blew past
   even the 280s Timeout with a hard `Sandbox.Timedout` kill (see
   app.py's module docstring for both incidents' full writeups). If a
   response comes back with `circuit_breaker_tripped: true`, that can mean
   either of two DIFFERENT limits (both real, both confirmed this
   project -- see REAL INCIDENT #3 in app.py's module docstring): a
   short-lived per-minute throttle (wait a few minutes and re-invoke), or
   the DAILY 100-query quota already being exhausted for the day (in which
   case re-invoking will just trip the breaker again immediately -- getting
   the exact same `circuit_breaker_tripped` result on back-to-back
   invocations is the tell; wait for the quota to reset, typically midnight
   Pacific, or request a quota increase in Google Cloud console).

   A catalog of, say, 300 published products takes ~5 days of these calls
   (70/day) to fully cover; re-running discovery against a product that
   already has candidates is safe (`insert_candidates` is idempotent
   against an already-known `youtube_video_id` -- see test_video_discovery.py).

   This scope genuinely rotates now -- each `{}` invocation picks the
   least-recently-searched products first (`last_video_discovery_at asc
   nulls first`, never-searched sorting ahead of everything), not the same
   top-N every time. That wasn't always true: the original `order by
   p.updated_at desc` never advanced (nothing in this pipeline touches
   `updated_at`), so repeated `{}` calls silently re-searched the same
   products forever and never reached the rest of the catalog -- a real
   bug caught in production and fixed by
   005_products_last_video_discovery_at.sql (see app.py's module docstring,
   ROTATION section, and `mark_product_searched`). One side effect worth
   knowing: since `{}` now always picks the least-recently-searched
   products, if you deliberately want to re-search products you already
   covered (e.g. after raising `MAX_RESULTS_PER_PRODUCT`, as happened here
   going from 5 to 20), passing an explicit `{"product_ids": [...]}` list
   is the reliable way to force it -- that scope selects exactly the ids
   you name regardless of their rotation position (it doesn't order by
   `last_video_discovery_at` at all). It still updates
   `last_video_discovery_at` for whatever it searches, same as every other
   scope -- so those products' rotation position resets to "just searched"
   afterward, which is the right outcome either way.

   **One-off correction if you're on a stack that ran video_discovery
   before migration 005 existed:** that migration added
   `last_video_discovery_at` with no backfill, so any product searched
   under the old `updated_at desc` rotation has real `product_videos`
   rows but a NULL `last_video_discovery_at` -- it looks "never searched"
   to the new rotation column and keeps jumping the queue ahead of
   products that genuinely never were. Real, confirmed gap on this
   project's own catalog: a count check found 231 products with a NULL
   column but only 174 with zero `product_videos` rows at all (~57
   products affected). Run this once (safe to re-run -- idempotent, only
   ever sets a currently-NULL column):
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     "$ADMIN_API_URL/admin/backfill-last-video-discovery-at"
   ```
   or `python3 scripts/backfill_last_video_discovery_at.py` (same
   `ADMIN_API_URL`/`ADMIN_API_TOKEN` env vars as every other script here).
   Either way returns `{"products_with_video_history": N, "products_updated": M}`
   -- `M` is how many products actually had the gap; 0 on a fresh stack or
   once caught up. See `service.backfill_last_video_discovery_at`'s
   docstring for why this is safe even if a real `video_discovery`
   invocation runs concurrently (it only fills in rows still NULL, never
   overwrites one that's already been legitimately set).
3. Check what landed in `product_videos` as pending:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" "$ADMIN_API_URL/video-candidates?status=pending"
   ```
4. Approve. For one candidate at a time:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"resolved_by":"al.wolfe@bringyourbest.co"}' \
     "$ADMIN_API_URL/video-candidates/<video-id>/approve"
   ```
   Or in bulk with `scripts/auto_approve_video_candidates.py`, which
   auto-approves every `match_confidence='high'` pending candidate and
   prints the `'low'` ones (title/channel/product) for you to eyeball and
   approve/reject by hand -- 'high' is still a simple title-token
   heuristic, not a guarantee (see that script's module docstring for the
   known false-positive shape, e.g. "Storm Absolute Power Review" also
   scoring high for the "Storm Absolute" product), so a wrong auto-approval
   is possible but reversible via the reject endpoint below, not silent or
   permanent:
   ```bash
   python3 scripts/auto_approve_video_candidates.py
   ```
   Reject a bad match (auto- or hand-approved) the same way approval
   works, just against `/reject`:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"resolved_by":"al.wolfe@bringyourbest.co","reason":"wrong product"}' \
     "$ADMIN_API_URL/video-candidates/<video-id>/reject"
   ```
5. Fixing a wrong match. `score_match`'s 'high' confidence only requires
   the brand name plus ANY ONE product-name token in the title -- a video
   titled "Storm Absolute Power Review" scores 'high' for the "Storm
   Absolute" product too, not just "Storm Absolute Power". This is a real,
   accepted risk of auto-approving 'high' matches in bulk (see
   scripts/auto_approve_video_candidates.py), not something prevented
   up front -- it's meant to be caught and fixed after the fact:
   ```bash
   # Move it to the right product (works at any status; keeps any
   # transcript/summary already fetched rather than losing it):
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"product_id":"<correct-product-id>"}' \
     "$ADMIN_API_URL/video-candidates/<video-id>/reassign"

   # If that 422s because the correct product already has its own row for
   # this same video (a real, legitimate case -- two products can share a
   # review video), delete the duplicate first, then retry the reassign:
   curl -X DELETE -H "Authorization: Bearer $TOKEN" \
     "$ADMIN_API_URL/video-candidates/<duplicate-video-id>"
   ```
   `/reassign` is a hard move, not a copy -- the row's `product_id`
   changes in place. `/reject` (step 4) is different from `DELETE` here:
   reject just marks `status='rejected'` and keeps the row for audit;
   `DELETE` actually removes it, which is what you want for cleaning up a
   genuine duplicate, not for "this video isn't relevant to any product."
6. Confirm transcripts show up after the home browser cron's next run (6k)
   -- approved candidates just sit with `transcript_note` unset until then,
   this isn't an async-Lambda "check back in a minute" step anymore:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" "$ADMIN_API_URL/video-candidates/<video-id>"
   ```
   Expect either a real `transcript`/`summary`, or `transcript_note` set to
   something like `no_captions_available` (a real, expected outcome for
   videos without captions, not a bug). If a candidate approved days ago
   still has no `transcript_note` at all, the cron job itself likely isn't
   running -- check `~/bowling-transcript-fetcher-browser.log` on the Pi
   first.
7. "Summary of summaries" -- a product-level rollup, synthesized from
   every approved video's own summary for that product. video_summarizer
   regenerates it automatically every time a video gets a real summary
   written (no separate trigger, nothing to run by hand) -- see
   src/video_summarizer/app.py's module docstring, SUMMARY OF SUMMARIES
   section, and `refresh_video_reviews_rollup`. A single summarized video
   is enough to produce one (not gated behind a minimum count); it still
   goes through Bedrock rather than just copying that one summary
   verbatim, specifically so the field's voice/framing stays consistent
   whether it's built from 1 video or 10 (a per-video summary is written
   in that video's own context -- "in this video, the reviewer notes..."
   -- which reads oddly copied straight into a product-level field).
   Regeneration is soft-fail: a Bedrock hiccup here never blocks or
   retries the video's own summary, it just leaves the existing rollup
   (if any) stale until the next successful video summarization retries it.
   ```bash
   curl -H "Authorization: Bearer $TOKEN" "$ADMIN_API_URL/products/<product-id>" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["video_reviews_summary"], d["video_reviews_summary_video_count"])'
   ```
   (`GET /products/{id}` already returns every column via `select *`, so
   no new admin API endpoint was needed to read a rollup.)
8. Backfilling rollups for products summarized before automatic
   regeneration existed. Step 7's automatic path only fires as a side
   effect of a video getting a *new* summary written -- any product whose
   videos were already approved+summarized before that trigger (or before
   this endpoint) existed has no rollup, and nothing revisits an
   already-summarized video to build one. This is also the fix after a
   bulk reassign/delete cleanup (step 5): a product's set of approved
   videos can change without any video getting freshly summarized, which
   the automatic trigger has no way to notice.

   One product at a time:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     "$ADMIN_API_URL/products/<product-id>/refresh-video-summary"
   ```
   Returns `{"product_id": ..., "rollup_regenerated": true, "video_count": N}`,
   or `{"rollup_regenerated": false, "reason": "no_summaries"}` if the
   product has no approved+summarized videos (not an error).

   `GET /products?needs_video_summary_refresh=true` lists exactly the
   products that call would actually change -- at least one approved+
   summarized video, and the stored rollup is either missing or stale
   relative to the current approved+summarized count (see
   `list_products`'s docstring in src/admin_api/service.py). Safe to
   re-run any time, not just once: it recomputes off the real current
   count each call.

   For the whole catalog, `scripts/backfill_video_review_rollups.py` pages
   through that filter and calls the refresh endpoint for each match,
   tolerating per-product errors (one Bedrock hiccup doesn't stop the
   batch -- it just gets picked up again next run):
   ```bash
   export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
   export ADMIN_API_TOKEN="$TOKEN"
   python3 scripts/backfill_video_review_rollups.py
   ```
   Logs a per-product line and a final `{"total": N, "refreshed": N, "errors": N}`
   summary; exits non-zero only if every product it tried to refresh errored.
9. Manufacturer description as rollup context. `products.description`
   (a column that's existed in the schema since migration 001 but was
   never actually populated by any scraper) is now scraped by all four
   platforms and fed into the "summary of summaries" prompt (step 7) as
   grounding context -- helps get technical details right (core/
   coverstock names, the lane conditions the manufacturer markets it for)
   without letting the rollup just restate marketing copy; the prompt is
   explicit the output must still reflect what reviewers actually said.
   Confirmed live via Claude in Chrome that all four platforms carry real,
   ball-specific description text (not just generic tier/tech blurbs) --
   see each scraper's `parse_description` docstring for the exact CSS
   selector used per platform:
   | Platform | Selector |
   |---|---|
   | Brunswick (`product_scraper`) | `.c-product-feature__info-body .u-hide` (visually hidden, but present in the raw server HTML) |
   | Storm/Roto Grip/900 Global (`commercebuild_product_scraper`) | `.secondary-desc` |
   | SWAG (`woocommerce_product_scraper`) | `.product-short-description` |
   | MOTIV (`netsuite_product_scraper`) | `section.product form.order-form div.wysiwyg` |

   `description` is coalesce-updated on every re-scrape (same pattern as
   `release_date`), so a parse miss on one run doesn't null out a
   previously-good value. Existing products won't have a description until
   their next re-scrape -- no migration or backfill script needed for this
   one specifically, it just fills in naturally as `product_scraper`/
   `commercebuild_product_scraper`/`woocommerce_product_scraper`/
   `netsuite_product_scraper` re-run (daily cron or manual invoke, same as
   any other field). The rollup itself only regenerates when a video gets
   summarized (step 7) or via the backfill endpoint (step 8), so a product
   whose description just got backfilled won't show it in
   `video_reviews_summary` until one of those triggers fires again.
   **Correction from a real run:** the plain (default-mode)
   `scripts/backfill_video_review_rollups.py` does *not* pick this up --
   its `needs_video_summary_refresh` filter is a pure video-count
   staleness check (see step 8) with no way to notice a description
   change, since that doesn't move the video count. Confirmed live: after
   backfilling descriptions onto 76 already-summarized products, a plain
   run reported `0 products needing refresh` -- correct behavior of that
   filter, not a bug, but it means a description backfill needs the
   broader mode described in the next step instead.
10. Catalog-wide rollup regeneration (`REFRESH_ALL` mode). For the "I just
    backfilled a field that the staleness filter can't see" case above --
    or any other one-time reason you want every eligible product's rollup
    regenerated regardless of whether it looks stale -- `GET /products`
    also accepts `has_approved_video_summaries=true`: every product with
    at least one approved+summarized video, no staleness comparison at
    all (deliberately broader than `needs_video_summary_refresh` from step
    8). `scripts/backfill_video_review_rollups.py` exposes this via a
    `REFRESH_ALL` env var:
    ```bash
    export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
    export ADMIN_API_TOKEN="$TOKEN"
    REFRESH_ALL=true python3 scripts/backfill_video_review_rollups.py
    ```
    Same per-product logging and `{"total", "refreshed", "errors"}`
    summary as the default mode. This is real Bedrock cost across every
    eligible product each time it's run, not just the stale ones -- treat
    it as an occasional deliberate pass (e.g. right after a description
    backfill like this one), not something to schedule routinely. Leave
    `REFRESH_ALL` unset for routine/scheduled use.
11. Admin site (`admin-site/index.html`). A single self-contained HTML/JS
    page covering the manual workflows this section has otherwise required
    curl/psql/a terminal for: review_queue approve/reject, video-candidate
    approve/reject/reassign/delete + manual transcript submission (a
    browser-based fallback to step 5's home script), product search +
    publish toggle + description/rollup viewing, and browser versions of
    steps 8/10's single-product and batch (stale/`REFRESH_ALL`) rollup
    refresh jobs. No build step or new AWS infra needed to run it -- open
    the file directly (`file://`), or host it wherever later; it talks
    straight to `admin_api`'s HTTP API from the browser via `fetch()`,
    using the same bearer token scheme as every script above.

    **Requires a redeploy** if `AdminHttpApi` hasn't been deployed since
    this page was added: it now needs a `CorsConfiguration` block (see
    `template.yaml`) so a browser on a different origin than the API
    actually receives a response instead of being blocked by CORS --
    `sam build && sam deploy` picks this up like any other template
    change.

    **Real gotcha hit on first live use, now fixed:** `CorsConfiguration`
    alone wasn't enough -- `AdminApiFunction`'s route was a single
    `Method: ANY` on `/{proxy+}`, and `ANY` matches `OPTIONS` too. API
    Gateway only auto-answers a CORS preflight for a path+method that has
    no explicit route of its own; since `ANY` already claimed one, the
    preflight fell through to the normal integration, which requires
    `TokenAuthorizer`, which 401s on the missing `Authorization` header
    every real preflight request has (browsers never attach custom
    headers to a preflight). Confirmed live via `curl -X OPTIONS
    <api-url>/health` returning a bare `{"message":"Unauthorized"}`.
    Fixed by splitting that one `ANY` event into four explicit
    `AdminApiGet`/`AdminApiPost`/`AdminApiPatch`/`AdminApiDelete` events
    (the actual methods `admin_api/app.py` defines) -- with no explicit
    `OPTIONS` route left, `CorsConfiguration`'s automatic unauthenticated
    preflight handling applies. Redeploy again if you deployed the CORS
    block before this fix.

    **Second real gotcha, also confirmed live:** even with the route fix
    above, opening `admin-site/index.html` directly via `file://` still
    fails CORS -- a `file://` page sends `Origin: null`, and AWS API
    Gateway's automatic CORS handling does not add
    `Access-Control-Allow-Origin` for a literal `null` origin, even with
    `AllowOrigins: ["*"]` (confirmed via curl: `Origin: null` gets a bare
    204 with no CORS headers at all; a real origin like
    `http://localhost:8000` gets the full set). So this page needs to be
    served from a real origin, not opened as a local file -- easiest way:
    `cd admin-site && python3 -m http.server 8000`, then open
    `http://localhost:8000` in a browser.

    On first open, fill in the Settings bar (top of the page): the same
    `ADMIN_API_URL` used above, the same bearer token, and your name (used
    as `resolved_by` on approve/reject calls) -- these persist in that
    browser's `localStorage` only. Deliberately a flat single file, not
    componentized -- meant to get real usage against the current API
    surface before investing in a framework-based rebuild.

    Product detail view (Products tab -> Detail) also renders the pulled-
    down images: `GET /products/{id}` already returns `product_images`
    rows (`image_type`, `weight_lbs_context`, `source_url`, `stored_url`)
    via `service.get_product()`, so this was a frontend-only addition, no
    `admin_api` change needed. `stored_url` (when set) is a public S3 URL
    for the "detail" size variant -- `ImageBucket`'s policy is
    intentionally public-read (see `template.yaml`), so it loads directly
    as an `<img src>` with no signing. The thumbnail shown is derived by
    swapping `detail.png` for `thumbnail.png` in that same URL (the
    storage convention `image_processor/app.py` documents -- same S3 key
    prefix, one object per size). A row whose `stored_url` is still null
    (queued in `ImageProcessQueue` but not processed yet, or stuck in its
    DLQ) falls back to `source_url` -- the original manufacturer image --
    and is labeled "not processed yet" rather than showing a broken image
    or nothing at all.

    **"Show all fields (raw)"** -- real ask from Al: he noticed data
    issues in the admin UI he suspects trace back to the scrapers, and
    wanted every DB column visible (not just whatever each hand-curated
    section above happened to render) so gaps show up by inspection
    instead of by guessing which field might be wrong. A collapsed
    `<details>` block at the bottom of each product's detail row expands
    into a generic field-name/value table built from whatever
    `GET /products/{id}` actually returns (`renderRawTable` in
    `admin-site/index.html` -- no per-field hardcoding, so it stays
    complete as columns get added later without anyone remembering to
    update this view too). `service.get_product()` was widened to match:
    `product_skus`/`product_images` went from a hand-picked column list to
    `select *` (previously missing `id`/`created_at`/`updated_at`/
    `part_number` off both), and three real tables that NO admin_api
    endpoint had ever exposed before this are now included per product:
    `discovered_url` (this product's own crawl record from
    `discovered_urls` -- `scrape_status`, `sitemap_lastmod`,
    `last_scraped_at`; matched by `url` since that table has no
    `product_id` FK; `null` if the product was never crawled through the
    normal sitemap/collection discovery, e.g. inserted by hand like the
    Hammerhead product from an earlier session), `bowlerdepot_matches`,
    and `bowwwl_matches` (this product's reconciliation rows against
    BowlerDepot's BigCommerce catalog and bowwwl.com, both tables that
    have existed since migrations 001/003 with no admin API surface at
    all until now). `brand_name`/`manufacturer_name` are also newly
    joined on -- `brand_id` alone is a bare UUID, not something you can
    eyeball for a data-quality pass. No `template.yaml` change needed:
    `GET /products/{id}` already existed and already routed through the
    `{proxy+}` GET route.

    **Brand filter dropdown.** The Products (and Cores) tab's brand
    filter used to be a raw-UUID text box -- `list_products`/`list_cores`
    already accepted `brand_id`, but you had to already know or separately
    look up the UUID to use it. New `GET /brands` endpoint
    (`service.list_brands` -- unpaginated, no search/filter params; a
    dozen-ish brands total is nowhere near enough to need any of that,
    unlike products/cores) backs a real `<select>` populated with brand
    names, shared by both tabs' `select.brand-filter` elements via one
    `loadBrandOptions()` call in `admin-site/index.html` (fires on page
    load and again right after Settings are saved, so a fresh
    API URL/token doesn't need a manual page reload to see brand names).
    `list_products`'s query also gained `left join brands b on b.id =
    p.brand_id` / `b.name as brand_name` so the Products tab's list column
    shows a real brand name instead of a truncated `brand_id` UUID. No
    `template.yaml` change needed here either -- `GET /brands` rides the
    same `{proxy+}` GET route every other admin_api endpoint does.

    **Release Date column.** Al asked whether MOTIV's on-page "available"
    date could show up as a release date column on Products -- turned out
    every scraper (`product_scraper`/`commercebuild_product_scraper`/
    `woocommerce_product_scraper`/`netsuite_product_scraper`) was already
    parsing and persisting this via each platform's own
    `parse_release_date` + `upsert_product`'s coalesce-preserve-existing
    pattern (see 003_date_tracking_and_bowwwl.sql, which added the
    `release_date` column's doc comment and the code-side parsing in the
    same pass) -- `list_products` just wasn't selecting it, and nothing
    rendered it. Added `p.release_date` to `list_products`'s SELECT
    (`service.py`) and a "Release Date" column to the Products tab table.
    Rendered via a new `fmtDateOnly()` helper rather than the existing
    `fmtDate()` -- `release_date` is a plain `DATE` column with no
    time-of-day, and `fmtDate()`'s `new Date(s).toLocaleString()` parses a
    bare ISO date as UTC midnight, which can display as the previous
    calendar day in negative-UTC-offset timezones; `fmtDateOnly()` reads
    the y/m/d parts straight off the ISO string instead. No `template.yaml`
    change needed -- same `{proxy+}` GET route as everything else in this
    list. `announced_date` is the harder, separate ask Al flagged himself
    -- no platform exposes a distinct "announced" date separate from
    release/availability anywhere in its HTML, especially not for
    older/historic balls, so it stays unpopulated and out of this column
    list until a real source turns up (see 003's own comment on that
    reserved column).

**Video candidates + rescan in the product detail view.** Al: "can we
add the video candidates for products into the product details view. i
think having them there is a good idea. also if we could add a button
with them to search for candidates again." Two additions:

1. `loadProductDetailInto` now also fetches `GET /video-candidates
   ?product_id={id}&status=all&limit=200` alongside the product itself
   (in parallel, `Promise.all`) and renders a "Video candidates" table
   right in the detail panel -- title/channel, match score, summarized
   yes/no, and the same Approve/Reject/Reassign/Delete actions the Video
   Candidates tab has, just refreshing this panel afterward instead of
   that tab's list. `status=all` is a new sentinel `service.
   list_video_candidates` (and `GET /video-candidates`) understands --
   `status=None` omits the WHERE clause entirely, showing pending/
   approved/rejected together. Every other existing caller is unaffected
   (`status` still defaults to `"pending"`, and any literal status value
   still filters exactly as before).

   **Why "all" statuses together, specifically:** Al's own example --
   "the combat solid has a bunch of videos approved for it that are for
   the original combat and combat hybrid but the new videos for it are
   not there." This is the exact false-positive shape `reassign_video_
   candidate`'s own docstring already documents: `score_match` scores
   'high' confidence on the brand name plus ANY ONE significant
   product-name token, so a review titled just "Combat" or "Combat
   Hybrid" scores 'high' for the "Combat Solid" product too (all three
   share the "combat" token), and `scripts/auto_approve_video_
   candidates.py` auto-approves 'high' matches in bulk without a human
   looking first -- a known, accepted tradeoff, not a new bug, and
   `reassign_video_candidate`/`delete_video_candidate` already exist as
   the correction tools for exactly this. What was missing was
   visibility: the Video Candidates tab only ever shows one status at a
   time and isn't scoped to a product by default, so a bad auto-approval
   like this could sit unnoticed indefinitely. Seeing a product's
   approved AND pending candidates side by side, scoped to just that
   product, is what actually lets an admin spot "wait, these two
   'approved' ones are for the wrong ball" and fix it on the spot.

2. **"Search for videos again" button**, `POST /products/{id}/discover-
   videos` -> `service.queue_video_discovery`. `VideoDiscoveryFunction`
   already accepted a `{"product_ids": [...]}` scope (see its own module
   docstring's job-shape list) -- this is just the first thing to
   actually invoke it from `admin_api` instead of by hand via `aws lambda
   invoke`. Unlike `queue_rescrape` (publishes onto an SQS queue a
   scraper Lambda already consumes), there's no queue in front of
   `VideoDiscoveryFunction` -- this calls `lambda:InvokeFunction`
   directly with `InvocationType='Event'` (async/fire-and-forget), since
   a real search.list call plus DB writes can take a few seconds and
   `VideoDiscoveryFunction`'s own `Timeout` is 280s (sized for YouTube's
   per-minute rate-limit backoff, see that function's module docstring)
   -- far past what's reasonable to block `AdminApiFunction`'s
   request/response cycle on. Directly relevant to Al's Combat Solid
   report's other half ("the new videos for it are not there"): the most
   likely explanation is simple staleness -- `video_discovery`'s rotation
   only reaches a given product again once the rest of the catalog has
   been searched (see `fetch_products_to_search`'s `last_video_discovery_
   at asc nulls first` ordering) -- and this button is exactly the "check
   this one product again right now" escape hatch for that, without
   waiting on the rotation or running a manual `aws lambda invoke`.

   `AdminApiFunction` gained a `VIDEO_DISCOVERY_FUNCTION_NAME` env var
   (`!Ref VideoDiscoveryFunction`, the function name, not its ARN -- SAM/
   CloudFormation `Ref` on a Lambda function resource returns the name)
   and a scoped `lambda:InvokeFunction` policy statement (exactly
   `VideoDiscoveryFunction`'s own ARN, not a blanket grant). Same
   soft-fail convention as `queue_rescrape`: returns `{"queued": false,
   "reason": ...}`, not a 500, if `VIDEO_DISCOVERY_FUNCTION_NAME` isn't
   configured on a given deployment.

   No other `template.yaml` change needed for either half of this --
   `POST /products/{id}/discover-videos` rides `AdminApiFunction`'s
   existing `/{proxy+}` catch-all (confirmed via the CFN-tolerant YAML
   parser: still 45 resources).

### 6j. Home transcript fetcher (residential caption fetching) -- optional, run outside AWS entirely

Real, live-tested finding this session (see
src/video_transcript_fetcher/app.py's module docstring for the full
evidence trail): YouTube's watch-page caption data comes back empty from
every AWS Lambda network path tried -- VPC-attached and non-VPC both,
across multiple videos, even with a real browser User-Agent -- but
succeeds from a residential connection. That's consistent with
IP/ASN-reputation-based detection that no code change inside AWS can fix.
`scripts/home_transcript_fetcher.py` is the honest way around that: a
low-volume script meant to run once a day from hardware you control at
home (a Raspberry Pi, a spare box, whatever's on your own residential
connection), not a rotating-proxy pool disguising bulk traffic -- see that
script's module docstring for the full reasoning, including the honest
caveat that this still doesn't make the fetch fully compliant with
YouTube's Terms of Service (Section 5.B prohibits "any automated means"
regardless of whose IP it's on), just lower-risk and non-deceptive
compared to what this project has explicitly ruled out.

It's a third possible producer for `VideoTranscriptResultQueue` --
`video_transcript_fetcher` (off AWS, per the Lambda-based path above) and
this script both feed the same queue via different means, and
`video_summarizer` doesn't know or care which one a given message came
from.

Setup on the Pi/home server. Modern Raspberry Pi OS (Debian 12/Bookworm+)
blocks a bare `pip install` into the system Python (PEP 668,
"externally-managed-environment") -- use a virtual environment instead of
fighting that protection:
```bash
python3 -m venv ~/bowling-transcript-fetcher-venv
source ~/bowling-transcript-fetcher-venv/bin/activate
pip install -r scripts/requirements.txt

export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used for every other admin API call in this runbook>"
python3 scripts/home_transcript_fetcher.py
```
(This is the same venv 6k's browser-based fetcher uses below --
`requirements-browser.txt` is a superset of `requirements.txt`, so one
venv covers both scripts; no need for two.)

Cron, once a day (put the env vars in a `chmod 700` wrapper script rather
than the crontab itself, so the token isn't sitting in plaintext in
`crontab -l` -- 700, not 600, since cron executes the script directly and
needs the execute bit, not just read; call the venv's `python3` directly
by full path, since cron doesn't run your shell's `source`d activation;
and log to a path in your home directory, not `/var/log`, which a normal
user typically can't write to without sudo):
```
0 7 * * * ~/run_transcript_fetcher.sh >> ~/bowling-transcript-fetcher.log 2>&1
```
where `run_transcript_fetcher.sh` (`chmod 700 ~/run_transcript_fetcher.sh`) contains:
```bash
#!/bin/bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<token>"
~/bowling-transcript-fetcher-venv/bin/python3 /home/pi/brunswick-scraper/scripts/home_transcript_fetcher.py
```

What it does each run: lists every `approved` video candidate that
doesn't already have a `transcript_note` or a summary (see
`needs_transcript` in the script -- deliberately does NOT re-check
candidates that were already tried, even ones that came back with no
captions, so this doesn't hammer the same handful of caption-less videos
every single day forever), fetches each one's transcript using your home
connection, and `POST`s the result to
`$ADMIN_API_URL/video-candidates/{id}/transcript` -- the same endpoint
publishes to `VideoTranscriptResultQueue`, so `video_summarizer` picks it
up and runs the Bedrock summarization exactly like it would for a
Lambda-fetched transcript.

Smoke-test it manually once before trusting the cron job:
```bash
cd brunswick-scraper
ADMIN_API_URL="$ADMIN_API_URL" ADMIN_API_TOKEN="$TOKEN" python3 scripts/home_transcript_fetcher.py
```
Check the log output for `Done: {'total': ..., 'got_transcript': ...,
'no_captions': ..., 'errors': ...}`, then confirm via
`GET /video-candidates/<video-id>` (same as 6i step 5) that a transcript
or an honest `transcript_note` actually landed.

### 6k. Browser-based home fetcher (Pi 5) -- for when the plain-HTTP fetcher hits PoTokenRequired

Real finding from testing 6j live: the plain-HTTP `home_transcript_fetcher.py`
gets past the network-level wall that blocked every AWS Lambda attempt
(caption track *listing* genuinely works from a residential connection),
but then hits a different, harder wall -- the actual transcript *content*
fetch requires a PoToken (YouTube's BotGuard-issued "proof of origin"
token), which no HTTP client without a real browser behind it can produce.
Confirmed via a live test returning a 200 with a completely empty body,
matching a known, open issue in the `youtube-transcript-api` project
(`jdepoix/youtube-transcript-api#592`). See
`scripts/home_transcript_fetcher_browser.py`'s module docstring for the
full reasoning, including why generating a PoToken ourselves (solving
YouTube's bot-detection challenge computationally) is explicitly NOT
something this project does -- that's real anti-bot-evasion, not an
incidental technical gap.

`home_transcript_fetcher_browser.py` takes a different approach instead:
a real, unmodified headless Chromium browser (via Playwright) loads the
actual video page and clicks the real "Show transcript" button -- the
same UI feature a human would use, rendered by the page's own
already-authenticated JavaScript, not a hand-built signed-URL request. It
reuses `home_transcript_fetcher.py`'s admin-API listing/submission logic
(`run()` now takes a pluggable `get_transcript_fn`) rather than
duplicating it -- only the actual YouTube-fetching mechanism differs
between the two scripts.

**Setup on the Pi 5** -- same venv as 6j (skip the `python3 -m venv` step if
you already created it there; `requirements-browser.txt` is a superset of
`requirements.txt` so this covers both scripts):
```bash
python3 -m venv ~/bowling-transcript-fetcher-venv   # skip if already created
source ~/bowling-transcript-fetcher-venv/bin/activate
pip install -r scripts/requirements-browser.txt
playwright install chromium
sudo ~/bowling-transcript-fetcher-venv/bin/playwright install-deps   # apt-installs system libraries Chromium needs -- requires sudo, run outside/after activation with the venv's own playwright binary

export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used everywhere else in this runbook>"
python3 scripts/home_transcript_fetcher_browser.py
```
Cron wrapper script should call `~/bowling-transcript-fetcher-venv/bin/python3`
by full path, same reasoning as 6j's wrapper.

**UNVERIFIED as of writing**: YouTube's DOM structure and class names for
the transcript panel aren't documented and drift over time, so the
selectors in this script are a best-effort starting point, not confirmed
against the real page. If a video comes back with `no_captions_available`
or `transcript_panel_found_but_text_extraction_returned_empty` and you
know that video genuinely has captions, check `scripts/debug/` -- the
script writes a screenshot (`.png`) and the full rendered page HTML
(`.html`) there on any failure to find the button or extract text. Share
those (or just describe what the screenshot shows near the "Show
transcript" button/panel) so the selectors can be corrected against real
evidence instead of another guess.

To watch it work instead of reading screenshots after the fact (useful
for the first real run, e.g. over VNC with a desktop environment on the
Pi):
```bash
TRANSCRIPT_FETCHER_HEADLESS=false python3 scripts/home_transcript_fetcher_browser.py
```

Cron, same pattern as 6j -- put env vars in a `chmod 700` wrapper script
rather than the crontab itself (700, not 600, since cron executes the
script directly and needs the execute bit; log to a path in your home
directory, not `/var/log`, which a normal user typically can't write to
without sudo):
```
0 7 * * * ~/run_browser_transcript_fetcher.sh >> ~/bowling-transcript-fetcher-browser.log 2>&1
```
where `run_browser_transcript_fetcher.sh` (`chmod 700 ~/run_browser_transcript_fetcher.sh`)
contains the same env-var-export pattern as 6j's wrapper script, but
calling `home_transcript_fetcher_browser.py` instead.

Same `needs_transcript` filtering as the plain-HTTP script applies here
too (via the shared `run()`) -- a candidate that already has a
`transcript_note` from a previous attempt (either fetcher) won't be
re-tried automatically. Clear it manually via `psql` (see 6j) to force a
recheck with the browser-based fetcher.

## 7. Ongoing operations

- **Check the DLQs periodically** (`bowling-scraper-product-scrape-dlq`,
  `-pdf-parse-dlq`, `-image-process-dlq`, `-woocommerce-product-scrape-dlq`,
  `-netsuite-product-scrape-dlq`, `-commercebuild-product-scrape-dlq`,
  `-video-summarize-dlq`, `-video-transcript-result-dlq`) -- a nonzero
  count means something's failing repeatedly, not just a transient blip
  (Lambda retries up to `maxReceiveCount` before landing there).
  `-video-summarize-dlq` now catches `video_transcript_fetcher` failures
  (it consumes that queue as of the split-architecture change) and
  `-video-transcript-result-dlq` catches `video_summarizer` failures.
- **SWAG, MOTIV, and commercebuild URL discovery have no automated
  schedule** even once their brand id parameters are set -- add a
  `Schedule` event to `WooCommerceUrlDiscoveryFunction`/
  `NetsuiteUrlDiscoveryFunction`/`CommercebuildUrlDiscoveryFunction` in
  `template.yaml` yourself once you're ready for them to run
  unattended (matching `UrlDiscoveryFunction`'s existing `rate(1 day)`
  pattern), or keep invoking manually. For
  `CommercebuildUrlDiscoveryFunction` specifically, keep the daily rate
  slow enough to respect stormbowling.com's `Crawl-delay: 10` -- the
  function's own inter-brand sleep already handles spacing *within* one
  invocation, a schedule just controls how often that whole invocation
  repeats.
- **Rotating the admin API token**: update the Secrets Manager secret's
  value; already-warm `AdminApiAuthorizerFunction` containers cache the
  old token for their remaining lifetime (see that module's docstring) --
  not instant revocation, by design, acceptable for a shared token.
- **`VideoDiscoveryFunction` has no automated schedule either**, same
  reasoning as the other discovery functions, plus a real quota reason:
  the confirmed 100-searches/day cap (70/invocation, see 6i) means "run it
  on everything every day" isn't actually sane math yet against a full
  catalog. Invoke it manually with
  an explicit `product_ids`/`brand_id` scope (see 6i) until you've decided
  how you actually want to spread coverage across the catalog over time.

## Troubleshooting quick reference

| Symptom | Check first |
|---|---|
| Admin API returns 401/403 even with the right token | `AdminApiAuthorizerFunction` logs; confirm `AdminApiTokenSecretArn` resolved and the secret's `token` field matches what you're sending |
| Admin API returns 500 | `AdminApiAuthorizerFunction` logs for a Secrets Manager error (bad ARN/missing IAM permission) -- this is a deliberate fail-closed path, not a bug in the 500 itself |
| Products never appear in the DB | `ProductScraperFunction` logs, then `bowling-scraper-product-scrape-dlq` |
| `info_sheet_url`/mass bias never populated | Confirm you're on the commit that fixed `parse_resources()`'s "Download"-link-text bug (see README) |
| MOTIV products never scrape | `bowling-scraper-netsuite-product-scrape-dlq`, then `fetch_page()`'s docstring in `netsuite_product_scraper/app.py` for next steps |
| MOTIV products all show `status = 'current'` | Real, confirmed, already-fixed incident -- see 6e.5. Confirm you're on the commit with `get_status_for_url()` in `netsuite_product_scraper/app.py`, redeploy, then run `scripts/backfill_netsuite_status.py` to correct any rows still wrong from before the fix |
| MOTIV products have unrelated/extra images attached | Real, confirmed, already-fixed incident -- see 6e.6. Confirm you're on the commit with the DOM-scoped `parse_images(soup, base_url)` in `netsuite_product_scraper/app.py`, redeploy, then run `scripts/rescrape_netsuite_products.py` to force a fresh scrape of every MOTIV product (no DB backfill applies here -- see 6e.6) |
| Images look cropped wrong | Pull a few from `ImageBucket` and eyeball against `image_processor/app.py`'s bbox-detection assumptions |
| BowlerDepot reconciliation reports nothing, ever | `CUSTOM_FIELD_NAME_CANDIDATES` mapping is probably wrong for your real store -- see step 6h |
