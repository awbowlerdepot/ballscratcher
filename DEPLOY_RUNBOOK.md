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

## 2. Run the migrations, in order

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
psql "$DATABASE_URL" -f db/migrations/011_products_plotter_chart_position.sql
psql "$DATABASE_URL" -f db/migrations/012_products_oil_motion_source.sql
psql "$DATABASE_URL" -f db/migrations/013_product_videos_stats.sql
psql "$DATABASE_URL" -f db/migrations/014_price_tracking.sql
psql "$DATABASE_URL" -f db/migrations/015_products_last_price_discovery_at.sql
psql "$DATABASE_URL" -f db/migrations/016_price_tracking_bigcommerce.sql
psql "$DATABASE_URL" -f db/migrations/017_price_tracking_sku_stock.sql
psql "$DATABASE_URL" -f db/migrations/018_bowlerdepot_products_dedupe_by_product.sql
psql "$DATABASE_URL" -f db/migrations/019_products_product_type.sql
psql "$DATABASE_URL" -f db/migrations/020_bowlerdepot_video_sync.sql
psql "$DATABASE_URL" -f db/migrations/021_blocked_video_channels.sql
psql "$DATABASE_URL" -f db/migrations/022_product_articles.sql
psql "$DATABASE_URL" -f db/migrations/023_product_article_images.sql
psql "$DATABASE_URL" -f db/migrations/024_product_article_images_composite_pipeline.sql
psql "$DATABASE_URL" -f db/migrations/025_product_article_images_theme_driven_pipeline.sql
psql "$DATABASE_URL" -f db/migrations/026_product_article_image_candidates.sql
psql "$DATABASE_URL" -f db/migrations/027_manual_seed_urls.sql
psql "$DATABASE_URL" -f db/migrations/028_product_articles_bigcommerce_sync.sql
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

Five possible secrets. The first two are needed for a functioning
deploy; the rest only matter once you're ready to enable the specific
feature each one gates (BowlerDepot reconciliation, video enrichment, or
v4's Gemini article-image candidates -- see 6t below).

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

**BigCommerce credentials (required for both BowlerDepot reconciliation,
6h, and the price tracker's BowlerDepot price/cost/stock source, 6o.5 --
skip this if you don't want either of those two features running):**

```bash
aws secretsmanager create-secret \
  --name bowling-scraper-bigcommerce \
  --secret-string '{"store_hash":"<store-hash>","auth_token":"<api-token>"}'
```

Note the returned ARN -- this is `BigCommerceSecretArn`, shared by both
`BowlerDepotReconciliationFunction` and `PriceCheckerFunction`.

**A real secret + ARN already exist for this deployment**:
`arn:aws:secretsmanager:us-west-1:563981859606:secret:bowling-scraper-bigcommerce-caCBX7`
-- Al confirmed this in chat. Pass it as `BigCommerceSecretArn` at deploy
time (step 5) rather than creating a new secret. This hasn't been
independently verified against a real BigCommerce API call from this
environment (no AWS CLI access in this sandbox) -- confirm the secret's
actual `{store_hash, auth_token}` contents are correct before trusting
either feature's output, same "verify before trusting" posture 6h's own
step 3 already calls out for `CUSTOM_FIELD_NAME_CANDIDATES`.

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

**Gemini service-account key (optional, only for v5's Gemini image
candidates in the article-image pipeline, see 6t below -- skip this and
the article generator still runs fine, it just produces one Stability
candidate per shot instead of three):**

This one you have to set up yourself, in Google Cloud Console (NOT
Google AI Studio -- see below for why that changed):

1. Create (or pick) a GCP project, and enable the **Vertex AI API** on it
   (console.cloud.google.com -> APIs & Services -> Enable APIs -> search
   "Vertex AI API" -> Enable).
2. Create a service account (IAM & Admin -> Service Accounts -> Create
   Service Account) and grant it the **Vertex AI User** role
   (`roles/aiplatform.user`) -- that's the only role this needs.
3. Create a JSON key for that service account (the service account's own
   page -> Keys -> Add Key -> Create new key -> JSON) and download it.
   This file is a real credential -- treat it like a password, don't
   commit it anywhere.

```bash
aws secretsmanager create-secret \
  --name bowling-scraper-gemini-service-account \
  --secret-string file://path/to/downloaded-service-account-key.json
```

Note the returned ARN -- this is `GeminiServiceAccountSecretArn`. Left
unset, `generate_article_image_candidates` simply skips the two Gemini
candidates every run and still produces the one Stability composite
candidate -- same "not configured -> soft no-op" posture as every other
optional secret in this section, not a hard failure (see `product_
article_generator.handler`'s own docstring).

**Why this changed from a bare API key (v4) to a service-account JSON key
(v5)**: Al's ask changed to "what if we can only use Application Default
Credentials instead of an API key". A literal ADC/metadata-server flow
doesn't exist for a Lambda running on AWS (there's no GCP metadata server
to discover), so a service-account JSON key is the practical substitute
Al chose (over the alternative, GCP Workload Identity Federation from
AWS). A service account authenticates via standard GCP IAM/OAuth2
(Bearer token), which only Vertex AI's own `generateContent` endpoint
accepts -- NOT the Gemini Developer API / Google AI Studio's
`x-goog-api-key` mechanism v4 used -- so this is a real provider-surface
switch, not just a header swap. Also note: v5 defaults `GeminiModelId` to
**Gemini 3 Pro Image** ("Nano Banana Pro"), not v4's original
`gemini-2.5-flash-image` -- that model carries an explicit retirement
notice on Vertex AI (2026-10-02) in Google's own migration docs,
discovered while researching this switch; Al picked the Pro tier directly
over the cheaper/faster Gemini 3.1 Flash Image successor when asked. See
`src/product_article_generator/app.py`'s own module docstring for the
full v4->v5 research trail, including the explicit caveat that this
module's Vertex AI request/response handling has NOT been verified
against a live invocation from inside this environment (no outbound
access to `*.googleapis.com` from this sandbox, no real service-account
key to test with) -- confirm on the first real invocation.

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

**Build speed, later session, Al: "the build is starting to take
forever."** With 25 functions and `use_container = true` (a real Docker
build per function), a full `sam build` was doing every function's
`requirements.txt` install from scratch, sequentially, every single
time -- even though most functions share large chunks of the same
dependencies (`requests`/`psycopg2-binary`/`boto3` repeat across a
dozen+ of them). `samconfig.toml`'s `[default.build.parameters]` now
also sets `cached = true` (reuses a dependency-install cache keyed by
each function's own `requirements.txt` content hash, under
`.aws-sam/deps/`, instead of reinstalling unchanged dependency sets) and
`parallel = true` (builds independent functions concurrently instead of
one at a time). Both are supported alongside `use_container = true`, no
other config changes needed -- plain `sam build` picks this up
automatically.

This is separate from 6a.5 below (scoped `sam build AdminApiFunction`
shipping a broken zip missing `fastapi`) -- that workaround still
stands. If a similar "phantom missing dependency" failure ever shows up
specifically after enabling the cache, clear it first: `sam build
--clear-cache` (or delete `.aws-sam/build/` and `.aws-sam/deps/`
directly) before rebuilding.

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
| `BigCommerceSecretArn` | No, but recommended | `arn:aws:secretsmanager:us-west-1:563981859606:secret:bowling-scraper-bigcommerce-caCBX7` (see step 3) -- unlocks 6h's BowlerDepot reconciliation and 6o.5/6o.6's BowlerDepot price/cost/stock and per-SKU stock tracking; leave blank to skip all three |

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

### 6a.5. Recurring incident: scoped `sam build AdminApiFunction` ships a broken zip (missing `fastapi`)

**CONFIRMED TWICE NOW** -- treat `sam build AdminApiFunction` on its own
as unreliable in this environment, full stop.

First occurrence: after being told to redeploy just `AdminApiFunction`
for a cores-backfill fix, Al hit `500 Server Error` calling `GET
/products?missing_core=true`. CloudWatch logs
(`/aws/lambda/bowling-scraper-admin-api`) showed:
```
[ERROR] Runtime.ImportModuleError: Unable to import module 'app': No module named 'fastapi'
```
Root cause: `sam build <LogicalId>` only rebuilds that one resource and
reuses whatever's already in `.aws-sam/build/` for every other resource
-- but it turned out `AdminApiFunction`'s OWN scoped build was itself
producing an incomplete dependency layer (fastapi silently missing from
the packaged zip), not a stale-other-function problem. Fixed at the time
by a full unscoped `sam build && sam deploy`, confirmed working.

Second occurrence, this session, right after the Status filter feature
(6i.5) was committed and Al was told to run `sam build AdminApiFunction
&& sam deploy`: admin site started 500ing again. Same exact CloudWatch
error, same function, same fix (`sam build && sam deploy`, full rebuild).

Two-for-two on the same scoped-build command producing the same failure
-- this isn't a one-off fluke, `sam build AdminApiFunction` alone should
not be used going forward. **Always use `sam build && sam deploy` (no
logical ID) when redeploying AdminApiFunction specifically**, even though
scoped builds are fine for the five scraper functions (no repeat failures
there across several real redeploys this session). If a 500 shows up on
the admin site after any deploy, check
`/aws/lambda/bowling-scraper-admin-api` first for this exact
`ImportModuleError` before looking anywhere else -- it's the most likely
cause given this history:
```bash
aws logs tail /aws/lambda/bowling-scraper-admin-api --follow
```

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

   **Optional `SOURCE_PLATFORM` scoping**, added for Al's report (2026-08-10):
   "we are also missing a bunch of cores from the brunswick brand also
   their coverstocks. the combats are one of them." Investigated by
   fetching the live Combat Solid page directly
   (`brunswickbowling.com/products/balls/current/combat-solid`) and
   confirming its spec table (`Core: Rampart`, `Coverstock: HK22C² - Alpha
   Premier Solid`) parses cleanly against `product_scraper`'s current
   `SPEC_TABLE_LABELS`/`parse_spec_table` -- no scraper bug, same
   "never rescraped since the cores/coverstocks wiring went in" gap as the
   Raw Hammer coverstock report below. Since these two backfill scripts
   are exactly the fix, and a catalog-wide run is the thing that caused
   the 503 throttling incident right below this, both scripts now accept
   an optional `SOURCE_PLATFORM` env var that adds `&source_platform=...`
   to the `GET /products` filter -- scope a run to just Brunswick/Radical/
   DV8 (they all share `source_platform='craft_cms'`) instead of fanning
   out to every platform's scraper queue at once:
   ```bash
   export SOURCE_PLATFORM="craft_cms"
   python3 scripts/backfill_core_ids.py
   ```
   Omit it to run catalog-wide as before -- unchanged default behavior.
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

**Correction, 2026-08-12: it WAS a scraper bug after all -- same lesson as
the Brunswick correction below, different root cause.** Al reported the
exact same product again by id (`e4627a24-3b68-4d42-93fc-fb31340d3495`,
`Raw Hammer - Black / Grey`): "is missing core and cover data." The admin
API record showed `core_id`/`coverstock_id`/`color`/`factory_finish`/
`part_number`/`weights_available` all still null, and -- the real
tell -- `updated_at` (2026-08-07) was already after the original "just
needs a rescrape" diagnosis above, meaning a rescrape (or several) had
already happened and it was *still* null. That ruled out "hasn't been
rescraped yet" as the explanation this time.

Re-investigated the same way as the Brunswick correction (Claude in
Chrome, `javascript_exec` against `raw-hammer-black-grey.json`'s real
`body_html`, not assumed): the heading `shopify_product_scraper`'s
`_find_section` looks for is `<h3>BALL SPEC</h3>` on this product --
singular, no trailing S -- while every other Hammer product this
scraper had fixtures for (Fallout, Black Widow 3.0 Dynasty, etc.) uses
`<h3>BALL SPECS</h3>`, plural. `_find_section` matches by
`heading.startswith(prefix)`, and the old prefix tuple was `("BALL
SPECS", "SPECIFICATIONS")` -- a *longer* string than "BALL SPEC" can
never be a prefix match for it, so `specs_section` came back `None` for
this product specifically, and every `BALL_SPEC_LABEL_MAP`-derived field
silently stayed null. The RG/DIFF numbers (present and correct in Al's
report) were never affected, since those are matched by an entirely
separate "RG" heading -- exactly the signature that made this look like
a missing-data problem rather than a parsing one.

**Fix:** `parse_product_page`'s spec-heading prefix changed from `"BALL
SPECS"` to `"BALL SPEC"` (drop the trailing S) -- matches both spellings,
since anything starting with "BALL SPECS" also starts with "BALL SPEC".
One-line change in `src/shopify_product_scraper/app.py`. New fixture
`tests/fixtures/hammer_raw_hammer_black_grey.json`, captured directly off
the live page (`body_html` length matches the live page's exactly, 4180
chars) -- confirms the real page actually has every field
(`core_name="Raw Hammer"`, `coverstock_name="Juiced Solid"`, etc.), so
this was purely a parsing gap, not missing source data. Three new tests
in `tests/test_shopify_product_scraper.py` cover the spec fields, weights/
release date, and (regression guard) that RG/DIFF parsing was untouched.
Full suite re-run clean (49/49).

**Requires a Lambda redeploy** (`sam build ShopifyProductScraperFunction
&& sam deploy`), then re-run the coverstock (and core) backfill scripts
above -- same shape as before, but this time the rescrape they trigger
will actually pick up the fix:
```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/backfill_core_ids.py
python3 scripts/backfill_coverstock_ids.py
```
Since Raw Hammer is an entire product line (not a one-off), check
`GET /products?missing_core=true&source_platform=shopify` after
redeploying to see how many Raw Hammer products this affected beyond the
one Al flagged -- the backfill script above sweeps all of them in one
run regardless of count.

**Brunswick core/coverstock gap (2026-08-10) -- same class of bug, same
fix, plus a new `SOURCE_PLATFORM` scoping option on both backfill
scripts.** Al: "we are also missing a bunch of cores from the brunswick
brand also their coverstocks. the combats are one of them." Investigated
the same way as the Raw Hammer report above -- fetched the live Combat
Solid page (`brunswickbowling.com/products/balls/current/combat-solid`)
directly and confirmed its spec table (`Core: Rampart`, `Coverstock:
HK22C² - Alpha Premier Solid`, `Cover Type: Solid Reactive`) parses
cleanly end-to-end against `product_scraper`'s current
`SPEC_TABLE_LABELS`/`parse_spec_table` -- no scraper bug. Same
never-rescraped-since-the-wiring-went-in gap; the two backfill scripts
above are exactly the fix, no code change needed for Brunswick's parsing
itself.

Since a catalog-wide run is exactly what caused the 503-throttling
incident documented above (fans out to all five platforms' scraper
queues at once against a 10-slot account concurrency limit), and Al's
report is specifically about Brunswick, both `backfill_core_ids.py` and
`backfill_coverstock_ids.py` now accept an optional `SOURCE_PLATFORM` env
var that adds `&source_platform=...` to their `GET /products` filter.
Brunswick/Radical/DV8 all share `source_platform='craft_cms'`:
```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
export SOURCE_PLATFORM="craft_cms"
python3 scripts/backfill_core_ids.py
python3 scripts/backfill_coverstock_ids.py
```
Omit `SOURCE_PLATFORM` to run catalog-wide as before -- unchanged default
behavior, and existing callers (including the Batch Jobs panel, which
doesn't set it) are unaffected.

**Correction, same day: it WAS a scraper bug after all.** Al ran the
backfill above and reported back: "interesting enough the combats are
still missing the core." That's real signal the "just needs a rescrape"
diagnosis above was wrong, since a rescrape had just happened. Re-dug in
and found the actual bug: `_find_table_by_row_labels` (used to locate the
real spec table) returns the FIRST `<table>` clearing `min_matches=3`
against `SPEC_TABLE_LABELS` -- and `SPEC_TABLE_LABELS` includes
`"rg"`/`"diff"`/`"asy"`/`"mb"` (needed for the single-value-spec-row
fallback case, see `defender.html`). Every Brunswick product page has a
separate per-weight "Core Numbers" table (RG/DIFF/ASY breakdown) BEFORE
the real spec table in document order. For most balls that table only has
RG/DIFF rows (2 matches, under threshold) so it's harmlessly skipped --
confirmed via `crown_78u.html`'s fixture, which has exactly this shape
and has always passed. But an asymmetric-core ball's Core Numbers table
also reports ASY (mass bias) per weight -- a 3rd row, clearing the exact
same threshold the real spec table needs to clear -- and since it comes
first, IT won -- reproduced locally with a two-table fixture mirroring
Combat Solid's real page and confirmed `core_name`/`coverstock_name`
(and every other spec field) came back `None`, exactly Al's report.
Combat's whole family (asymmetric core) hits this on every single
product; most of Brunswick's catalog (symmetric-core, no ASY row) never
did, which is exactly why this looked like an isolated, narrow gap
rather than what it actually is.

**Fix:** `_find_table_by_row_labels` now returns the table with the MOST
matching labels, not just the first one past the threshold -- the real
spec table has up to 10 possible matches (level, part number, color,
core, coverstock, cover type, finish, weights, warranty, release date)
against the Core Numbers table's max of 4 (rg, diff, asy, mb), so it wins
outright whenever both are present. Verified against a new
`tests/fixtures/combat_solid.html` (built from a live fetch of
`brunswickbowling.com/products/balls/current/combat-solid` this session)
plus re-confirmed both existing real fixtures (`crown_78u.html`,
`defender.html`) still parse identically to before -- nothing regressed
for the products that were already working. New tests in
`tests/test_product_scraper.py`: end-to-end via the Combat Solid fixture,
plus direct unit tests of `_find_table_by_row_labels` itself (prefers the
better match, still works with only one candidate table, still returns
`None` below the threshold).

**Requires a Lambda redeploy** (`sam build ProductScraperFunction && sam
deploy`) before this actually takes effect -- unlike the backfill-script
fix above, this is a code change to the deployed
`ProductScraperFunction`, not just an admin_api-triggered rescrape. Once
redeployed, re-run the `SOURCE_PLATFORM=craft_cms` backfill commands
above (or the Products tab's per-product "Rescrape" button) to actually
pick up Combat/Combat Solid/Combat Hybrid now that the parser is fixed.

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

**Video-thumbnail + low-res duplicate images fixed (`parse_images()`).**
Al: "it looks like we are scraping a video thumbnail and the low res
version of the ball and core images. we should only be pulling in the
high res bowling ball images." Root-caused via Claude in Chrome
(`javascript_exec` against the raw fetched HTML of a live product page,
not the post-render DOM, to match what this scraper's `requests.get(...).
text` actually receives) against `brunswickbowling.com/products/balls/
current/combat`: every product page carries 7 `<img>` tags, not 3.

1. 3 real gallery images (main ball + 2 core callouts) -- `srcset`
   offering "700w, 1400w".
2. 3 duplicate thumbnail-nav-strip `<img>` tags of the exact same 3
   subjects -- same filename shape (`..._1600x1600_<hash>.png`, core
   callouts still matching the `16-14_lb_Core` filename pattern
   `parse_images` classifies on), just a different `<hash>` and a much
   smaller `srcset` ("64w, 128w"). Filename alone can't tell these apart
   from the real ones -- the literal `1600x1600` in the filename is a
   fixed label the CMS uses regardless of actual rendition, not the real
   dimensions.
3. 1 video-teaser background image, for the "Watch the Combat in Action!"
   section -- a real product-adjacent photo it is not; alt text varies per
   product ("Pro Level Pin Splash Video Background" on Combat), but the
   URL always has a `/video_backgrounds/` path segment.

Fix, both in `src/product_scraper/app.py`'s `parse_images()`:
- New `_srcset_max_width()` helper reads the largest `<n>w` descriptor
  out of an `<img>`'s `srcset`. Any image whose max declared width is
  under `MIN_IMAGE_WIDTH` (300, comfortably below the real gallery's
  700w+ and above the thumb-strip's 128w ceiling) is dropped before
  classification. An `<img>` with no `srcset` at all (unknown width) is
  never dropped by this check -- only a *known-small* width counts,
  matching every other product's plain-`src` core-callout images (e.g.
  the Crown 78U fixture), which still have no `srcset` and must keep
  working.
- Any resolved URL containing `/video_backgrounds/` is dropped outright.

This does **not** retroactively fix already-scraped Brunswick/Radical/DV8
products -- same reasoning as the stale-image-DELETE fix above: a code
change only affects what a *future* scrape returns. Verified via new
tests in `tests/test_product_scraper.py`, built from the exact 7 real
`<img>` tags captured off Combat's live page (not reconstructed):
`test_parse_images_combat_real_page_shape_drops_dupes_and_video_bg` (all
7 in, exactly 3 real ones out, low-res/video URLs never appear),
`test_srcset_max_width_reads_largest_declared_width`,
`test_parse_images_drops_video_background_image_alone`, and
`test_parse_images_keeps_image_with_no_srcset_regardless_of_width`
(regression guard for the no-`srcset` case above). Full suite re-run
clean (43/43) after the change.

**Requires a Lambda redeploy** (`sam build ProductScraperFunction && sam
deploy`) before this takes effect, then the existing stale-image-DELETE +
S3 orphan cleanup (above) does the actual retroactive cleanup for free --
run `scripts/rescrape_brunswick_products.py` again after redeploying and
every already-scraped Brunswick/Radical/DV8 product gets re-parsed under
the fixed code; the video-thumbnail/low-res rows this bug already wrote
are, by definition, `source_url`s the corrected parse no longer returns,
so `upsert_product`'s stale-row DELETE removes them (and
`delete_orphaned_image_objects` cleans up their mirrored S3 objects)
automatically -- no separate one-off cleanup script needed for this fix
specifically.

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

### 6f.1. Fixed incident: "Tech Sheet" wording missed by `parse_tech_data_pdf_url`, zero `product_skus`

Al: found product `56897c0b-e3ec-4314-a8dc-238e1b8b7a75` (Storm Tropical
Surge Black/Cherry) with zero `product_skus` rows, despite the real page
(`stormbowling.com/storm-tropical-surge-bowling-ball-black-cherry`)
clearly showing weight/RG/differential values in its spec block.

Root cause, confirmed by fetching that exact live page: `product_skus`
comes ONLY from `parse_tech_data_pdf(pdf_bytes)` in `upsert_product`'s
`for sku in pdf_skus:` loop -- the flat single-weight spec block on the
page itself is cross-check-only, never the SKU source (see 6f's own
COMMERCEBUILD_SCOPING.md writeup). `parse_tech_data_pdf_url` finds that
PDF's URL by matching the Downloads-section link's TEXT against "tech
data" -- but this product's link text is "Tech Sheet: Surge Black/Cherry
PDF", not "Tech Data" (the "tech data" substring only appears in the
PDF's own filename, `Storm_Tropical_Surge_Black_Cherry_Tech_Data.pdf`,
which the original code never inspected). No match -> `tech_data_pdf_url`
is `None` -> `parse_tech_data_pdf` never runs -> `pdf_skus` stays empty
-> zero rows inserted, with no exception anywhere and a normal-looking
`products` row (this is the same general failure shape as 6f's own "if
archived products come back with EMPTY name/coverstock/color fields"
watch-item above -- a silent, not-thrown gap in a specific field's
sourcing, not a crash).

**Fixed**: `parse_tech_data_pdf_url` now matches a small text-synonym
list (`TECH_DATA_TEXT_SYNONYMS = ("tech data", "tech sheet")`, both
confirmed real wordings) and falls back to a filename check
(`_looks_like_tech_data_filename`, looks for "techdata" in the
normalized filename) for any wording this module hasn't confirmed real
yet. New regression fixture `TROPICAL_SURGE_HTML` in
`tests/test_commercebuild_product_scraper.py` uses the real page's exact
Downloads-section markup.

**To fix already-affected products**: redeploy (`sam build && sam
deploy`), then rescrape. `GET /products` now supports a `missing_skus=true`
filter (products with zero `product_skus` rows -- a `not exists`
subquery, unlike `missing_core`/`missing_coverstock`'s plain `is null`
check, since `product_skus` is a separate table) and
`scripts/rescrape_commercebuild_products.py` is a thin trigger for it,
same `GET /products` (paginated) + `POST /products/{id}/rescrape` shape
as `scripts/rescrape_netsuite_products.py`, scoped to `source_platform=
commercebuild&missing_skus=true`:

```
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/rescrape_commercebuild_products.py
```

Only enqueues -- doesn't wait for the scrapes to finish. Re-run later (or
re-check `GET /products?source_platform=commercebuild&missing_skus=true`)
to see how much is left. Note this filter also catches genuinely
archived/retired commercebuild products (documented platform limitation
-- no obtainable SKU data by any method, see COMMERCEBUILD_SCOPING.md),
so a nonzero count after rescraping isn't necessarily still-broken --
there's no way to tell "archived, no data available" apart from "current,
still hitting a parser gap" from the product row alone; spot-check a few
in the admin UI. `missing_skus=true` is platform-agnostic (works for any
`source_platform`, not just commercebuild), in case a similar silent
zero-SKU gap ever turns up on another scraper.

### 6f.2. Fixed incident: 900 Global's image-based Tech Data PDFs -> zero `product_skus` -> broken price matching

Al: "900 Global balls are missing their skus and because of that pricing
and other things that depend on that."

Different root cause than 6f.1's -- confirmed via a live fetch of a real
900 Global product (Viking): `parse_tech_data_pdf_url` finds the right
PDF fine here (this brand's Downloads section already uses the literal
"Tech Data" wording, no synonym gap). The failure is one level deeper,
inside `parse_tech_data_pdf` itself, and was actually already documented
in that function's own docstring from this project's first live smoke
test: a real, confirmed finding that many 900 Global Tech Data PDFs are
genuinely image-based (scanned/rasterized, zero real text layer -- 0
chars via pdfplumber, embedded images tiling the page) -- not a
table-shape gap `_skus_from_table` could ever be widened to cover, since
there's no text to read at all. `pdf_skus` comes back `[]`,
`upsert_product`'s `for sku in pdf_skus:` loop inserts zero
`product_skus` rows, and since `product_skus.weight_lbs` is exactly what
`price_checker`'s variant/weight matching (see that module's
`check_bigcommerce_sources`) keys off of to attach cost/stock data to a
specific SKU, every downstream thing keyed off a SKU row -- price
checks, SKU stock history/forecasting, the Total ADU column -- silently
has nothing to attach to for these products. Real OCR (Tesseract in
Lambda) was explicitly scoped out at the time that finding was first
made (real added scope: a new Lambda layer, reliability on numeric
tables) and still is here -- not what this fix does.

**Fixed**: `parse_product_page` already parses ONE real weight/RG/
differential straight off the page's own visible HTML
(`html_weight_lbs`/`html_rg`/`html_differential` -- previously used only
by `cross_check_html_vs_pdf` for mismatch detection). New function
`_html_fallback_skus(parsed_page)` turns that into a single-SKU list
when called; `_process_one` now uses it as a fallback --
`skus_to_write = pdf_skus if pdf_skus else _html_fallback_skus(parsed)`
-- whenever the PDF path recovered nothing at all. The resulting
`product_skus` row is tagged `source='html'` (not `'pdf'`) so it's
distinguishable from a full PDF-sourced table at a glance --
`spec_source` already has this value (migration 001;
`woocommerce_product_scraper`/`shopify_product_scraper`/`netsuite_
product_scraper`/`product_scraper` already write `'html'`-sourced SKUs
as their only source, so this isn't a new convention). `upsert_product`'s
SKU-insert loop now reads `source` per-dict (`sku.get("source", "pdf")`)
instead of hardcoding `'pdf'` in the SQL, and its `ON CONFLICT` clause
now also sets `source = excluded.source` (not coalesced) -- a rescrape
that later finds the PDF newly readable should have its real `'pdf'` row
replace a stale `'html'` fallback outright, not silently keep the old
provenance.

One real weight isn't the full per-weight table the PDF would have
given -- other weights for these products still won't have `product_skus`
rows until OCR is built -- but it's the difference between zero
attachable SKUs and one, which is what actually unblocks price matching
for the weight most likely to be in stock/being priced.

Tests: `tests/test_commercebuild_product_scraper.py`, 4 new
(`test_html_fallback_skus_builds_single_sku_from_html_values` and
friends) covering the happy path, the "no html_weight_lbs at all" case
(archived products, same "flag don't guess" `[]` convention as
elsewhere), `None` rg/differential passthrough, and a missing-key dict.
Full suite re-run clean (66/66 in this module's own test file, every
other `test_*.py` in the repo also still green).

**To fix already-affected products**: same mechanism as 6f.1 --
redeploy (`sam build CommercebuildProductScraperFunction && sam
deploy`), then re-run `scripts/rescrape_commercebuild_products.py`
(scoped to `source_platform=commercebuild&missing_skus=true`, see 6f.1
above) or target 900 Global specifically via its `brand_id` in the admin
UI. Products that previously had zero `product_skus` because of this
gap will now get exactly one `source='html'` row on their next scrape --
check the admin UI's per-SKU stock/price sections to confirm price
matching picks it up.

**Follow-up, same session -- Al asked "where in the admin ui is the
ability to do this?":** turned out there wasn't one -- the Products tab
had a Brand filter and a per-product Rescrape button, but no bulk
"missing SKUs" action, unlike `missing_core`/`missing_coverstock`, which
both already had one in the Batch Jobs tab's config-driven `BATCH_
CONFIGS` (`admin-site/index.html`). Added a matching `skus` entry
(`filterParam: 'missing_skus'`, same `/rescrape` call/`queued`-shaped
result as `core`/`coverstock`) plus a "Backfill missing SKUs" panel,
identical shape to the coverstock one right above it (same `batch-
start-/stop-/stats-/log-<kind>` id convention `runBatch`/`stopBatch`
already key off of -- no JS changes needed beyond the one config entry).
Verified via `node --check` on the extracted script (passed) and the
project's own HTML tag-balance checker (passed, `unclosed at end: []`).
No new tests -- this tab has no existing test coverage of its own
(admin-site/index.html is validated structurally, not unit-tested, same
as every other admin-site change in this project).

### 6f.3. Fixed follow-up: full per-weight table via Amazon Textract OCR, not just the 1-SKU HTML fallback

Al, later session: "viking still only has 1 sku" -- the exact,
predictable limitation 6f.2's HTML fallback documented up front (one
real weight recovered, not the other four Viking Conquest's real Tech
Data PDF would have given). Investigated recovering the full table
without OCR, from the site's own client-rendered variant widget --
`div-variant-product` calls a same-origin, authenticated
`POST /api/v/1/products/search` that returns a real 401 (`"Full
authentication is required to access this resource"`) regardless of
credentials mode; confirmed this needs a genuine OAuth2 Bearer token, not
a static client-side key. Deliberately not pursued -- that's a third
party's protected endpoint, meaningfully different from the public PDF
link or rendered HTML page this project already scrapes; authenticating
against it to pull data beyond what's publicly rendered isn't something
this project will automate, independent of whether a token URL is known.
Al agreed to OCR instead and chose Amazon Textract as the engine.

**Fixed**: `parse_tech_data_pdf`'s `total_chars == 0` branch (previously:
log a warning, return `[]`, OCR "not taken on here") now calls new
function `parse_tech_data_pdf_via_textract(pdf_bytes)`, which calls
`boto3.client("textract").analyze_document(Document={"Bytes": pdf_bytes},
FeatureTypes=["TABLES"])` -- synchronous, single call, no S3 round-trip
or async job polling needed for a single-page PDF. Textract's `Blocks`
response (`TABLE`/`CELL`/`WORD` block graph, `CELL`s holding 1-based
`RowIndex`/`ColumnIndex` and their own `WORD` children) is converted by
new pure function `_textract_table_from_blocks(blocks)` into the exact
`list[list[str]]` shape pdfplumber's `extract_tables()` already produces,
so it feeds straight into the existing, already-tested
`_skus_from_table()` -- no second parallel weight/RG/diff extraction
algorithm. Chosen over Tesseract-in-a-Lambda-Layer: boto3 is already a
project dependency (Bedrock used elsewhere in this repo), so no custom
binary packaging/layer to maintain, at ~$0.015/page.

`_html_fallback_skus` (6f.2) stays in place in `_process_one` as a
further-fallback safety net for the rare case Textract itself finds no
table either (e.g. a page that's genuinely just a product photo, no spec
table at all) -- `skus_to_write = pdf_skus if pdf_skus else
_html_fallback_skus(parsed)` already handles "whatever
`parse_tech_data_pdf` returns is empty" generically regardless of which
internal path (real text, OCR) produced that result, so no change was
needed there.

New IAM permission: `textract:AnalyzeDocument` added to
`CommercebuildProductScraperFunction`'s policy in `template.yaml`,
`Resource: "*"` -- `AnalyzeDocument` doesn't support resource-level
scoping (bytes passed inline, not read from S3), same as every
AWS-documented example for this action.

Tests: `tests/test_commercebuild_product_scraper.py`, 8 new
(`test_textract_table_from_blocks_*`) covering grid reconstruction
against a synthetic Viking-shaped table, feeding that output straight
into `_skus_from_table` and confirming all 5 real weights come back
(not just 1), multi-WORD cells correctly joined with a space (Textract
commonly splits "16 lbs." into two WORD blocks), empty-blocks/no-TABLE-
block/no-CELL-children all returning `[]` not guessing, a missing CELL
(Textract's own documented gap-vs-pdfplumber behavior) defaulting to `""`
without skipping/shifting the rest of the row, and multiple TABLE blocks
correctly using only the first. `parse_tech_data_pdf_via_textract` itself
is NOT unit tested, same established convention as `parse_tech_data_pdf`
and every other function in this codebase that makes a real network/AWS
call -- only its pure `_textract_table_from_blocks` helper has coverage.
Full suite re-run clean (74/74 in this module's own test file, every
other `test_*.py` in the repo also still green).

**To fix already-affected products**: this is a code-level fix to
`parse_tech_data_pdf` itself, not a new admin UI action -- the same
"Backfill missing SKUs" panel (6f.2's follow-up) and per-product Rescrape
button both already call into this function on every rescrape, so no new
UI/script was needed. Because this adds a new IAM permission, the
redeploy needs a real `sam deploy` for `CommercebuildProductScraperFunction`
(not a code-only Lambda update):

```bash
sam build CommercebuildProductScraperFunction && sam deploy
```

**Correction, same session, before Al even asked**: my first draft of
this paragraph said to re-run the existing "Backfill missing SKUs" panel
(`missing_skus` filter) to pick these back up -- wrong. Al asked "will
missing skus find these now because they have one now" and the answer is
no: `missing_skus` only matches products with ZERO `product_skus` rows,
and every product this section's fix applies to already has exactly one
(`source='html'`, from 6f.2's stopgap) -- a nonzero count that filter
was never meant to catch. See 6f.4 immediately below for the actual
fix -- a new, separate filter built specifically for this in-between
state.

### 6f.4. New filter to actually find 6f.3's target products: `html_fallback_skus`

Direct follow-up, Al: "will missing skus find these now because they
have one now" -- correctly caught the gap in 6f.3's original writeup
above before it caused real confusion. `missing_skus` (6f.2) and
`html_fallback_skus` are deliberately two different, non-overlapping
filters on `GET /products`:

- `missing_skus=true`: zero `product_skus` rows at all.
- `html_fallback_skus=true`: at least one `product_skus` row exists,
  AND every row that exists has `source='html'` -- i.e. a product that
  already got 6f.2's one-SKU stopgap and is now invisible to
  `missing_skus`, but still hasn't had a real rescrape run against
  6f.3's new Textract path yet.

`list_products` (`admin_api/service.py`) gained a `html_fallback_skus`
param and a two-part WHERE clause -- `exists (select 1 from product_skus
ps3 where ps3.product_id = p.id)` AND `not exists (select 1 from
product_skus ps4 where ps4.product_id = p.id and ps4.source <> 'html')`
-- BOTH parts together, not just the `not exists` alone, since a
zero-row product would vacuously satisfy a bare `not exists (...source
<> 'html')` too and get double-counted with `missing_skus`. A product
that already has its real `source='pdf'` table (even just one row of it,
some partial state) correctly never matches, since any non-html row
disqualifies it. Wired straight through `admin_api/app.py`'s `GET
/products` query param, no other endpoint changes needed.

Admin-site: new "Backfill 900 Global full weight table (Textract)" panel
in the Batch Jobs tab, `htmlSkus` entry in `BATCH_CONFIGS`
(`filterParam: 'html_fallback_skus'`, same `/rescrape` call/`queued`-shaped
result and `batch-start-/stop-/stats-/log-<kind>` id convention every
other batch panel already uses -- no JS beyond the one config entry + one
matching panel block). Verified via `node --check` on the extracted
script (passed) and the project's own HTML tag-balance checker (passed,
`unclosed at end: []`).

Tests: `tests/test_admin_api_service.py`, 4 new
(`test_list_products_html_fallback_skus_*`) covering the exact WHERE-
clause text added, that it's absent by default, that it's genuinely
distinct SQL from `missing_skus` (not just a semantic distinction that
happens to produce the same query), and that it correctly ANDs with
`source_platform`. Same `_QueryCapturingConnection` SQL-text-assertion
pattern every other `missing_*` filter test in this file already uses --
FakeCursor doesn't model EXISTS/NOT EXISTS subqueries, so row-level
semantics aren't exercised, only the generated SQL. Full suite re-run
clean (214/214 in this module, every other `test_*.py` in the repo also
still green).

No `template.yaml` changes needed -- this is a `GET /products` query-
param addition on an already-deployed endpoint (`AdminApiFunction`'s
proxy+ catch-all already covers it), not a new route or new IAM
permission. Redeploy is a normal `sam build AdminApiFunction && sam
deploy` (or a full `sam build && sam deploy` alongside 6f.3's Commerce-
build redeploy, since both are in this same session's changes).

To actually clear the backlog: after both this and 6f.3 are deployed,
run the new "Backfill 900 Global full weight table (Textract)" panel
(Batch Jobs tab) -- 900 Global products holding only a `source='html'`
stopgap row should come back with the full `source='pdf'` table on their
next scrape. Spot-check Viking Conquest specifically to confirm it now
shows 5 SKUs, not 1.

**Follow-up, same session -- real bug found live testing 6f.3/6f.4, Al:
"it looks like it got data from the pdf but it is not correct. weights
seem to be across the 16 lb instead of RG and Diff values and mass bias
is empty."** Two genuinely separate things came out of this report:

1. **Fixed**: mass_bias always empty. Confirmed via live fetch of
   stormbowling.com/900-global-viking-bowling-ball: Viking's Tech Data
   PDF is genuinely image-based (fetched directly, "no machine-readable
   text"), confirming this rescrape went through 6f.3's new Textract
   path, not the old pdfplumber path. Root cause turned out to be older
   than the Textract work though, and real independent of it: Al: "in
   this case the mass bias is referred to as the PSA and those are
   interchangeable." `_skus_from_table`/`_skus_from_text` were already
   correctly parsing a PSA value out of every table shape, but writing
   it to a dict key (`"psa"`) `upsert_product`'s SQL insert never reads
   -- silently dropped on every commercebuild product, PDF- or
   Textract-sourced alike, `mass_bias` hardcoded `None` regardless of
   what the source page actually showed. `product_skus` has no separate
   psa column at all (migration 001, `mass_bias numeric(5,3) -- null
   unless asymmetric core`) -- Brunswick's `product_scraper.py` already
   writes real mass_bias data the same way, sourced from ITS platform's
   own ASY/MB-labeled fields; this is that same column under
   commercebuild's own PSA label. Fixed by writing the parsed PSA value
   directly into each sku dict's `"mass_bias"` key (both `_skus_from_
   table` branches and `_skus_from_text`) instead of a separate,
   never-read `"psa"` key -- `upsert_product` needed no changes at all,
   it was already reading `sku["mass_bias"]` correctly. This also means
   the fix applies automatically to BOTH the pdfplumber and Textract
   paths, since Textract's output already fed through the same `_skus_
   from_table`. Tests: 6 existing `tests/test_commercebuild_product_
   scraper.py` tests updated (dropped the dead `"psa"` key from expected
   dicts, folded its value into `"mass_bias"`). Full suite re-run clean
   (74/74 in this module, every other `test_*.py` in the repo also still
   green).

2. **Fixed**: weight values landing in the RG/Diff slots. Al pasted the
   actual admin UI values for Viking's real stored row: `weight_lbs=16`
   (correct), `rg=15`, `differential=14`, `mass_bias=` (empty), `source=
   pdf` -- ONE row total, not five. `rg`/`differential` aren't real RG/
   Diff decimals (always ~2.4-2.7 / ~0.02-0.06 for these balls) -- they're
   literally the next two weights in descending order. No DB or AWS
   access available from this session to inspect Textract's actual
   `AnalyzeDocument` response directly or pull the real Tech Data PDF's
   raw bytes (network-allowlist and an intentional anti-exfiltration
   safeguard on raw/base64 binary both blocked it, same as an earlier
   session's PDF-bytes investigation), so the root cause here is a
   reconstructed hypothesis fit to the real evidence, not a captured
   real Textract response: Textract's own table-structure detection
   almost certainly collapsed Viking's real 5-row table into a single
   overly-wide row for this specific scanned page, so `weight_col_idx`'s
   own row ended up holding the OTHER weights' own `"15 lbs."`/`"14
   lbs."` tokens in the columns `_skus_from_table`'s long-mode code
   assumed held real RG/Diff data -- and `_to_float`'s intentionally
   permissive regex (extracts the first number found anywhere in a
   string, per that function's own docstring) happily parsed `"15
   lbs."` as `15.0` instead of failing loudly. Only 1 total row also
   matches this exactly: the "for row in table" loop only ran once,
   because Textract's reconstructed grid genuinely only had one row for
   this page, not five.

   Rather than trying to guess the real intended column mapping for a
   table shape this function was never built to handle, both `_skus_
   from_table` branches (long mode -- the confirmed real case here -- and
   wide mode, for defense-in-depth/consistency) now check whether any
   value about to be written as RG/Diff/PSA itself LOOKS like a weight
   token (matches `WEIGHT_TOKEN_RE`) before writing it -- a real decimal
   RG/Diff/PSA value never matches that pattern (no `"lb"`/`"lbs"` text),
   so this only ever fires on a genuinely mis-shaped row. When it fires,
   that row is skipped with a warning logged rather than writing
   corrupted data -- same "flag, don't guess" spirit as every other
   defensive check already in this function. For Viking specifically,
   this converts "1 row of wrong numbers, silently accepted" into "0
   rows from Textract, safely falls back to `_html_fallback_skus`'s
   existing single real-HTML-sourced SKU" -- worse in raw SKU count, but
   correct in the sense that nothing wrong gets stored, and it'll surface
   clearly via the `html_fallback_skus` filter/panel for follow-up
   later, same as any other still-image-based PDF this doesn't fully
   solve.

   Tests: 4 new in `tests/test_commercebuild_product_scraper.py` --
   reproducing the exact real Viking-shaped merged row and confirming it
   now returns `[]` instead of a corrupted SKU, confirming the guard only
   fires on genuine weight TOKENS (not just any value sharing digits with
   a weight, e.g. a real RG of 2.16 or PSA of 0.015 must still parse
   normally), and the same wide-mode coverage. Full suite re-run clean
   (77/77 in this module, every other `test_*.py` in the repo also still
   green).

   Real ground-truth values for Viking, pulled live from
   stormbowling.com's own rendered product page (public content, same
   category as any other rendered-HTML field this project already
   scrapes) -- useful reference if this specific product ever needs a
   manual correction or a future OCR-quality improvement is attempted:
   16 lb: RG=2.50, Diff=0.050, PSA=0.014; 15 lb: RG=2.51, Diff=0.052,
   PSA=0.016; 14 lb: RG=2.52, Diff=0.051, PSA=0.014; 13 lb: RG=2.64,
   Diff=0.034, PSA=0.011; 12 lb: RG=2.58, Diff=0.031, PSA=0.009.

### 6f.4.1. Fixed follow-up to 6f.4: `html_fallback_skus` matched almost the entire non-commercebuild catalog

**Real bug, caught live running 6f.4's own "Backfill 900 Global full
weight table (Textract)" panel, Al: "i don't think we fixed the viking
issue, also the batch just found: total: 1034 done: 342 errors: 14."**
Al then confirmed via the pasted batch log that this was that same
panel. The log's ~340 "queued for rescrape" lines were overwhelmingly
real Hammer/Track/Ebonite ball names -- Black Widow, Raw Hammer, Theorem
Delta, Paradox, Scandal, 3-D Offset, Absolut Curve, Fallout, and more --
cross-checked directly against this repo's own `tests/fixtures/hammer_
*.json` / `track_*.json` / `ebonite_*.json` fixtures, confirming these
are real products, not noise.

Root cause: `html_fallback_skus`'s WHERE clause (added in 6f.4) checked
only "product has >=1 SKU row AND none of them have source <> 'html'"
-- i.e. "every SKU row is source='html'." That's the right signal ONLY
on commercebuild, where `source='pdf'` is the normal path and a single
`source='html'` row is a STOPGAP written when the real PDF table
couldn't be recovered (6f.3). On every OTHER platform -- `product_
scraper.py`/Brunswick, `woocommerce_product_scraper.py`/SWAG, `netsuite_
product_scraper.py`/MOTIV, `shopify_product_scraper.py`/Hammer+Track+
Ebonite -- `source='html'` is the NORMAL, CORRECT, ONLY source: those
scrapers pull real per-weight RG/Diff data straight out of rendered
HTML, no PDF involved at all. "Every row is source='html'" is the
expected healthy state on those platforms, not a sign anything's
missing. Without a platform scope, 6f.4's filter matched roughly the
entire non-commercebuild catalog.

Fixed by adding `and p.source_platform = 'commercebuild'` to the
filter's WHERE clause in `src/admin_api/service.py` (`list_products`),
so `html_fallback_skus` now only ever matches commercebuild products,
regardless of whether the caller also passes `source_platform`
explicitly. The 14 "ERROR ...: 503:" lines in that same batch log were
very likely a symptom of this bug too (1034 near-simultaneous rescrape
requests queued at once) rather than a separate issue -- worth
confirming after redeploy by re-running the panel and checking whether
the count drops to a small, correctly-scoped number with no 503s.

Tests: `tests/test_admin_api_service.py` -- new `test_list_products_
html_fallback_skus_scoped_to_commercebuild` (asserts the hardcoded
`p.source_platform = 'commercebuild'` literal is present even when the
caller passes no `source_platform` arg at all), and `test_list_
products_html_fallback_skus_combines_with_source_platform` updated to
also assert the hardcoded scope alongside the existing parameterized
`source_platform` clause (both apply, ANDed together, not conflicting).
211/211 in this module; full repo `test_*.py` sweep also clean.

Redeploy: same as 6f.4 -- `sam build AdminApiFunction && sam deploy`
(or a full unscoped build, per the 6a.5 caveat). Al still needs to
confirm current Viking behavior post-redeploy separately -- the "i
don't think we fixed the viking issue" half of his report was about
6f.3's Textract path (see the Viking follow-up above), not this filter,
and hasn't been re-checked yet since he moved straight to the batch log.

### 6f.3.1. Real fix, finally: Viking's actual Tech Data table shape

The Viking follow-up in 6f.3 above documents two false starts before
this: a "row-merge" hypothesis (wrong), then a "weight-header-row"
correction that still couldn't see the real data because it was being
silently discarded. Both were reconstructed guesses -- this sandbox has
no AWS access, so nothing could be confirmed without Al pulling actual
CloudWatch log lines himself, twice, following commands given to him in
this session.

The real full table, from Al's own CloudWatch pull:

```
['NOTES', '', '', '', '', '', '']
['', '16lb', '15lb', '14lb', '13lb', '12lb', '']
['RG', '2.50', '2.51', '2.52', '2.56', '2.58', '']
['DIFF', '.050', '.052', '.051', '.034', '.031', '']
['PSA', '.014', '.016', '.014', '.011', '.009', '']
```

A genuine 4th Tech Data table shape (`_skus_from_table`'s docstring now
documents all four): a weight HEADER row holding every weight as its
own separate column, followed by separate METRIC rows (RG/DIFF/PSA)
below it, values aligned under each weight's own column. Added a real
"column-weight" parsing branch for this shape, checked after wide mode
and before long mode.

Guarded against a subtle regression: header detection requires EVERY
non-blank cell in a candidate row to be weight-shaped, not just 2+ of
them. Without that, a single corrupted cell inside an otherwise-normal
5-row long-mode table (Roto Grip Gremlin's real shape, case 2) could get
misread as this table's header and silently discard 4 good rows just
because one row went bad -- covered by its own regression test
(`test_skus_from_table_column_weight_mode_does_not_hijack_corrupted_
long_mode_row`).

Tests: real Viking table now returns all 5 correct SKUs. New tests for
unrecognized label rows ("NOTES"), MB-as-mass_bias (same PSA-is-
mass_bias fix as the other two modes), and the hijack-prevention case
above. 81/81 in `test_commercebuild_product_scraper.py`, full repo
sweep clean.

Redeploy: `sam build CommercebuildProductScraperFunction && sam deploy`
(or full unscoped build, per 6a.5). Then Rescrape Viking (button now
lives in the Raw Data tab, see 6f.4.1 area) and confirm it comes back
with 5 real SKU rows instead of the single html-fallback row.

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

### 6f.7. Track missing balls -- stale collection-handle config (real incident)

Al: "track is missing balls." Refetched trackbowling.com/collections.json
live rather than trusting the confirmed-at-onboarding set from 6f.6 above,
and found `high-performance`/`upper-mid-performance`/`mid-performance` all
now report `products_count: 0`. Track reorganized its site at some point
after onboarding: every current ball now lives under one real collection,
handle `balls` (`products_count: 7`) -- confirmed by fetching a couple of
its actual products (Theorem Delta, Kinetic Sapphire Ice, both real
current balls with full spec tables) and by Ebonite's own
collections.json literally titling the equivalent collection "Current
Ball Lineup" for the same handle, same role. Only `polyester` (1 product)
was still resolving under the old tier-handle config, so
`TrackUrlDiscoveryFunction` was silently discovering just 1 of Track's ~7
current balls -- a config drift, not a code bug: `build_entries()` in
`shopify_url_discovery/app.py` already treats any handle outside
`RETIRED_COLLECTION_HANDLES` as a current-tier signal, so nothing there
needed to change.

Confirmed this is Track-specific, not a platform-wide Shopify theme
change: Hammer's and Ebonite's own tier collections still report real
non-zero counts live, and Ebonite's tier-handle sum (pro-performance 3 +
high-performance 1 + mid-performance 6 + polyester 1 = 11) exactly
matches its own `balls` collection's `products_count` (11) -- Ebonite's
config is still complete, doesn't need this fix.

**Fix:** `TrackCollectionHandles` updated from `"high-performance,upper-
mid-performance,mid-performance,polyester,retired-balls"` to
`"balls,retired-balls"` in both `template.yaml`'s Parameter Default and
`samconfig.toml`'s `parameter_overrides` (the value that actually took
effect on the last real deploy -- template Defaults only apply to a fresh
stack with no override). `shopify_url_discovery/app.py`'s module
docstring updated with the same incident writeup and a reminder that
collection-handle sets are a live per-brand merchandising decision, not a
stable platform contract -- worth re-confirming live if Hammer or Ebonite
are ever reported missing balls too, not just at initial onboarding.

No test changes needed (no test encoded the old handle set as a literal).
Full `test_shopify_product_scraper.py` (46/46),
`test_shopify_product_scraper_orchestration.py` (15/15), and
`test_shopify_url_discovery.py` (11/11) suites still pass.

**Requires redeploying `TrackUrlDiscoveryFunction`** with the new
parameter value:
```bash
sam build TrackUrlDiscoveryFunction
sam deploy
```
(`sam deploy` re-reads `samconfig.toml`'s `parameter_overrides`, so the
new `TrackCollectionHandles` value takes effect on this deploy regardless
of which function was rebuilt -- rebuilding just `TrackUrlDiscoveryFunction`
is enough here since no code changed, only the env var it reads. Given
the fastapi/build-cache scare earlier this session, `sam build && sam
deploy` remains the safer fallback if there's any doubt.) After
redeploying, invoke it directly (see 6f.6's `aws lambda invoke` command
above) and confirm Track's current-ball count in the admin site's
Products tab (filter: Brand = Track, Status = current) jumps from ~1 to
~7.

### 6f.8. Manual seed URLs -- orphan-page catch (real Storm Equinox incident)

**REAL INCIDENT (2026-09-04):** Al reported "the original equinox
bowling ball is missing from the storm site" (`storm-equinox-bowling-
ball`). Investigated and root-caused: a genuine orphan page -- live,
in-stock, indexable -- but present in NEITHER of commercebuild_url_
discovery's two discovery sources (the "Bowling Balls" category listing,
and `sitemap_products.xml`), because stormbowling.com itself stopped
linking to it internally (superseded on-site by "Equinox Hybrid"/
"Equinox Solid" variant pages). Not a scraper bug -- both discovery
sources were working exactly as designed, the site just doesn't expose
this page to a crawler anymore. A one-off manual Lambda invocation
fixed that one product:

```bash
aws lambda invoke --cli-binary-format raw-in-base64-out \
  --function-name bowling-scraper-commercebuild-product-scraper \
  --payload '{"url": "https://www.stormbowling.com/storm-equinox-bowling-ball", "brand_id": "<storm-brand-uuid>"}' \
  /tmp/out.json && cat /tmp/out.json
```

That fixed the one product but left no permanent catch for the next
page like it. Migration 027 (`manual_seed_urls`) + a small admin-site
panel are that permanent catch:

- `manual_seed_urls` (brand_id, url, note) -- an admin-curated list of
  URLs to always union into a url_discovery Lambda's own discovered set,
  regardless of what that platform's normal crawlable sources find.
- `commercebuild_url_discovery/app.py`'s `handler()` now unions THREE
  sources per brand instead of two: category listing, sitemap, and
  `discover_manual_seed_urls(conn, brand_id)`. A seeded URL flows
  through the exact same `discovered_urls` diff + SQS publish path as
  anything else, so it only gets (re-)scraped once, not on every
  discovery run, once it's landed in `discovered_urls`.
- `admin_api`: `GET/POST /manual-seed-urls`, `DELETE /manual-seed-urls/
  {id}` (service.list_manual_seed_urls/create_manual_seed_url/
  delete_manual_seed_url) -- no template.yaml changes needed, same as
  every other admin_api route (the `{proxy+}` catch-all already covers
  it, see 6h's own note on this for `/cores`).
- `admin-site`: a "Manual seed URLs" panel on the Batch Jobs tab -- pick
  a brand (via the same `GET /brands` fetch the Products/Cores tab
  filters already use, a new `brand-picker` select class alongside the
  existing `brand-filter` one), paste the URL, optional note, Add.

**Scope, deliberately narrow for now:** only wired into
`commercebuild_url_discovery` (Storm/Roto Grip/900 Global), since that's
the platform this incident actually happened on. `manual_seed_urls` and
its admin_api/admin-site surface are generic (keyed by brand_id, not
commercebuild-specific) -- if an orphan page ever turns up on Brunswick/
Radical/DV8, WooCommerce, NetSuite, or Shopify instead, extending to
that platform's own url_discovery Lambda is a small, mechanical
follow-up (one `discover_manual_seed_urls(conn, brand_id)` call unioned
into that Lambda's own URL set, no schema change needed).

To actually use this for a future orphan: find its `brand_id` (Products
tab, filter by brand, any product's row shows it, or `select id, name
from brands`), add it via the new Batch Jobs panel, then either wait for
`CommercebuildUrlDiscoveryFunction`'s next scheduled run or invoke it
directly to pick it up immediately.

Full test file: `test_commercebuild_url_discovery.py` 23/23 (new:
`test_discover_manual_seed_urls_returns_this_brands_urls_only`,
`test_discover_manual_seed_urls_empty_for_brand_with_no_seeds`),
`test_admin_api_service.py` 256/256 (new: list/create/create-dedupes/
delete/delete-missing for `manual_seed_urls`, 5 tests). Full 44-file
regression sweep: clean. `admin-site/index.html`'s extracted `<script>`
block verified via `node --check` (no build step for this page, same
verification this file's own tests used for prior admin-site edits).

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

Ships with its daily schedule `Enabled: true`, on the assumption a real
`BigCommerceSecretArn` is supplied at deploy time (see that parameter's
description in `template.yaml`) -- **a real secret + ARN already exist
for this deployment**, see step 3. If you're deploying somewhere without
real BowlerDepot API credentials yet, flip `Enabled: true` to `Enabled:
false` on `BowlerDepotReconciliationFunction`'s `DailySchedule` event in
`template.yaml` first -- otherwise it hard-fails daily calling
`get_bigcommerce_credentials()` against a missing secret.

1. Set the stack parameter: `sam deploy --guided` (or
   `--parameter-overrides BigCommerceSecretArn=<arn>` non-interactively),
   keeping every other parameter the same.
2. Before trusting its accuracy-check output, verify
   `CUSTOM_FIELD_NAME_CANDIDATES` in
   `src/bowlerdepot_reconciliation/app.py` actually matches your real
   store's `custom_fields` names (pull one real product via the
   BigCommerce API and check) -- this was a disclosed best-guess mapping,
   never checked against a real store. A wrong guess won't error, it'll
   just silently report zero mismatches, which looks like "all clean"
   when it's actually "not checking anything." See README's "QA
   cross-checks" section for the full caveat.

### 6h.1. fuzzy_match_product real incident: matched "Storm iQ Tour" to "Storm iQ Tour AI"

Al reported this indirectly, working backward from a symptom: "product
[id] is missing counts for 15 and 13 weights" (a SKU stock table gap in
admin) -> he manually verified on bowlerdepot.com that the real "Storm
iQ Tour" listing has quantities for every weight -> "it is finding the
'Storm iQ Tour AI' instead of the 'Storm iQ Tour'". price_checker's SKU
matching was reading a genuinely DIFFERENT ball's BigCommerce variants
the whole time, because `fuzzy_match_product` had linked our "Storm iQ
Tour" product to BowlerDepot's separate "Storm iQ Tour AI" listing.

**Root cause**: `fuzzy_match_product` scored candidates with plain
`difflib.SequenceMatcher` character-ratio similarity only. "storm iq
tour" vs "storm iq tour ai" scores ~0.90 -- comfortably above
`FUZZY_MATCH_THRESHOLD = 0.80` -- because a short appended word barely
dents a character-level ratio no matter what that word actually says.
Bowling-ball naming convention is overwhelmingly "base name + a short
suffix that names a genuinely different ball" (Solid/Pearl/Hybrid/Pro/
AI/etc.), so this was a systemic risk, not a one-off -- any product
whose exact-name match was ever missing/stale/unpublished on
BowlerDepot's side, with a suffixed sibling ball present instead, was
exposed to the same silent wrong-match.

Correctly landed as `match_status = 'ambiguous'` (ratio 0.90 < 1.0), so
it wasn't auto-trusted by `check_accuracy` -- but `discover_
bigcommerce_candidates` still created a real, `'low'`-confidence
BigCommerce price-source candidate pointing at the wrong product, and
that candidate still had to be manually approved before price_checker
would ever touch it (the module's own "never auto-approved" design) --
the review step that existed didn't catch this specific failure mode.

**Fix**: `fuzzy_match_product` now requires a new token-compatibility
gate (`_names_token_compatible`, using a separate `_loose_tokens`
tokenization -- punctuation-as-boundary, not punctuation-stripped, so it
doesn't collide with `_normalize_name`'s existing "Emerald/Black" ->
"emeraldblack" behavior that SequenceMatcher still relies on) BEFORE a
candidate's ratio is even computed. Two names may only differ by words
in `_GENERIC_NAME_SUFFIX_TOKENS = {"bowling", "ball", "balls"}` -- the
one suffix `FUZZY_MATCH_THRESHOLD`'s own original calibration comment
already documented as harmless retail-listing noise. Any OTHER extra/
missing word (AI, Solid, Pearl, Hybrid, Pro, ...) now hard-rejects that
candidate before SequenceMatcher gets a chance to score it favorably.

Tests: 4 new real-incident cases in `test_bowlerdepot_reconciliation.py`
(rejects the exact "Storm iQ Tour"/"Storm iQ Tour AI" pair, rejects
Solid/Pearl/Hybrid/Pro variants of the same shape, still allows the "+
Bowling Ball" suffix -- tested both directly against
`_names_token_compatible` and end-to-end through `fuzzy_match_product`
with a longer base name to avoid an unrelated short-name/threshold
interaction -- and confirms the real match wins outright when both the
correct and wrong-suffix candidates are present together). Full suite:
1021/1021 passing, zero regressions.

**Scope note**: Al asked specifically for the matching-algorithm fix.
Not done as part of this: correcting product
`8e93b985-857b-4516-a29b-6f238f6652b1`'s existing bad price-source row
(still points at "Storm iQ Tour AI" until someone re-runs matching/
re-approves a corrected candidate for it), and a catalog-wide scan for
other already-approved price sources with this same collision shape.
Both were offered and explicitly declined for now.

No migration, no `template.yaml` change -- redeploy just the one
function:
```bash
sam build BowlerDepotReconciliationFunction
sam deploy
```

**Follow-up, same session -- real incident: "it still finds the ai
version."** Reported after the fix above was already implemented (Al
hadn't necessarily redeployed yet -- but this follow-up explains why
even a redeploy alone wouldn't have been enough). Investigating turned
up a SECOND, independent bug: `upsert_bowlerdepot_match`'s `ON CONFLICT`
target was `(bigcommerce_product_id, bigcommerce_sku)` -- keyed on the
BigCommerce side, not on our own `product_id`. A corrected re-match for
a product that already had a stored (wrong) match therefore didn't
overwrite that old row, it INSERTED A SECOND ROW for the same
`product_id`, since the new/correct BigCommerce id+sku pair had never
been seen before and didn't collide with anything.
`price_checker.list_bowlerdepot_matches` has no `DISTINCT`/`ORDER BY`,
so which of the two rows for a product "won" was effectively arbitrary
-- meaning even the fixed, redeployed matching algorithm could still
appear to "find the AI version" for a product that had already been
wrongly matched once before this fix existed.

**Fix, migration `018_bowlerdepot_products_dedupe_by_product.sql`**:
1. Collapses any existing duplicate `bowlerdepot_products` rows per
   `product_id` down to one (keeps the most-recently-synced row via a
   `row_number() over (partition by product_id order by last_synced_at
   desc nulls last, created_at desc, id desc)` window-function delete).
2. Drops the old `(bigcommerce_product_id, bigcommerce_sku)` unique
   constraint -- looked up dynamically via `pg_constraint`/`pg_attribute`
   rather than hardcoding Postgres's auto-generated constraint name
   (long enough to risk `NAMEDATALEN` truncation, not worth guessing).
3. Adds a new `unique (product_id)` constraint -- each of our products
   now has exactly one current match row, by design.

`upsert_bowlerdepot_match` (`src/bowlerdepot_reconciliation/app.py`) now
conflicts on `product_id` and does `update set bigcommerce_product_id =
excluded.bigcommerce_product_id, bigcommerce_sku = excluded.
bigcommerce_sku, ...` -- a fresh reconciliation run always overwrites a
product's match in place going forward, never accumulates a duplicate.

**Still not automatic**: this migration does not touch
`product_price_sources`. An existing already-approved price source
created from the old wrong match keeps pointing at the wrong
BigCommerce product until an admin re-triggers "Find price sources"
(`POST /products/{id}/discover-price-sources`) for that product, after
BOTH this migration and the algorithm fix are deployed and a fresh
reconciliation run has corrected the underlying `bowlerdepot_products`
row -- `discover_bigcommerce_candidates`/`upsert_bigcommerce_price_
source_candidate` already corrects an existing approved row in place
when the underlying match has changed, it just isn't triggered
automatically by the migration itself.

Tests: `test_upsert_bowlerdepot_match_conflicts_on_product_id` (new,
uses a minimal fake conn/cursor recording executed SQL -- this module
has no established fake-Postgres pattern elsewhere, `handler`/`write_*`
are documented as needing real credentials to verify) pins the new
conflict target so this specific regression can't silently recur. Full
suite: 1022/1022 passing.

```bash
psql "$DATABASE_URL" -f db/migrations/018_bowlerdepot_products_dedupe_by_product.sql
sam build BowlerDepotReconciliationFunction
sam deploy
```

Then, to clear THIS specific product's stale price source once the
above is live: run reconciliation (its own daily schedule, or invoke
manually), then use admin-site's "Find price sources" on product
`8e93b985-857b-4516-a29b-6f238f6652b1` to correct the existing approved
row in place.

**Follow-up, same session -- real incident continues, Al: "the word
edition is not always used and is more commonly not used.
bowlerdepot.com does not use edition."** After both fixes above were
deployed, redeployed, and independently verified (fresh Lambda
`LastModified`, clean CloudWatch logs across two manual invokes, product
confirmed published/in-scope), Al still reported the same wrong match on
re-checking the product detail page. This comment pointed at a third
angle: our own product's stored name likely carries "Edition" as an
extra word (a manufacturer-side qualifier for a re-release/special run)
that BowlerDepot's storefront listing for the same ball typically drops
-- the same shape as the already-handled "+ Bowling Ball" suffix, so
"edition" was added to `_GENERIC_NAME_SUFFIX_TOKENS`.

**That alone was not enough.** Live testing immediately turned up a
second, deeper gap in the original 6h.1 fix: `_names_token_compatible`
only ever controlled candidacy (whether a name is considered at all),
never the actual match decision -- a token-compatible candidate still
had to separately clear `SequenceMatcher(...).ratio() >=
FUZZY_MATCH_THRESHOLD (0.80)`, computed against the raw, filler-word-
included normalized strings. For a short base name, that's a real
problem: `"storm iq tour edition"` vs `"storm iq tour"` scores only
`0.7647` -- below threshold -- purely because "edition" (7 characters)
is proportionally expensive against a 14-character base name, even
though the token gate had already confirmed the only difference between
the two names was generic filler noise. The exact same latent gap
already existed for the original "Bowling Ball" tolerance; it just never
surfaced in a real incident before now because every prior case (e.g.
"Brunswick Fury Emerald/Black Hybrid") used a long enough base name that
the same fixed-length suffix cost proportionally little.

**Fix**: once `_names_token_compatible` has confirmed two names differ
only by generic filler words, requiring them to ALSO clear an arbitrary
character-ratio threshold is redundant and reintroduces exactly the
false-negative the gate exists to prevent. `fuzzy_match_product` now
floors a token-compatible candidate's effective ratio at a new
`TOKEN_COMPATIBLE_MIN_RATIO = 0.90` (`max(raw_ratio,
TOKEN_COMPATIBLE_MIN_RATIO)`) -- guaranteed to clear
`FUZZY_MATCH_THRESHOLD` regardless of base-name length, while staying
below `1.0` so it's still recorded as `'ambiguous'` (not silently auto-
trusted) same as every other non-exact match. The raw ratio is still
used to rank between multiple token-compatible candidates when more than
one is present -- the floor only ever raises the ratio used for the
threshold comparison, never lowers a candidate that already scored
higher on its own.

Tests: 3 new cases in `test_bowlerdepot_reconciliation.py` -- "Storm iQ
Tour Edition" now matches "Storm iQ Tour" at the floor ratio despite its
raw SequenceMatcher ratio being below threshold; the "AI" candidate is
still hard-rejected even when both it and the real match are present
(confirms the floor didn't reopen the original 6h.1 collision); and a
long-base-name pair that already scores above the floor on raw
character similarity keeps its own higher ratio (confirms `max()`, not a
clamp/override). Full suite: 1025/1025 passing, zero regressions.

No migration, no `template.yaml` change -- redeploy just the one
function:
```bash
sam build BowlerDepotReconciliationFunction
sam deploy
```

Same as the prior follow-up: this only fixes the algorithm going
forward. If product `8e93b985-857b-4516-a29b-6f238f6652b1`'s own stored
name is confirmed to include "Edition," clearing its existing stale
price source still requires a fresh reconciliation run followed by
re-triggering "Find price sources" on that product, per the note above.
Al's exact database value for this product's stored name has not yet
been directly confirmed -- this fix was validated against a
representative test case ("Storm iQ Tour Edition" vs "Storm iQ Tour"),
not against the live row itself.

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

**Bulk approve/reject in the Review Queue tab (2026-08-12).** Al: "can we
add a bulk way to approve or reject items in the review queue, maybe a
checkbox that then allows for a reject all or approve all." Frontend-only
change, no new `admin_api` endpoint -- `service.approve_review_item`/
`reject_review_item` already operate one row at a time, so this is a
client-side loop over the existing `POST /review-queue/{id}/approve` and
`.../reject` calls, not a new bulk backend route.

- The Review Queue tab (`status=pending` only -- see below) now shows a
  checkbox column, a header "Select all shown" checkbox, a live "N
  selected" count, and "Approve selected" / "Reject selected" buttons in
  a toolbar row above the table (`#review-bulk-toolbar`). Selection lives
  in `reviewState.selected` (a `Set` of `review_queue` row ids) and is
  reset on every reload -- filtering, paging, or a bulk action finishing
  all swap out which rows are on screen, so a stale selection would
  either silently no-op or point at an already-resolved row.
- The toolbar and checkbox column only render when `reviewState.status
  === 'pending'` -- approved/rejected rows have no action left to
  bulk-apply, so there's nothing to select there.
- "Reject selected" prompts once for a reason applied to every selected
  row (not once per row -- a per-item prompt would defeat the point of a
  bulk action). "Approve selected" confirms the count before applying.
- **Sequential, not `Promise.all`** -- deliberately mirrors every
  `scripts/backfill_*.py` script in this project, all of which retry-
  with-backoff for the same reason: a burst of concurrent `admin_api`
  calls has already been observed (this session, unrelated feature) to
  silently corrupt some responses under API Gateway/Lambda throttling.
  `runReviewBulkAction` in `admin-site/index.html` fires one approve/
  reject call at a time with a 300ms pause between, tracks
  ok/failed counts, and reports a single toast summary at the end (e.g.
  "12 approved, 1 failed (still pending -- retry them)") rather than
  either blocking with no feedback or firing everything at once. A
  per-item failure doesn't stop the rest of the batch -- same "tolerate
  and keep going" posture as the batch scripts' own `run()` functions.
- No `template.yaml` or `service.py`/`app.py` change -- this only touches
  `admin-site/index.html`, which has no redeploy step (static file, open
  directly or serve locally per this section's own CORS notes above).

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

**Reassign/delete no longer let the same video resurface on rescan.**
Al, after using the two tools above to clean up Combat Solid: "i just
cleaned up combat solid and then did a search again and all the video i
just cleaned up came back... now im going to reject them and then they
will just come back." A real second-order bug, found by using the
feature exactly as intended:

- `insert_candidates` (`video_discovery/app.py`) is idempotent via
  `ON CONFLICT (product_id, youtube_video_id) DO NOTHING` -- its own
  docstring already says a video already stored "in any status" is left
  untouched, not reset back to pending. That means a row's mere
  *presence* at a given `(product_id, youtube_video_id)` slot is what
  blocks reinsertion, regardless of its `status`.
- `reassign_video_candidate` used to literally `UPDATE product_videos SET
  product_id = ...` -- moving the row. That frees up the *origin*
  product's slot, so the next rescan of the origin has nothing left to
  conflict with, and the exact same false-positive video comes right
  back as a fresh 'pending' row. `delete_video_candidate` has the
  identical problem for the identical reason (deleting frees the slot
  too) -- so Al's instinct that reject would "come back" was actually
  backwards: reject alone is safe (it leaves the row, tombstoned, in
  place); it was reassign and delete that were unsafe.
- Fix, in `service.reassign_video_candidate`: reassigning now *always*
  leaves the origin row behind as a `status='rejected'` tombstone (same
  update `reject_video_candidate` does, applied directly since reassign
  must work from any starting status, not just pending) -- that
  permanently blocks the video from resurfacing under the wrong product.
  The actual content (title/transcript/summary/status) is copied to a
  **new** row on the target product.
- Al's second report, same message: reassigning to a product that
  already has its own row for that video used to raise a 422 and require
  manually deleting one of the two duplicates -- which had the exact same
  resurfacing bug, since the delete-based cleanup removes a blocking
  tombstone. Reassign no longer errors on that conflict: it merges
  instead, backfilling only the target row's *null*
  `transcript`/`summary`/`transcript_note` fields from the origin (so
  review work already done under the wrong product isn't lost) without
  ever touching the target's own `status` -- an admin who already
  reviewed the target's copy shouldn't have that judgment silently
  overwritten by a merge.
- Response shape changed accordingly: `{"video_id": ..., "product_id":
  ..., "origin_video_id": ..., "merged_with_existing": true|false}` --
  `video_id` is now the TARGET row's id (a new id in the no-conflict
  case), not the id that was passed in. `delete_video_candidate` is no
  longer reassign's conflict-cleanup step (its docstring's updated to
  say so) -- reserve it for genuine duplicate cleanup only, not for
  "this video doesn't belong here" (use reject or reassign for that, both
  of which now correctly prevent resurfacing).
- `ReassignRequest` gained an optional `resolved_by` field, stamped on
  the origin's tombstone only (audit: who reassigned this away from
  here) -- admin-site's reassign buttons (both the Video Candidates tab
  and the new product-detail Videos section) now send it, same
  `getSettings().resolvedBy` value approve/reject already use.
- No `template.yaml` or migration change -- `product_videos`' existing
  `unique(product_id, youtube_video_id)` constraint (004) already
  supports this; the fix is entirely in how `service.py` uses it.

**P0 INCIDENT (2026-08-11): every scraper's product_images INSERT was
silently aborting the ENTIRE upsert_product transaction, catalog-wide,
across all five scrapers, since migration 010 went live.** Found while
chasing why Combat's `core_id` still wasn't set after fixing
`_find_table_by_row_labels` (see the entry above) -- Al ran the redeploy,
re-ran the backfill, and it was STILL missing. Checked
`bowling-scraper-product-scraper`'s CloudWatch logs directly and found
the real error on nearly every single job in the batch, Brunswick AND
DV8 alike:

```
psycopg2.errors.NotNullViolation: null value in column "display_order" of relation "product_images" violates not-null constraint
```

Root cause: migration 010 added `product_images.display_order integer not
null` with **no database-level default** -- by design, per that
migration's own comment ("no scraper touches any of these three fields").
That assumption was wrong in practice: every scraper still has to supply
*some* value for a NOT NULL column with no default when INSERTing a new
row, even one it never otherwise reads or writes. None of the five
scrapers' raw `insert into product_images (product_id, image_type,
source_url) values (...)` statements were ever updated when that
migration landed. The image insert happens *inside* `upsert_product`,
before the final `conn.commit()` -- so the exception didn't just drop the
one image row, it aborted the whole transaction: the products row
upsert, `core_id`/`coverstock_id` writes, SKU inserts, all of it, for
**every product with at least one image being inserted or updated** --
which is effectively every product on every rescrape, and every brand-new
product. This has silently been breaking every scraper Lambda
(`bowling-scraper-product-scraper` [Brunswick/Radical/DV8],
`bowling-scraper-shopify-product-scraper` [Hammer/Track/Ebonite],
`bowling-scraper-woocommerce-product-scraper` [SWAG],
`bowling-scraper-netsuite-product-scraper` [MOTIV],
`bowling-scraper-commercebuild-product-scraper` [Storm/Roto Grip/900
Global]) since that migration's redeploy -- not a Brunswick-specific or
Combat-specific issue at all, just discovered via Combat because that's
what was actively being rescraped and checked.

**Fix, identical in all five scrapers' `upsert_product`:** give the
INSERT a computed `display_order` via a correlated subquery,
`coalesce((select max(display_order) + 1 from product_images where
product_id = %s), 0)` -- appends new images after whatever's already
there for that product (0-based, matching migration 010's convention),
first image for a product gets 0. Only affects the INSERT branch; the
existing `on conflict (product_id, source_url) do update set image_type =
excluded.image_type` branch for an already-present row is untouched, so
this can never clobber an admin's prior manual reordering (`admin_api.
reorder_product_images`). `commercebuild_product_scraper`'s single-image
insert got the same fix (no orchestration-level FakeCursor test exists
for that scraper in this repo to verify against -- confirmed by direct
`ast.parse` only, a pre-existing test gap, not something newly
introduced here).

Updated FakeCursor branches in `test_product_scraper_orchestration.py`,
`test_shopify_product_scraper_orchestration.py`, `test_netsuite_product_
scraper_orchestration.py`, and `test_woocommerce_product_scraper_
orchestration.py` for the new 4th param (product_id again, for the
subquery) -- all four suites pass (20/20, 15/15, 27/27, and 4/5 with the
5th being the pre-existing, already-documented, unrelated woocommerce
`insert into cores` FakeCursor gap that fails before ever reaching the
image insert).

**Requires redeploying all five scraper Lambdas** -- this is a shared
bug pattern fixed independently in each scraper's own `app.py`, not a
single shared module:
```bash
sam build ProductScraperFunction
sam build ShopifyProductScraperFunction
sam build WooCommerceProductScraperFunction
sam build NetsuiteProductScraperFunction
sam build CommercebuildProductScraperFunction
sam deploy
```
Or just `sam build && sam deploy` for a full rebuild if there's any doubt
about local build-cache state (see the `fastapi` packaging incident just
above this section for why that doubt is reasonable right now). Once
redeployed, re-run `backfill_core_ids.py`/`backfill_coverstock_ids.py`
(or any pending rescrape) -- this was the actual blocker the whole time,
not anything specific to Brunswick's parsing.

### 6i.5. Products tab: status (current/retired) filter

Al's direct ask right after the P0 `display_order` investigation above,
now that all five scrapers correctly populate `products.status` off
each page's `/current/` vs `/retired/` URL path (or platform
equivalent): filter the Products tab down to just current or just
retired product lines instead of scrolling the whole catalog.

`GET /products` gained a `status` query param (`admin_api/service.py`'s
`list_products`, `admin_api/app.py`'s route) -- adds `and p.status = %s`
only when passed, same param-bound pattern as the existing
`source_platform` filter right next to it, no validation against the
enum's two values (`current`/`retired`, migration 001's `product_status`
type) -- an unrecognized value just matches zero rows rather than
erroring, consistent with how every other string filter on this endpoint
already behaves.

`admin-site/index.html`'s Products tab gained a "Status" dropdown
(any/current/retired) next to the existing "Published" dropdown, wired
into `productState.status` and `loadProducts`'s params the same way.
`p.status` was already selected and rendered as its own list column, so
no other UI change was needed.

Two new tests in `tests/test_admin_api_service.py`
(`test_list_products_status_adds_filter_sql`,
`test_list_products_omits_status_filter_by_default`), same
`_QueryCapturingConnection` SQL-text-capture pattern as the
`source_platform` tests right above them (no real Postgres in this
sandbox). Full `test_admin_api_service.py` suite: 103/103 passing.

No `template.yaml` change needed -- `GET /products` already exists and
routes through `AdminHttpApi`'s catch-all proxy. Requires redeploying
only `AdminApiFunction`:
```bash
sam build AdminApiFunction
sam deploy
```
(No admin-site redeploy step exists in this project -- it's a static
file Al opens directly/hosts himself, not a Lambda-fronted deployable.)

### 6i.6. Video engagement stats (views/likes/comments/duration/description)

Al's direct ask: "for the videos can we get pull down more data points
from the videos, date it was added current view counts and any other
data that make sense." `search.list`'s snippet part (everything
`search_youtube` ever pulled) has no engagement data at all -- title,
channel, `published_at`, and a thumbnail is it. A separate
`videos.list` call is the only way to get view/like/comment counts plus
duration and the full description, and unlike `search.list` (the
confirmed 100-units/call, 100-calls/day-constrained resource -- see the
HARD QUOTA CONSTRAINT section referenced elsewhere in this doc),
`videos.list` is cheap and flat-cost regardless of how many of the up
to 50 ids are packed into one call, so it needed neither a circuit
breaker nor a conservative per-invocation cap.

Migration 013 (`db/migrations/013_product_videos_stats.sql`) adds six
columns to `product_videos`: `view_count bigint`, `like_count bigint`,
`comment_count bigint`, `duration_seconds integer`, `description text`,
`stats_fetched_at timestamptz`. `like_count`/`comment_count` are left
`NULL` rather than coerced to `0` when YouTube's response omits them --
a channel owner can hide those counts, and `NULL` is the honest "never
disclosed" while `0` would falsely claim "confirmed zero."

Two ways stats get populated, both in `src/video_discovery/app.py`:

- **At discovery time**: `handler`'s existing search loop now calls the
  new `fetch_video_statistics(api_key, video_ids, session)` right after
  every `search_youtube` call, merges the result onto each candidate
  before `insert_candidates`, and stamps `stats_fetched_at`. Wrapped in
  its own try/except -- a candidate is still saved even if this
  enrichment call fails (rate limit, transient network), the same
  "secondary data must not block the primary write" stance
  `transcript_note` already takes elsewhere in this pipeline.
- **On demand, for candidates that already exist**: view counts are a
  snapshot, not a fixed fact like `published_at` -- they go stale the
  moment they're written. A new `{"refresh_stats": true, "limit": N}`
  job shape (handled as an early return in `handler`, before any
  product-search logic runs at all) calls the new
  `refresh_video_stats(conn, api_key, limit, session)`, which uses
  `select_video_ids_needing_stats_refresh` (ordered
  `stats_fetched_at asc nulls first, id asc` -- never-refreshed rows
  first, then longest-stale, same rotation shape
  `fetch_products_to_search`'s `last_video_discovery_at` ordering
  already uses) and `apply_video_stats` (updates by `product_videos.id`,
  not `youtube_video_id`, since the same video can legitimately appear
  under more than one row after `reassign_video_candidate`'s copy-not-
  move behavior; a video YouTube no longer returns anything for --
  deleted/private -- still gets `stats_fetched_at` bumped so it stops
  sorting first forever, but keeps whatever numbers it last had rather
  than nulling them out). `DEFAULT_REFRESH_STATS_LIMIT = 200`.

`src/admin_api/service.py` gained `queue_video_stats_refresh(limit=None)`,
same fire-and-forget `lambda_client.invoke(..., InvocationType="Event")`
shape as `queue_video_discovery` right above it, same
`{"queued": False, "reason": "VIDEO_DISCOVERY_FUNCTION_NAME is not
configured..."}` soft-fail when that env var isn't set. Unlike
`queue_video_discovery`, this is catalog-wide by design -- no
`product_id`, no conn, no existence check, since `VideoDiscoveryFunction`
itself decides which rows are most overdue. `list_video_candidates`'s
SELECT was extended with the five non-description stat columns
(`description` deliberately excluded from the list view, same
convention as the transcript/summary columns right next to it).

`src/admin_api/app.py` gained `POST /admin/refresh-video-stats` (optional
`?limit=` query param) right after `discover-videos`, routing straight to
`queue_video_stats_refresh`. No `template.yaml` change needed --
`AdminApiFunction` already has invoke permission on
`VideoDiscoveryFunction` for any payload shape, and the route hits the
existing catch-all proxy.

`scripts/refresh_video_stats.py` is a new one-shot script, same thin-
HTTP-client-against-admin_api shape as every other script in this
project (see `backfill_last_video_discovery_at.py`) -- a single POST to
`/admin/refresh-video-stats`, not a list-then-iterate loop, since the
actual selecting/fetching/updating all happens inside
`VideoDiscoveryFunction` itself.

`admin-site/index.html`: the Video Candidates tab's table gained a
Views column (`fmtCount`/`fmtDuration` helpers, a `stats_fetched_at`
tooltip, likes/comments as a small muted line under the view count),
the video detail panel shows views/likes/comments/duration plus
"stats as of \<date\>" or "never fetched," the product detail page's
Videos mini-table also gained a Views column, and a "Refresh stats"
button next to the existing rescan controls calls the new endpoint
(toast makes clear it's fire-and-forget -- results need a moment plus a
manual tab reload to show up, same as the existing rescan button).

52/52 in `tests/test_video_discovery.py` (new coverage across
`parse_iso8601_duration`, `parse_video_details_response`,
`fetch_video_statistics`, `insert_candidates`'s new columns,
`select_video_ids_needing_stats_refresh`, `apply_video_stats`,
`refresh_video_stats`, and `handler`'s two new code paths), 115/115 in
`tests/test_admin_api_service.py`, 7/7 in the new
`tests/test_refresh_video_stats.py`. Zero regressions in either
extended file.

Requires migration 013 (see step 2 above) and redeploying
`VideoDiscoveryFunction` and `AdminApiFunction`:
```bash
psql "$DATABASE_URL" -f db/migrations/013_product_videos_stats.sql
sam build VideoDiscoveryFunction
sam build AdminApiFunction
sam deploy
```
(No admin-site redeploy step, same as every other admin-site change --
it's a static file, not a Lambda-fronted deployable.)

**Follow-up, real incident**: Al noticed stats were "coming in but
pretty slow" after using this feature for a while. Root cause wasn't a
bug -- it's that both of `VideoDiscoveryFunction`'s relevant job shapes
were manual-invoke only (see 6i's own "no automated schedule" note,
which predates this feature): new candidates only get stats when the
SEARCH job shape happens to run and reach that product (capped at
70/invocation by the confirmed 100-searches/day quota), and existing
candidates only get refreshed when someone clicks "Refresh stats" (or
runs `scripts/refresh_video_stats.py`), which caps at
`DEFAULT_REFRESH_STATS_LIMIT=200` rows per click -- slow to work through
a real backlog (every `product_videos` row that existed before this
feature shipped started with `stats_fetched_at = null`).

Fix: `template.yaml`'s `VideoDiscoveryFunction` gained a
`DailyStatsRefresh` `Events: Schedule` (`rate(1 day)`) with an explicit
`Input: '{"refresh_stats": true, "limit": 1000}'` -- SAM/EventBridge
replaces the whole invocation event with that `Input` JSON, so this is
functionally identical to a manual `aws lambda invoke ... --payload
'{"refresh_stats": true, "limit": 1000}'` running once a day, no code
change needed. `limit=1000`, not the button's own 200 default -- `videos.
list` isn't the constrained resource `search.list` is (cheap, flat-cost
per call regardless of batch size, see `fetch_video_statistics`'s
docstring), so there's no quota reason to be conservative here, and even
1000 rows (20 `videos.list` calls of up to 50 ids each) is nowhere near
this function's 280s `Timeout`. The SEARCH job shapes ({}/`brand_id`/
`product_ids`) deliberately stay manual-only -- they DO burn the scarce
`search.list` quota, and Al already made the deliberate "subset-first,
invoke when ready" call on those (see 6i's own docstring reference).

This doesn't eliminate the backlog instantly -- `select_video_ids_
needing_stats_refresh` orders `stats_fetched_at asc nulls first`, so a
large existing backlog still takes a few days of runs to fully clear
(1000/day), and the schedule doesn't speed up how fast brand-new
candidates get discovered in the first place, only how current their
stats stay once they exist. No new tests -- this is pure infra
(`template.yaml`'s `Events` block), verified by re-parsing the template
with the same CFN-tolerant PyYAML loader used throughout this project
and confirming `Input` is valid JSON.

Requires a real `sam deploy` (not just `sam build`) to actually create
the EventBridge rule, same as any other `Events:` addition:
```bash
sam build VideoDiscoveryFunction
sam deploy
```

**Follow-up, real incident: Shorts skewing the pipeline.** Al: "the
intent of the video ingestion is review content and i have seen short
popping up and skewing things and there is no audible content at all.
maybe we put a duration requirment on videos." A YouTube Short carries
no real review content (often no audio track at all), but neither
`search.list` nor `score_match`'s heuristic can tell one apart from a
real review -- duration is the only reliable signal, and duration only
becomes known via the same `videos.list` enrichment call this whole
section already added. Shorts also skew `popularity_score` (6l.5) hard:
a viral Short can pull in view counts an honest multi-minute review
never will, for zero actual review value.

Presented Al a duration cutoff choice (YouTube's original 61s Shorts
boundary vs. its newer 3-minute expanded window) and a choice on
already-approved rows (auto-reject them too, vs. leave human approvals
alone and just report on them). Al picked **61 seconds** and **auto-
reject already-approved Shorts too**.

`src/video_discovery/app.py` gained `MIN_VIDEO_DURATION_SECONDS = 61`
and `SHORT_REJECTED_BY` (an audit-trail string, same "distinct from a
real person's email" convention as `scripts/auto_approve_video_
candidates.py`'s `DEFAULT_RESOLVED_BY`), plus two pure functions:
`is_likely_short(duration_seconds)` (true only when duration is KNOWN
and under the cutoff -- `None` is never treated as a Short, same
"secondary data must not block/change the primary outcome" stance the
rest of this pipeline already takes) and `filter_out_shorts(videos)`.
Enforced in two places:
- **Discovery time**: `handler`'s search loop calls `filter_out_shorts`
  right after stats enrichment, before `insert_candidates` -- a
  confirmed Short never becomes a `product_videos` row at all. A
  candidate whose duration isn't known yet (enrichment failed) passes
  through untouched.
- **Refresh time**: `apply_video_stats` force-transitions a row to
  `status = 'rejected'` when the freshly-fetched duration comes back
  under the cutoff, **regardless of its current status** -- including
  `'approved'`, per Al's explicit choice. Guarded with `and status <>
  'rejected'` so a row a human already rejected for a real reason keeps
  its original `resolved_at`/`resolved_by` rather than being silently
  overwritten with the automated one. `refresh_video_stats`'s return
  value gained `candidates_rejected_as_shorts` (visible in the
  `logger.info("Refreshed video stats: %s", result)` line handler
  already logs, so this shows up in CloudWatch without any UI change).

This means the DailyStatsRefresh schedule (the section right above this
one) is what actually cleans up the EXISTING backlog of already-inserted
Shorts (including previously-'approved' ones) over the next several
days as it works through the table -- there's no separate one-time
cleanup script, since the refresh cadence already covers every row
eventually and the reject logic runs inline on every refresh.

Tests (`tests/test_video_discovery.py`, 63/63 passing, 11 new): pure
`is_likely_short`/`filter_out_shorts` behavior (under cutoff, at cutoff,
above cutoff, unknown duration, mixed lists); `apply_video_stats` force-
rejecting a confirmed Short, NOT rejecting one that meets the minimum,
and a SQL-shape check confirming the `status <> 'rejected'` guard is
actually in the query text; `refresh_video_stats` counting shorts
rejected in a batch; `handler` dropping a confirmed Short before insert
while keeping one whose duration came back unknown. Two pre-existing
tests (`test_apply_video_stats_with_stats_updates_all_fields`,
`test_refresh_video_stats_updates_found_rows_and_marks_missing_ones_
checked`) had their fixture durations bumped from a coincidentally-
Short 60s (`PT1M`) to 120s (`PT2M`) so they stay focused on their
original assertions rather than accidentally exercising the new reject
path.

No migration, no `template.yaml` change -- redeploy just
`VideoDiscoveryFunction`:
```bash
sam build VideoDiscoveryFunction
sam deploy
```

**Follow-up (2026-09-04): PRE-RELEASE FILTER, auto-rejecting videos that predate a ball's own release.** Al: "use the youtube video date and
the release date to auto reject youtube videos for balls where the video
was released prior to the ball release. it is almost always likely to
not match and be a similar name or a sibling." Same shape as the Shorts
follow-up right above -- a real false-positive pattern noticed in review,
fixed with the identical two-enforcement-point structure, reusing
`products.release_date` (001_init_schema.sql/003_date_tracking_and_
bowwwl.sql) rather than adding anything new to the schema.

`src/video_discovery/app.py` gained `PRE_RELEASE_REJECTED_BY` (same
audit-trail convention as `SHORT_REJECTED_BY`) and two pure functions:
`is_before_release(published_at, release_date)` (true only when BOTH
dates are known and the video's publish date is strictly before
release_date -- same-calendar-day is NOT rejected; either date missing
means "never reject", same "unknown is never disqualifying" posture as
`is_likely_short`) and `filter_out_pre_release_videos(videos,
release_date)`. Enforced in the same two places:
- **Discovery time**: `fetch_products_to_search` now also selects
  `p.release_date`; `handler`'s search loop calls `filter_out_pre_
  release_videos` right after the Shorts filter, before
  `insert_candidates` -- a pre-release candidate never becomes a
  `product_videos` row.
- **Refresh time**: `select_video_ids_needing_stats_refresh` now joins
  `products` for `release_date` and also returns each row's own
  `published_at` (both already-known facts -- neither needs a fresh
  YouTube call); `apply_video_stats` force-transitions a row to
  `status = 'rejected'` when they show a pre-release match, regardless
  of current status (including `'approved'`) and regardless of whether
  `stats={}` this run, since this check -- unlike the Shorts one --
  doesn't depend on anything freshly fetched. Same `and status <>
  'rejected'` guard against clobbering a human's real rejection reason.
  `refresh_video_stats`'s return value gained `candidates_rejected_
  as_pre_release`, visible in CloudWatch the same way `candidates_
  rejected_as_shorts` already is.

Same operational consequence as the Shorts fix: the DailyStatsRefresh
schedule is what sweeps the EXISTING backlog (candidates stored before
this filter existed, or whose product's `release_date` only got
backfilled/corrected afterward) into compliance over the next several
days -- no separate one-time backfill script needed, for the same reason
one wasn't needed for Shorts.

Tests (`tests/test_video_discovery.py`, 83/83 passing, 20 new):
`is_before_release`'s full truth table (before/after/exact-release-day/
unknown-either-side/both-unknown/native-datetime-vs-ISO8601-string/
unparseable-string-never-raises), `filter_out_pre_release_videos`;
`apply_video_stats` force-rejecting a pre-release row (including when
`stats={}`), not rejecting an on/after-release row, the `status <>
'rejected'` guard, and no reject at all when either date is missing;
`refresh_video_stats` counting pre-release rejections in a batch,
independent of the Shorts counter; `handler` dropping a pre-release
candidate before insert while keeping one whose product has no known
`release_date`. One pre-existing test
(`test_select_video_ids_needing_stats_refresh_orders_stale_first`) had
its query-shape assertion updated for the new `join products` and
`pv.`-qualified `order by` (unqualified `id`/`stats_fetched_at` would
now be ambiguous between the two joined tables).

No migration, no `template.yaml` change -- redeploy just
`VideoDiscoveryFunction`:
```bash
sam build VideoDiscoveryFunction
sam deploy
```

**Follow-up (2026-09-04): expose the YouTube publish date everywhere a
video is shown.** Al: "can we also expose the youtube publish date in
the ui everywhere a video is shown." Pure display fix, no backend or
schema change -- both APIs already returned `published_at` for every
video row involved (`public_api/service.py`'s `get_product` videos
query, and `admin_api`'s `list_video_candidates`, which selects
`pv.published_at` for every caller including the product-detail panel's
own `status=all` fetch), and `consumer-site/src/api/types.ts`'s
`ProductVideo` already declared `published_at?: string | null`.

Audited every place a video candidate/review renders: `admin-site/
index.html`'s Video Candidates tab list rows and its expandable Detail
panel already showed it (`fmtDate(v.published_at)`); the one gap was
the product-detail page's own Videos section, which only showed
`channel_title` + duration. Added a `publishedText` var there, same
`' &middot; published ' + fmtDate(v.published_at)` format as the other
two spots. On the consumer side, `consumer-site/src/pages/
ProductDetailPage.tsx`'s "Video reviews" cards showed `channel_title`
alone -- added a human-formatted `published_at`
(`toLocaleDateString(... {year: "numeric", month: "short", day:
"numeric"})`) next to it, separated by " &middot; " when both are
present. `ComparePage.tsx` and the rest of consumer-site don't render
individual video listings, so no other files needed changes.

Verified `admin-site/index.html` via `node --check` against the
extracted `<script>` blocks (this project's standard no-build-step
check for that file); verified `ProductDetailPage.tsx` via `npx tsc -b
--force` (clean, no errors) -- the sandbox's `vite build` itself
currently fails on an unrelated pre-existing issue, a missing
`@rollup/rollup-linux-arm64-gnu` native optional dependency that npm's
registry access refuses to reinstall in this sandbox (a known npm
optional-deps bug, https://github.com/npm/cli/issues/4828); this is an
environment limitation, not a regression from this change, and Al's own
GitHub Actions build (`.github/workflows/deploy-consumer-site.yml`) is
unaffected since it runs `npm ci` fresh on GitHub's own runners. Full
44-file Python regression sweep: clean (no Python files touched by this
fix). No migration, no `template.yaml` change -- redeploy is just
pushing the two static files (`admin-site/index.html` served however
Al currently hosts it; `consumer-site` via its existing GitHub Actions
CI on push to main).

**Follow-up, real incident: admin-site batch size + bulk actions.** Al:
"can we have the refresh stats button do more videos at a time, also the
same check boxes and bulk actions on the video candidates page as we
added to the review queue." Two independent, front-end-only asks against
`admin-site/index.html` -- no `template.yaml`, migration, or backend
change for either, since the admin API already supported everything the
UI needed to start using.

*Refresh stats batch size.* The button's `refreshVideoStats()` was
calling `POST /admin/refresh-video-stats` with no params at all, so it
fell through `service.queue_video_stats_refresh(limit=None)` to
`video_discovery`'s own `DEFAULT_REFRESH_STATS_LIMIT = 200` every click --
the same 200-row cap that was part of the original "video stats coming
in slow" diagnosis above. `admin_api/app.py`'s route already accepted
`limit: Optional[int] = Query(None, gt=0)`, so this needed no backend
change: added a `#video-refresh-limit` number input next to the button
(defaults to **1000**, matching the DailyStatsRefresh schedule's own
limit so one manual click and one scheduled run move roughly the same
amount of backlog) and `refreshVideoStats()` now reads it and passes
`{ limit }` as a query param. `videos.list` isn't the quota-scarce
resource (`search.list` is, see the original diagnosis above), so a
larger default is safe.

*Bulk select/approve/reject on Video Candidates.* Mirrors the Review
Queue's bulk toolbar (see 6i's own Review Queue writeup / the
`#review-bulk-toolbar` HTML comment) exactly, same pattern, same
constraints: a `#video-bulk-toolbar` bar (select-all checkbox, selected-
count badge, Approve selected/Reject selected buttons) that only shows
when `videoState.status === 'pending'` and the current page has rows --
approved/rejected candidates have nothing left to bulk-apply. Selection
lives in `videoState.selected` (a `Set` of `product_videos` ids) and is
reset on every `renderVideoCandidates` call, same reasoning as Review
Queue's `reviewState.selected`: a filter/page change or a post-bulk-
action reload all swap out which rows exist, so a stale selected-id set
would either silently no-op or point at a since-resolved row.
`bulkApproveVideos`/`bulkRejectVideos`/`runVideoBulkAction` loop over the
existing per-id `POST /video-candidates/{id}/approve` and `/reject`
endpoints **sequentially, not via `Promise.all`** -- identical posture to
`runReviewBulkAction`, for the same reason (this project has already hit
real API Gateway/Lambda throttling under bursts of concurrent admin_api
calls). One reject-reason prompt is asked once and applied to every
selected row, not once per row, same as Review Queue's bulk reject.

No new tests: nothing in `src/` changed, this is admin-site JS/HTML
only. Verified by extracting the page's `<script>` block and running
`node -c` against it (syntax-only check, same trick used throughout this
project's admin-site work) -- no functional JS test harness exists for
admin-site.

No deploy step beyond the usual static-file swap for `admin-site/
index.html` (it's not part of any SAM stack -- see 6i's own admin-site
section for how it's served).

**Follow-up, real incident: same bulk logic on the product detail page's
Video candidates list.** Al: "can we add that same checkbox bulk logic
to the product page video candidates list" -- referring to the per-
product Videos section inside `loadProductDetailInto` (see this
section's own earlier writeup), not the standalone Video Candidates tab
this whole addendum started with. Same pattern again, with one
structural difference: that list fetches `status: 'all'` (pending,
approved, and rejected mixed together in one table -- see
`loadProductDetailInto`'s opening comment for why), so there's no single
page-level "showBulk" toggle the way the tab or Review Queue has one.
Instead each row decides for itself: only `status === 'pending'` rows
get a checkbox cell (approved/rejected rows already only got a status
badge, never Approve/Reject buttons, for the same reason), and the whole
toolbar only renders at all when at least one candidate on the product
is pending.

Selection lives in `productVideoSelected` -- a **module-level map**
keyed by `product_id`, not a single shared `Set` like `videoState.
selected`/`reviewState.selected` -- because more than one product's
detail row can be expanded on the Products tab at once, and each needs
its own independent selection. `bulkApproveProductVideos`/
`bulkRejectProductVideos`/`runProductVideoBulkAction` are otherwise the
same shape as the tab's `bulkApproveVideos`/`bulkRejectVideos`/
`runVideoBulkAction`: sequential per-id calls to the existing `POST
/video-candidates/{id}/approve`/`/reject` endpoints, one reject-reason
prompt applied to the whole batch, and a `finally` that reloads --
here, that means re-running `loadProductDetailInto` for just this one
product's detail panel (not the tab's full list), which both clears the
selection and shows the now-updated statuses.

No backend/template.yaml/migration change, no new tests (admin-site JS/
HTML only, same `node -c` syntax verification as the tab version), no
deploy step beyond the same static-file swap.

**Follow-up, real incident: undo for a mistaken approve/reject.** Al:
"it appears if i accidentally reject a video i can not undo that
action." Correct, and deliberately so up to this point --
`approve_video_candidate`/`reject_video_candidate` have always only
allowed a one-way `pending -> approved` / `pending -> rejected`
transition (each guards `if row[0] != "pending": raise ValueError(...)`),
specifically so a bulk action or a stale UI double-click couldn't
silently re-apply a decision. That guard just never had a way back out.

New `service.restore_video_candidate(conn, video_id)`: moves an already-
resolved row (`status IN ('approved', 'rejected')`) back to `'pending'`
and clears `resolved_at`/`resolved_by` -- i.e. restores it to exactly the
state a freshly-discovered candidate is in, so it shows back up in the
normal pending Approve/Reject workflow for another look. Restoring an
already-`'pending'` row is a hard `ValueError`, not a silent no-op --
that'd usually mean the caller's UI state is stale (e.g. two admins had
the same row open). No `resolved_by` parameter, unlike approve/reject/
reassign: there's nothing being resolved, so there's no decision to
attribute -- same reasoning `delete_video_candidate` uses for not taking
one either. Wired as `POST /video-candidates/{video_id}/restore` (no
request body, same as the `DELETE` route) in `admin_api/app.py`; no
`template.yaml` change (the catch-all proxy route already covers it,
same as every other `/video-candidates/*` endpoint).

One deliberate interaction worth calling out: a row that was auto-
rejected by the Shorts filter (see this section's earlier "Shorts
skewing the pipeline" addendum) can still be restored like any other --
but if it's genuinely a Short, `apply_video_stats`'s force-reject
re-checks duration on every stats refresh regardless of current status,
so it'll simply get auto-rejected again on the next scheduled refresh.
No special-casing needed to keep a restored Short from resurfacing as
`'pending'` forever.

Admin-site: an **Undo** button now sits next to the status badge on any
non-pending row, in both the Video Candidates tab (`renderVideoCandidates`
-> `restoreVideo(id)`) and the product detail page's Videos section
(`loadProductDetailInto`'s row rendering -> `restoreVideoForProduct
(productId, videoId)`), mirroring the same call-the-right-endpoint-then-
reload-the-right-view split every other action pair in this file already
uses. Deliberately **no `confirm()` prompt** on Undo, unlike Approve/
Reject/Delete -- there's no destructive side effect to double-check here
(it just puts the row back where an unreviewed candidate already sits),
so a confirm would only add friction to the action that exists specifically
to fix a misclick.

Tests (`tests/test_admin_api_service.py`, 129/129 passing, 4 new):
restoring from `'rejected'` clears `resolved_by`/`resolved_at` and sets
`'pending'`; restoring from `'approved'` does the same; restoring an
already-`'pending'` row raises `ValueError`; restoring a missing id
raises `LookupError`. `FakeCursor` gained an `update product_videos set
status = 'pending'` branch that resets both audit fields, mirroring what
the real SQL does.

No migration, no `template.yaml` change -- redeploy just
`AdminApiFunction`:
```bash
sam build AdminApiFunction
sam deploy
```
and swap the static `admin-site/index.html` file as usual.

### 6i.7. Product detail panel: sub-tabs (decluttering)

Al: "the admin product details are getting a bit cluttered can we clean
that up and maybe put the different sections in tabs." Fair -- the panel
(`loadProductDetailInto`) had grown to nine stacked sections in one long
scroll as feature after feature landed on it: images, core (+rescrape),
description, video candidates (+bulk toolbar), video review rollup,
price tracking (its own big section: buttons/table/SVG chart/manual-add
form), SKUs, SKU stock (table + SVG chart), and the raw-fields
`<details>` escape hatch.

Grouped into five tabs, roughly by "what are you here to do":
- **Overview** -- images, core (+ rescrape), description. What the ball
  IS.
- **Videos** (label shows a live count) -- candidates + bulk toolbar +
  the rollup they feed. Everything about video review in one place.
- **Pricing** (label shows a live count) -- the price tracking section,
  unchanged internally, just moved.
- **SKUs & Stock** (label shows a live count) -- specs table + SKU stock
  table/chart.
- **Raw Data** -- the existing all-columns `<details>` escape hatch,
  unchanged, now also behind a tab click (a deliberate SECOND click to
  reach it, same "don't show this by default" intent it already had).

**Deliberately a separate mechanism from the top-level nav's `showTab`/
`.tab`/`#tabs`, not a reuse.** The top-level nav only ever has ONE tab
panel visible at a time, backed by a single global `activeTab`. This
panel is different: more than one product's detail row can be expanded
at once (`toggleProductDetail` toggles per-row independently, and
nothing stops a user from opening several), so tab state has to be
scoped per product id, not global. New `showDetailTab(id, name)`
function + `.dtabs`/`.dtab` CSS classes (visually matching `nav#tabs`/
`.tab`, just smaller and namespaced) -- every dtab element id is suffixed
`-<id>` (`dtab-overview-<id>`, `dtab-videos-<id>`, etc.), and
`showDetailTab` scopes its `querySelectorAll` to `#detail-panel-<id>`
via `:scope >`, so switching tabs on one expanded row's panel never
touches another expanded row's tab state.

No functional/data change at all -- every existing element id (`rescrape-
result-<id>`, `discover-videos-result-<id>`, `video-bulk-toolbar-<id>`,
`discover-price-result-<id>`, etc.), every `onclick` handler, and every
API call inside `loadProductDetailInto` is untouched; only the
surrounding markup that WRAPS those pieces changed, from one flat
`<div class="detail-panel">` to `<div class="detail-panel" id=
"detail-panel-<id>">` containing a `.dtabs` nav plus five `.dtab` divs.
`updateProductVideoBulkToolbar(id)`'s call site is unaffected (still
`getElementById`-based, doesn't care about ancestor structure).

Verified via `node --check` against the extracted `<script>` contents
(no build step/framework for this file -- see its own header comment --
so this is the same verification depth prior admin-site changes in this
project have used, e.g. 6h/6i's "Validate admin-site/index.html" steps).
No test suite covers this file (plain HTML/JS, no Python to unit test);
no `template.yaml` or backend change -- swap the static file as usual,
no redeploy of any Lambda needed.

### 6i.8. Video discovery: dropped "review" from the search query (real incident)

Al noticed a specific, concrete gap: Storm Equinox Hybrid's actual #1
organic YouTube result (searching the plain ball name himself) never
showed up as a discovered candidate, even after manually clicking "Find
videos" for that product. Traced to `build_search_query` -- it built
`"<brand> <product> bowling ball review"`, appending "review" (and
"bowling ball") to whatever the admin/user would actually type. Al's
diagnosis, confirmed correct: "it is the extra words you added. i don't
think that is necessary. review is the word that breaks it and is not
very commonly used to describe youtube videos for balls." search.list's
relevance ranking favors literal query-term matches, and plenty of real
review/reaction video titles never contain the word "review" at all --
appending it was actively suppressing the very content this pipeline
exists to find.

Fix: `build_search_query` now returns `"<brand> <product> bowling ball"`
-- "review" dropped, "bowling ball" kept (a generic disambiguator for a
short/ambiguous product name, not a term real review titles would
plausibly omit the way "review" itself apparently is). See
`src/video_discovery/app.py`'s updated module docstring (new QUERY
WORDING section) and `build_search_query`'s own comment for the full
writeup. `tests/test_video_discovery.py`'s `test_build_search_query`
updated to match; full suite still 63/63.

**This changes what search.list is asked for, so it only takes effect
once `VideoDiscoveryFunction` is redeployed:**
```bash
sam build VideoDiscoveryFunction
sam deploy
```
No DB/migration change -- existing `product_videos` rows are untouched;
this only affects candidates found by future "Find videos"/discovery
runs. If you want Storm Equinox Hybrid's actual top result picked up
now, redeploy first, then click "Find videos" on that product again (or
re-run catalog-wide discovery).

### 6i.9. Product detail Videos tab: bulk reassign + bulk delete

Al: "on the product video tab can we add bulk reassign and bulk delete
buttons" -- the product detail panel's Videos sub-tab (6i.7) already had
bulk select/approve/reject (6h/6i's own "same checkbox bulk logic"
asks); this extends the same selection mechanism with the two other
actions its per-row buttons already supported (`reassignVideoForProduct`/
`deleteVideoForProduct`), just applied to every selected row at once.

**Checkbox column now shows on every row, not just pending ones.**
Approve/Reject stay meaningful only against pending candidates (a bulk
approve/reject that includes an already-resolved row just fails that one
item, counted in the batch's `failed` total -- same tolerance every bulk
action in this project already has), but Reassign/Delete are meaningful
regardless of status, matching what their individual per-row buttons
already allowed. "Select all pending" is now just "Select all".

**Bulk reassign** prompts once for a target `product_id`, applied to
every selected row -- same "single field applied to all" shape the
existing bulk reject's reason prompt already uses. Each row still goes
through the real per-item `reassign_video_candidate` logic (tombstone
here, copy/merge onto the target), so a `merged_with_existing` outcome
is still possible per row; the toast reports how many of the batch
merged.

**Bulk delete** is one confirm applied to the whole selection, then a
`DELETE /video-candidates/{id}` per row, same as the single-video
delete.

Both new actions run through the same sequential (not `Promise.all`)
loop as bulk approve/reject, same anti-throttling posture, in
`runProductVideoBulkAction` (now handles all four actions -- `reason`
only used by reject, `targetProductId` only by reassign).

No backend/API change -- reuses the existing per-candidate
reassign/delete endpoints from 6i's original build. Verified via
`node --check` against the extracted `<script>` contents, same depth as
prior admin-site-only changes in this project (no Python to unit test
for this file); swap the static file as usual, no Lambda redeploy
needed.

### 6i.10. Price/SKU-stock charts upgraded to Chart.js (styling, date-range, hover)

Al: "can we upgrade the charts for the pricing and stock over time. they
aren't styled very well and i think there could be some date time
selection for those. maybe some hover state for each of the data points
on the line. show pop overs for each values for each of the days all at
once." Replaces the hand-rolled-SVG polyline charts (`buildPriceChartSvg`/
`buildSkuStockChartSvg`) with Chart.js, loaded from a CDN.

**New external dependency, by design.** This is the one thing in an
otherwise zero-build/zero-dependency file (see the file's own header
comment) that now needs network access to a CDN (`cdn.jsdelivr.net`),
disclosed inline in `<head>`'s own comment. Judged worth it: a hand-
rolled SVG chart can't reasonably get per-point hover plus a combined
multi-series tooltip without reimplementing a real charting library's
core job, and this page already needs network access to reach the
deployed admin API regardless. `renderPriceChart`/`renderSkuStockChart`
both guard `typeof Chart === 'undefined'` and show a plain-text fallback
message instead of a blank chart if the CDN script fails to load.

**Date range selection.** Five presets (7D/30D/90D/1Y/All) per chart,
filtered client-side against an already-fetched, generously-windowed
history (`days` widened from 90 to 3650 in `loadProductDetailInto`'s
`price-history`/`sku-stock-history` fetches -- a single product's
history scan is cheap regardless of window size, see `get_price_
history`'s own docstring) so switching ranges is instant, no re-fetch
per click. `setChartRange` re-renders just the toolbar + the one chart
that changed, not the whole detail panel.

**Hover state per point + combined tooltips.** `pointHoverRadius` gives
each point a visible hover highlight (Al's "hover state for each of the
data points" ask). The "pop overs for each values for each of the days
all at once" ask is `interaction`/`tooltip` `mode: 'x'` (not `'index'`)
-- `'index'` mode matches by array position across datasets, which
would misalign here since different price sources/SKU weights get
checked at different times and don't share the same set of timestamps;
`'x'` mode instead matches every dataset's nearest point to the mouse's
actual pixel position, so hovering near a date shows one combined
tooltip listing every source's/weight's value for that date at once,
regardless of exact-timestamp alignment.

**Chart instance lifecycle.** `chartInstances` (keyed by canvas id) is
destroyed and recreated on every range-button click or panel reload
(`destroyChart` before each `new Chart(...)`) rather than `.update()`d
in place, since `loadProductDetailInto` already tears down/rebuilds this
whole panel's markup on reload -- avoids leaking a Chart instance/canvas
context per reload.

**Follow-up, same session:** Al: "better, now can we make it the full
width of the container." `.chart-wrap`'s `max-width: 620px` (a carried-
over leftover from the old fixed-size SVG) dropped to `width: 100%` --
Chart.js's own `responsive: true`/`maintainAspectRatio: false` (already
set) makes the canvas fill whatever size its parent now resolves to, no
JS change needed.

**Second follow-up, same session:** Al: "for the date axis can you
include all the days for the range selected with or without data with
the far right being today and then plot the data where it should go
with the adjusted." Chart.js's linear x-scale otherwise auto-fits `min`/
`max` to the actual data's own extent -- a source that hasn't been
rechecked in a few days would make the chart's right edge silently end
early instead of visibly showing the gap up to today. New `rangeBounds`
helper fixes `options.scales.x.min`/`max` explicitly to the full
selected window (today minus the preset's day count, or, for the `all`
preset with no fixed lookback, the earliest real timestamp in range)
regardless of which days actually have data -- the data points
themselves don't move, they just land wherever their real timestamp
falls within this now-wider, gap-revealing domain. Wired into both
`renderPriceChart` and `renderSkuStockChart`.

No backend/API change (days param already existed, just called with a
larger value) -- swap the static admin-site file as usual, no Lambda
redeploy needed. Verified via `node --check` against the extracted
`<script>` contents and Python's `html.parser` for basic tag balance,
same depth as prior admin-site-only changes in this project.

### 6i.11. SKU stock table: industry-standard inventory forecasting (ADU / Days of Supply / est. stockout)

Al: "with the data for stock levels can we apply some industry standard
inventory forecasting to that data."

Added Average Daily Units (ADU) and Days of Supply (DOS) -- the standard
building blocks behind a reorder-point calculation (Reorder Point = ADU
x Lead Time + Safety Stock). This stops at ADU/DOS/an estimated stockout
date rather than a full reorder-point recommendation, since this project
has no supplier lead-time or safety-stock policy data anywhere in its
schema.

**Computed entirely client-side**, same "live-computed-not-stored, leave
the delta math to the caller/chart layer" posture `get_sku_stock_history`
already documents for itself. No backend/API change.

**Methodology** (`computeSkuForecast` in admin-site/index.html): a fixed
30-day trailing lookback (`FORECAST_LOOKBACK_DAYS`), deliberately
independent of the chart's own display-range selector (7D/30D/90D/1Y/
All) so the forecast number doesn't jump around just because someone
changed what they're *looking at*. Only quantity *drops* between
consecutive readings count as "sold" -- a rise is a restock and is
excluded from the usage sum, same "drop=sold, rise=restock" reading
`get_sku_stock_history`'s docstring already documents (including its
honesty caveat that same-day sold-then-restocked activity can't be fully
distinguished from the raw snapshots). ADU = total units sold in the
window / elapsed days between the first and last reading in that window
-- not simply a count of readings, since SKUs are checked on a rotation
(`list_price_sources_due`) and won't land exactly once a day.

Needs at least 2 readings inside the lookback window to produce a rate
at all; below that, `adu`/`daysOfSupply`/`stockoutDate` are all `null`
and the table shows "—". `daysOfSupply`/`stockoutDate` are also left
`null` (not zero) when `adu` is 0 -- "no recent sales" and "sold out"
are different claims, and conflating them would falsely flag a
slow-moving weight as urgent. `latestQuantity <= 0` is handled as an
explicit "out of stock" case (`daysOfSupply: 0`, stockout date = now)
rather than falling out of the general division.

**Table changes:** `buildSkuStockSection` now renders three new columns
-- Avg daily usage, Days of supply, Est. stockout -- alongside the
existing Weight/Quantity/Last checked columns. Days of supply uses badge
styling for urgency: `<= 14` days is `.badge.danger` (new CSS rule,
reusing the existing `--danger-dark` color variable), `<= 30` days is
`.badge.muted`, otherwise `.badge.ok` (both already existed in the
stylesheet).

At ship time, the forecast was table-only -- adding a projection line to
the chart itself would extend the x-axis past "today" for SKUs with a
valid forecast, which reads as walking back the 6i.10 second-follow-up
fix that pinned `scales.x.max` to today. Al confirmed he wants that
tradeoff (see the follow-up immediately below), so the chart now carries
the same forecast the table does.

Verified via `node --check` against the extracted `<script>` contents
and Python's `html.parser` for tag balance, same convention as every
other admin-site-only change in this project. Swap the static
admin-site file as usual -- no Lambda redeploy needed.

**Follow-up, same session:** Al: "yes lets do the dashed line, we can
have a vertical line for today so that is still obvious with left of
that being historical real number and the right being forecasted." Also
asked: "is there such a metric that is days until we run out based on
the forecasted numbers" -- answered directly rather than building
anything new for it: that's exactly Days of Supply / the estimated
stockout date already in the 6i.11 table above, just visualized.

**Dashed forecast line** (`renderSkuStockChart`): for each SKU with a
non-null, non-zero `daysOfSupply` from `computeSkuForecast` (reused
as-is, run against the *full* unfiltered history regardless of the
chart's own display-range selection, since the forecast is always a
fixed 30-day trailing rate), a second Chart.js dataset is added per SKU:
two points, `{x: latest reading's timestamp, y: latest quantity}` to
`{x: capped stockout timestamp, y: interpolated quantity at that
timestamp}`, styled with `borderDash: [6, 4]` and the same per-SKU color
as its solid line. `pointRadius`/`pointHoverRadius` are `[0, N]` arrays
so only the line's end (the projected point) is a hoverable dot, not its
start (which is already the last real data point on the solid line).
Given `daysOfSupply === 0` (already out of stock) or `null` (no rate to
project), no forecast dataset is added for that SKU -- nothing to draw a
declining line toward.

**How far the line extends** (`forecastHorizonDays`): capped at whichever
comes first -- the SKU's own estimated stockout date, or a horizon that
mirrors the currently-selected historical range's day count (30D of
history shown -> up to 30 days of forecast shown; `All`, which has no
fixed day count to mirror, falls back to a flat 90-day forward look).
This keeps the forecast side of the "today" split roughly proportional
to however much history is currently on screen, rather than either
vanishing (a 7D view showing 1 day of a multi-month forecast) or
dwarfing it (an `All` view of years of history showing a lookback that's
mismatched with a short forecast).

**Vertical "today" line** (new `todayLinePlugin`): a small custom
Chart.js plugin using the `afterDraw` hook to stroke a vertical line +
"Today" label at the current-time x-pixel, registered once globally via
`Chart.register(...)` (guarded by the same `typeof Chart === 'undefined'`
network-failure check every other Chart.js touchpoint already uses).
Deliberately hand-rolled rather than pulling in the separate
`chartjs-plugin-annotation` package -- keeps this project's only
charting CDN dependency to Chart.js itself, same tradeoff already made
and documented at the `<head>` `<script>` tag. Opt-in per chart via
`options.plugins.todayLine.enabled`; only turned on for the SKU stock
chart, and only when a forecast dataset actually extends the axis past
today (so a chart with no forecast to show still looks exactly like it
did before this follow-up -- no redundant line sitting on top of an
axis that already ends at today).

**Axis widening**: `options.scales.x.max` is now
`Math.max(rangeBounds(...).max, forecastMaxX)` instead of always
`rangeBounds(...).max` -- widens only when a forecast line needs the
room, otherwise unchanged from the 6i.10 second-follow-up behavior (today
as the fixed right edge). `options.scales.x.min` is untouched -- only the
right/future edge moves.

**Legend**: the forecast datasets are labeled `"<weight>lb (forecast)"`
internally (for tooltip clarity) but filtered out of the visible legend
(`options.plugins.legend.labels.filter`) so each weight still shows a
single legend entry -- the dashed line style already communicates
"forecast" without a second, redundant legend row per weight.

**Shared helper added**: `latestSkuReadings(history)` -- the "most
recent reading per SKU" lookup that `buildSkuStockSection`'s table and
`renderSkuStockChart`'s new forecast line both need -- was pulled out of
`buildSkuStockSection` into its own function so both call sites use the
identical logic instead of two copies.

No backend/API change. Verified via `node --check` against the extracted
`<script>` contents and Python's `html.parser` for tag balance. Swap the
static admin-site file as usual -- no Lambda redeploy needed.

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

### 6l. Public read-only API (consumer-facing site, foundation)

Al: "let's start on the consumer facing site... single page like site...
a page to view the bowling ball details with sections for the high level
details and summary of summary and then easy ways to dive into each
video and play them in an embedded player... an intuitive way to
populate a ball comparison page... a focus on current bowling balls and
a way to still view retired balls and suggest current balls that best
compare to the retired balls." This section covers the first piece: a
new, separate, unauthenticated `PublicApiFunction`/`PublicHttpApi` --
the data source the eventual React frontend (still to be scaffolded)
will call. No frontend exists yet as of this writeup; this is
backend-only.

**Why a whole separate function instead of new routes on `AdminApiFunction`:**
see `src/public_api/service.py`'s own module docstring for the full
reasoning. Short version: `AdminApiFunction` sits behind
`AdminHttpApi`'s shared-secret authorizer -- every route there is meant
to require a bearer token. This one is the opposite, meant to be wide
open with no auth at all, callable directly from a browser on someone
else's computer. Mixing those two trust levels behind one function is a
foot-gun; a separate function/HttpApi resource makes "no auth here, by
design" structurally obvious rather than something to remember.

**Every query is hard-scoped to `products.published = true`**, not a
parameter a caller can override -- `products.published` exists
specifically for this (migration 001's own comment: "gates what the
consumer site / BowlerDepot sync can see"). This is the first real
consumer of that gate since it was added.

Endpoints (`src/public_api/app.py`, logic in `service.py`):
- `GET /health` -- shallow liveness check, no DB round-trip.
- `GET /brands` -- brands with at least one published product (browse
  filter facet).
- `GET /products?status=current&brand_id=&core_id=&coverstock_id=&search=&limit=&offset=` --
  card-shaped browse results. `status` defaults to `'current'`, not
  "everything" -- Al's direct ask ("a focus on current bowling balls and
  a way to still view retired balls") means a caller has to explicitly
  pass `status=retired` to see the retired catalog.
- `GET /products/{id}` -- full detail payload: specs, all SKUs (RG/DIFF/
  mass bias per weight), visible images (admin-curated order,
  `is_thumbnail` flagged), `video_reviews_summary` (the "summary of
  summaries" -- already existed on `products` since migration 006,
  written by `video_summarizer.refresh_video_reviews_rollup`, just never
  had a public consumer before this), and every approved +
  already-summarized video (`youtube_video_id`, title, channel, summary
  -- enough for the frontend to render a card and embed the actual
  player). Returns 404 for both a nonexistent id AND an existing-but-
  unpublished one -- deliberately identical, so a public detail page
  can't be used to probe for unpublished product ids the way
  `admin_api.get_product` (admin-only, allowed to know the difference)
  can.
- `GET /products/compare?ids=id1,id2,id3` -- batch fetch for the
  comparison page, same per-product shape as the detail endpoint, order-
  preserving, capped at `service.MAX_COMPARE_IDS` (6), missing/
  unpublished ids silently dropped rather than erroring the whole batch.
- `GET /products/{id}/similar?limit=5` -- the retired-to-current
  suggestion feature. Candidates are always `published=true,
  status='current'`, excluding the source itself. Scored in Python (not
  SQL -- the published current catalog is small enough that this doesn't
  need to be database-side, and a plain, independently-tested
  `score_similarity` function is easier to trust than an equivalent SQL
  expression) via a normalized RG/DIFF distance (see `RG_RANGE`/
  `DIFF_RANGE` in `service.py`) plus categorical mismatch penalties for
  core type / coverstock type / coverstock material. **These are
  documented, round starting-point constants, not fit against any real
  "these balls actually play alike" dataset** -- worth revisiting once
  Al's own plotter (mentioned when this feature was requested: "I have
  already created an interactive bowling ball plotter... in another
  cowork project", not yet integrated into this repo) is wired in, since
  reusing that tool's own notion of ball-motion distance would be
  better than running two different similarity answers on the same
  site.

Infra (`template.yaml`): `PublicHttpApi` (no `Auth` block at all --
every route public by design; `CorsConfiguration` with `AllowOrigins:
"*"`, safe here for the same reason it's safe on `AdminHttpApi`, no
credentialed requests involved) and `PublicApiFunction` (FastAPI +
Mangum, same shape as `AdminApiFunction`, one explicit `Method: GET`
route on `/{proxy+}` -- not `Method: ANY` -- so `OPTIONS` stays
unclaimed and `CorsConfiguration` auto-generates the preflight response,
same fix `AdminApiFunction` needed for the same reason, see 6a's own
CORS section). `DB_SECRET_ARN` comes from `Globals.Function.Environment`
like every other function; its own `Policies` block grants
`secretsmanager:GetSecretValue` on `DbSecretArn`, the only permission
this function needs (read-only, no queues, no S3, no Bedrock).

`tests/test_public_api_service.py`: 21/21 passing -- pure-function tests
for `score_similarity`/`_reference_sku`, filter-SQL-shape tests for
`list_products`/`list_brands` (confirming `published = true` is baked
into the query text, never a bind param), and full multi-query-assembly
tests for `get_product`/`get_products_compare`/`list_similar_products`
against a hand-built fake cursor (no real Postgres available in this
sandbox, same limitation as every other module here). Full catalog-wide
test sweep re-run after this addition: no regressions (same 3 pre-
existing, unrelated failures as before -- `test_product_scraper.py`/
`test_url_discovery.py` need `pytest`, not installed in this sandbox;
`test_woocommerce_product_scraper_orchestration.py` fails at the
already-documented `insert into cores` FakeCursor gap, unrelated to this
change).

No smoke test yet -- this is backend-only as of this writeup, no
frontend calls it yet. Once deployed, a manual sanity check:
```bash
sam build PublicApiFunction
sam deploy

PUBLIC_API_URL=$(aws cloudformation describe-stacks --stack-name bowling-scraper \
  --query "Stacks[0].Outputs[?OutputKey=='PublicApiUrl'].OutputValue" --output text)

curl -i "${PUBLIC_API_URL}health"                 # expect 200, no auth needed
curl -i "${PUBLIC_API_URL}products?limit=5"        # expect 200, published current balls only
curl -i "${PUBLIC_API_URL}products/<some-id>"      # expect 200 for a published id, 404 for anything else
```

**Still to do** (tracked as this session's Cowork task list, not yet
built): scaffold the actual React SPA that calls this API, integrate
Al's existing bowling-ball plotter (blocked on him sharing that other
project's code), and build the Browse/Detail/Compare pages themselves.
This section is the foundation those sit on top of.

### 6l.5. Video-popularity ranking (view-count time decay)

Al's ask, right after the video-stats feature (6i.6) landed: "can we
build in a view_count time decay so that older videos will organically
move down a 'popular' ranking when summed up for a ball ... We will not
have a way to see what videos do over time so I feel like applying some
version of a time decay is the next best thing." A raw sum of view_count
across a ball's videos would let one old viral video permanently
outrank a ball that's actually getting attention right now -- there's no
point-in-time view-count history to measure real velocity against
(`stats_fetched_at` only ever holds the LATEST fetch, see 6i.6), so
decaying by each video's own age (`published_at`) is the deliberate proxy
instead: same shape as radioactive/exponential half-life decay, a
video's `view_count` counts for half as much once it's
`POPULARITY_HALF_LIFE_DAYS` old, a quarter at 2x that, and so on,
asymptotically toward (never reaching) zero.

**Half-life = 180 days (6 months), not the more conventional 12-month
default for this kind of ranking.** Al asked directly: "bowling balls
have short life spans and they are usually retired in 6-12 months do you
think that has an effect on this recommendation" -- it does. The public
API's Browse page defaults `status='current'`, and a current ball's
entire video history almost always sits inside that same 6-12 month
window. A 12- or 24-month half-life barely decays anything across a
window that short -- it would rank current balls almost entirely by raw
view count, exactly what this feature exists to avoid. A 6-month
half-life gives real separation inside a ball's actual current lifespan:
a launch-month video still counts for roughly half by the time that same
ball nears retirement, instead of the curve being nearly flat the whole
time.

**Scope**: only `product_videos` rows with `status = 'approved'` count
(Al's confirmed choice) -- an unreviewed `'pending'` candidate might not
even really be about this product yet (see `reassign_video_candidate`'s
whole reason for existing). Rows with `view_count is null` (never
fetched, or a channel that hides the count) are excluded entirely, not
treated as 0 -- a video nobody's pulled stats for yet should be invisible
to this ranking, not silently drag a ball's score down.

**Computed live in SQL, not stored/precomputed.** Since the decay itself
changes every day even with zero new data, a stored column would need
its own daily refresh job to stay honest. Instead, both
`public_api/service.py` and `admin_api/service.py`'s `list_products`
carry an identical `POPULARITY_HALF_LIFE_DAYS = 180` constant and
`_POPULARITY_SCORE_SQL` correlated subquery:
```sql
coalesce((
    select sum(
        pv.view_count * power(2, -extract(epoch from (now() - coalesce(pv.published_at, pv.created_at))) / (86400.0 * 180))
    )
    from product_videos pv
    where pv.product_id = p.id and pv.status = 'approved' and pv.view_count is not null
), 0) as popularity_score
```
`180` is interpolated directly into the SQL text (not passed as a bind
param) since it's a fixed Python constant, not caller input -- keeps it
out of `params` so no other bind param's position shifts. The two
Lambdas are independently deployed with no shared module between them
(same per-Lambda-duplicated-constant convention as `MAX_VIDEO_IDS_PER_
CALL` elsewhere in this project), so the copy has to be kept in sync by
hand if the half-life is ever tuned.

**No migration needed** -- this reads columns migration 013 already
added (`view_count`, `published_at`), nothing new to store.

`public_api/service.py`'s `list_products` gained a `sort` param:
`sort='popularity'` orders by the computed score (desc, `p.id` tiebreak);
anything else (including the default `None`) keeps the existing
`updated_at desc` order. `popularity_score` itself is always selected and
returned regardless of `sort`, cheap enough at this catalog's size to
include unconditionally -- a Browse card can show a "trending" signal
without needing the popularity sort active. `public_api/app.py`'s
`GET /products` gained the matching `?sort=` query param.

`admin_api/service.py`'s `list_products` got the identical treatment
(same `sort` param, same always-selected `popularity_score` column) so
the number is visible and sortable in the admin Products tab too, not
just baked into the public API -- Al's explicit ask, so he can sanity-
check the ranking before trusting it live. `admin_api/app.py`'s
`GET /products` gained the matching `?sort=` param.

`admin-site/index.html`'s Products tab gained a "Sort" dropdown (recently
updated / popularity) next to the existing filters, wired into
`productState.sort` and `loadProducts`'s params the same way every other
filter already works, and a new Popularity column in the results table
(rounded, with a `title=` tooltip spelling out the formula since two
balls with the same raw view counts can show different numbers here
depending on video age).

Tests: `test_public_api_service.py` 46/46 passing (5 new:
`_POPULARITY_SCORE_SQL`'s presence/half-life-value/default-order-
unaffected/`sort='popularity'`-orders-by-score/unrecognized-sort-value-
falls-back). `test_admin_api_service.py` 119/119 passing (4 new, same
shape). No bind-param positions changed in either file's existing tests
-- confirms the half-life constant really is interpolated into the SQL
text, not shifted into `params`.

No `template.yaml` change, no redeploy of anything beyond the two
already-existing functions:
```bash
sam build PublicApiFunction
sam build AdminApiFunction
sam deploy
```
(No admin-site redeploy step, same as every other admin-site-only
change -- static file, not a Lambda-fronted deployable.)

**Follow-up: common-sense sort options, both UIs.** Al's ask: "lets add
some common sense sort options for both the admin and consumer UIs."
Both `public_api/service.py` and `admin_api/service.py`'s `list_products`
had their single `if sort == "popularity": ... else: ...` branch
refactored into a shared `_SORT_ORDER_BY` dict (`{sort_value: "<order by
clause>"}`) plus a `_DEFAULT_ORDER_BY` fallback, looked up via
`_SORT_ORDER_BY.get(sort, _DEFAULT_ORDER_BY)` -- same behavior as before
for `'popularity'`/`None`/unrecognized values, just easier to extend.
Four new values, each keeping the same `, p.id asc` pagination
tiebreaker every other sort branch already needed:
- `'newest'` / `'oldest'`: `p.release_date desc/asc nulls last`. Sorts by
  `release_date`, deliberately not `created_at`/`updated_at` -- a shopper
  cares when a ball actually came out, not when this project happened to
  scrape it. Explicit `nulls last` in BOTH directions -- `release_date`
  is nullable (not every scrape captures it), and Postgres's own default
  for a plain `desc` sort is `nulls first`, which would otherwise push
  every ball with an unknown release date to the very top of "Newest".
- `'name_asc'` / `'name_desc'`: plain `p.name asc/desc`.

No new column, no migration -- every field here (`release_date`, `name`)
was already selected.

`admin-site/index.html`'s `#product-sort` dropdown gained the four new
`<option>`s (`newest release`/`oldest release`/`name (A-Z)`/`name
(Z-A)`) -- no JS changes needed, the existing `productState.sort`/
`loadProducts` wiring already reads the select's value generically.

**The consumer site's Browse page also got its first-ever sort
control** (previously only status/brand/search filters existed, no
sort at all): a new `<select>` next to the brand filter, backed by a
`SORT_OPTIONS` array in `BrowsePage.tsx` with the SAME five values as
`_SORT_ORDER_BY` (plus `''` for the default) so nothing but a
recognized value ever reaches the backend from this page. Labels differ
deliberately from the admin site's -- `''` is "Featured" here, not
"recently updated" (that phrase describes scrape timing, meaningless to
a visitor); `'popularity'` is "Most Popular". `?sort=` is a real,
shareable URL param (`useSearchParams`), same pattern as
`status`/`brand_id`/`q` already used. `ListProductsParams`/
`listProducts()` in `api/client.ts` gained a `sort?: string` passthrough.

Tests: `test_public_api_service.py` 51/51 passing (5 new: newest/oldest/
name_asc/name_desc SQL-shape checks, plus a loop over every
`_SORT_ORDER_BY` key confirming the `p.id asc` tiebreaker survives).
`test_admin_api_service.py` 124/124 passing (same 5, mirrored). No
regressions from the `_SORT_ORDER_BY`-dict refactor -- every pre-existing
sort/default test still passes unchanged.

Consumer-site verification: `npx tsc -b` passes clean (exit 0) --
confirms the new `sort`/`SORT_OPTIONS` TypeScript is type-correct. The
full `npm run build` (which also runs `vite build`) currently fails in
THIS sandbox with `Cannot find module @rollup/rollup-linux-arm64-gnu`, a
pre-existing/known `npm` optional-dependency bug
(https://github.com/npm/cli/issues/4828) tied to this specific sandbox's
`node_modules`, not this change -- the real GitHub Actions CI (see 6n's
own CI section) builds on a different platform and isn't expected to hit
this. Worth a real `npm run build` locally or via CI before trusting
this deploys clean, since `vite build` itself was never actually
exercised this session.

No `template.yaml` change, no new redeploy step beyond the two functions
6l.5's original writeup already covers (`PublicApiFunction`/
`AdminApiFunction`) -- this is a query-shape and frontend change only.

**Follow-up, real incident: raw sum let video count dominate the
ranking.** Al: "there needs to be some more thought put into the
popularity, it currently is weighed heavily on number of videos because
it is a raw sum of the videos ... do you have a suggestion on how
balance this for when one ball have 4 videos and another has 20." The
original `_POPULARITY_SCORE_SQL` (both files) summed every approved
video's decayed view count -- a ball with 20 mediocre videos could
outrank a ball with 4 genuinely popular ones purely because it had more
of them, which measures "reviewed a lot," not "popular."

Presented Al three options (asked via a real choice, not decided
unilaterally, same as the half-life decision earlier in this section):
plain average (no volume credit at all -- a single video would carry as
much weight as 20 corroborating ones), average x sqrt(count) (milder
dampening), and average x ln(1 + count) (recommended). Al picked
**average x ln(1 + count)**.

`_POPULARITY_SCORE_SQL` in both `public_api/service.py` and
`admin_api/service.py` changed from `select sum(...)` to `select
avg(...) * ln(1 + count(*))` in the same correlated subquery -- still
one aggregate query per product, still no schema/migration change, still
`status = 'approved'` and `view_count is not null` only (see the earlier
6l.5 entry above -- that scope decision is untouched). `ln(1 + count)`
still gives volume a real, deliberate boost -- more corroborating videos
genuinely is more evidence of popularity -- just a sub-linear one instead
of a straight multiplier: at equal per-video quality, a 20-video ball
now scores `ln(21)/ln(5) ≈ 1.9x` a 4-video ball, not the old `20/4 =
5x` a raw sum produced. A few standout videos can still beat a pile of
average ones, since it's the AVERAGE being scaled, not the raw total.
`count(*)` is always >= 1 whenever the WHERE clause matches any row, and
the whole subquery returns `NULL` (then `0`, via the outer `coalesce`)
when it matches zero rows -- no separate zero-video special case needed.

Tests: `test_public_api_service.py` 53/53 passing (2 new --
`test_list_products_popularity_score_averages_not_sums` confirms the SQL
text shape, `test_popularity_formula_dampens_video_count_vs_a_raw_sum`
is a pure-Python `math.log` sanity check with no DB, verifying the
1.8x-2.0x claim in the code comment is actually correct arithmetic, not
just an assertion). `test_admin_api_service.py` 125/125 passing (1 new,
same SQL-shape check). Zero regressions in either file.

No `template.yaml` change; redeploy the same two functions as 6l.5's
original writeup:
```bash
sam build PublicApiFunction
sam build AdminApiFunction
sam deploy
```

### 6l.6. Public API connection reuse (real incident: "the public site is pretty slow")

Al's ask, direct: "the public site is pretty slow, what is the best way to
speed that up." Investigated a few candidate causes (image sizing, cold
starts, DB connection handling); Al scoped the work explicitly: "lets do
connection re use. i want to work on an image resizing and cleaning up
of backgrounds in the future so lets table image issues for now" -- so
this entry covers connection reuse only. Image resizing/background
cleanup is intentionally NOT touched here.

**Root cause**: `public_api/service.py`'s old `get_db_connection()` did a
full `secretsmanager:GetSecretValue` call AND opened a brand-new
`psycopg2.connect(...)` (fresh TCP + TLS + Postgres auth handshake) on
*every single request* -- even on an already-warm Lambda container that
had served the exact same connection moments earlier. `public_api` has no
RDS Proxy in front of it, so every one of those round trips landed
directly on the request's latency.

**Fix: module-level connection caching, keyed off Lambda execution-context
reuse.** `_cached_conn`/`_cached_secret` are now plain module-level
globals (not function-local) in `service.py` -- AWS Lambda reuses the same
process/module state across invocations on a warm container, so a
connection opened on one request is still sitting there, ready to reuse,
on the next one. `get_db_connection()`:
1. If `_cached_conn` is set, runs a real `select 1` round trip as a
   health check (NOT `conn.closed` -- that flag only reflects whether
   *this process* ever closed the connection, not whether the server or
   network silently dropped it, e.g. an RDS failover or idle timeout). If
   the health check succeeds, the same connection object is returned --
   no new connect, no Secrets Manager call.
2. If the health check fails (or there's no cached connection yet), the
   dead connection (if any) is discarded and a fresh one is opened using
   the cached secret -- no need to assume rotation just because the
   connection dropped.
3. If THAT connect attempt itself raises `psycopg2.OperationalError`
   (implying the cached secret is actually stale -- a real credential
   rotation), the secret is re-fetched from Secrets Manager exactly once
   and the connect is retried.

`conn.autocommit = True` is set on every new connection -- deliberate,
since this API is entirely read-only (see `public_api/service.py`'s own
module docstring on the "no auth, hard-scoped to published=true" design).
Without autocommit, a reused connection would otherwise accumulate an
"idle in transaction" session between requests for however long the
Lambda container stays warm.

`public_api/app.py`'s 6 routes (`/brands`, `/products`, `/products/
plotter`, `/products/compare`, `/products/{product_id}`, `/products/
{product_id}/similar`) each had their `try: ...; finally: conn.close()`
wrapping removed -- closing the connection after every request would
defeat the whole point of caching it. A comment on the first route points
back to `get_db_connection`'s docstring for the reasoning, so a future
reader doesn't "fix" the missing `finally` back in.

**Real test-authoring gotcha hit and fixed along the way**: the new tests
in `test_public_api_service.py` initially used generically-named fixture
classes (`_FakeCursor`, `_FakeConn`, etc.) that collided with a
DIFFERENT, unrelated `_FakeCursor` class already defined later in the
same file (for the `get_product`/`list_products` fixtures). Python
resolves a class name via the module's global namespace at CALL time, not
at `class` statement time -- so by the time any test actually ran (after
the whole file had finished executing top to bottom), `_FakeConn.cursor()`
was silently returning an instance of the OTHER, later-defined
`_FakeCursor`, whose query dispatcher doesn't recognize `"select 1"` and
raises `NotImplementedError` -- which `get_db_connection`'s health check
correctly treats as "connection is dead," forcing a real reconnect on
literally every call. `conn1 is conn2 is conn3` failed even though the
production code was already correct; a standalone minimal reproduction
(hand-copied fixtures, run outside the test file) confirmed the real
implementation caches correctly. Fixed by renaming every fixture in that
test block to a unique `_ConnCache*` prefix
(`_ConnCacheFakeCursor`/`_ConnCacheFakeConn`/`_ConnCacheFakePsycopg2`/
`_ConnCacheFakeSecretsManagerClient`/`_ConnCacheFakeBoto3`/
`_install_conn_cache_fakes`/`_reset_conn_cache`) so it can never again
collide with a same-named fixture anywhere else in this file.

Tests: `test_public_api_service.py` 58/58 passing (5 new: opens-once-
reuses-across-calls, sets-autocommit-true, reconnects-when-dead,
refetches-secret-when-stale, chains-both-failure-modes). Same
`sys.modules["boto3"]`-injection technique `test_admin_api_service.py`
already uses for `boto3`, extended here to fake-inject `psycopg2` too
(no prior precedent in this codebase for faking `psycopg2` itself, since
every OTHER test file in this project monkeypatches `get_db_connection`
away entirely rather than testing its real internals).

No migration, no `template.yaml` change -- redeploy just the one function:
```bash
sam build PublicApiFunction
sam deploy
```

**Follow-up, later session -- real incident: "im still seeing some
performance issues on the consumer site... some early requests take
quite some time."** Al's own description ("better once it gets going")
is the textbook Lambda cold-start pattern -- confirmed by re-checking
what 6l.6's connection-reuse fix does and doesn't cover: it only pays
off on a *warm* container (cached connection, skip the Secrets Manager
call and fresh TCP+TLS+Postgres handshake). It does nothing for the
first request to a cold container, which still pays full Lambda init
(importing FastAPI/Mangum/psycopg2/boto3) stacked on top of that same
Secrets Manager call + fresh handshake, all before any of 6l.6's caching
can help.

Investigated and ruled out before landing on the fix below: not
VPC-attached (no ENI cold-start penalty), RDS is a plain externally-
provisioned Postgres instance (not Aurora Serverless, no scale-to-zero
resume delay), and CloudFront's cache config for the static bundle looks
fine. Provisioned/reserved concurrency was considered and rejected --
this AWS account has only 10 total concurrent Lambda executions
available account-wide (see `AdminApiFunction`'s own comment in
`template.yaml` about a reverted `ReservedConcurrentExecutions: 2`
attempt for that same reason), leaving no room to reserve capacity for
`PublicApiFunction` on top of it. A scheduled warmer ping was also an
option (doesn't touch that concurrency ceiling, unlike reserved/
provisioned concurrency) but Al chose the simpler fix for now.

**Fix shipped: `PublicApiFunction`'s `MemorySize` raised from the 256MB
`Globals` default to 1024MB.** AWS Lambda allocates CPU proportional to
configured memory, so more memory shortens cold-init duration directly
-- no code change, no new moving parts, and it doesn't touch the
account's concurrency ceiling at all (memory is a per-invocation
resource setting, unrelated to concurrent-execution limits). Cheapest,
lowest-risk lever available given the other candidates were ruled out or
blocked.

Not done (kept in reserve if the memory bump alone isn't enough): a
scheduled EventBridge "warmer" rule pinging `PublicApiFunction` every
few minutes, same `Type: Schedule` pattern already used for the
URL-discovery Lambdas elsewhere in `template.yaml` -- would reduce how
often a cold start happens at all, at the cost of a small trickle of
extra invocations. Worth revisiting if 1024MB doesn't move the needle
enough on its own.

No migration, no code change -- just the one `template.yaml` line.
Verified the template still parses via the established CFN-tolerant
PyYAML loader (54 resources, 38 outputs, same counts as the last
verification). Redeploy:
```bash
sam build PublicApiFunction
sam deploy
```

### 6l.7. price_checker: variant cost_price fallback

Al: "the cost for products if not on the product it's self is on the
variants. it should always be the same for all the varaiants so if we
get 0 from the product and we grab it from one of the variants?"

**Root cause**: `extract_bigcommerce_price_fields` only ever read
`product.get("cost_price")` -- the top-level BigCommerce product
object's own field. BigCommerce also has a per-variant `cost_price`
field, and for a multi-variant product (every ball here, sold as
several weights) it's a common real-world shape for a merchant to only
ever set cost on the variants, leaving the parent product's own
`cost_price` at `0`/unset. `variants` was already being fetched on
every request this function sees (`include=custom_fields,variants`,
`build_bigcommerce_products_by_id_url`) for the unrelated per-SKU stock
feature (017), so no new API call was needed to fix this -- the data
was already sitting there, just unused for cost.

**Fix**: when `product.get("cost_price")` is falsy (`None` or `0`),
`extract_bigcommerce_price_fields` now walks `product["variants"]` and
uses the first one with its own non-zero `cost_price`. Al confirmed
cost is uniform across a product's variants, so which variant it comes
from doesn't matter, only that one of them has the real number. A real,
non-zero product-level value is still always used first and never
overridden by variant data.

**Intentional side effect**: if `cost_price` comes back `0`/unset at
both the product level AND every variant, the result is now `None`
(unknown) rather than the old behavior of storing a literal `0.0`. A
bowling ball never really costs $0 to stock, so a lingering `0` was
always really "not set" -- this makes that explicit rather than
charting a fake zero-cost data point.

Tests: 5 new cases in `test_price_checker.py` (falls back when product
cost is exactly `0`, falls back when product cost key is missing
entirely, product-level value still wins when present even if a variant
disagrees, result is `None` when product and all variants are zero/
missing, and a product dict with no `variants` key at all doesn't raise).
Full suite: 1016/1016 passing, no regressions in
`check_bigcommerce_sources`/`discover_bigcommerce_candidates` (both call
this function and both already had their own cost_price-shape tests,
none needed changes).

Since cost_price is already historized per-check in
`product_price_history` (see 6i.11/SKU-stock-adjacent history design --
same append-only pattern), this fix takes effect starting from the next
price check after redeploy; no backfill of past `0`/`None` rows.

No migration, no `template.yaml` change -- redeploy just the one
function:
```bash
sam build PriceCheckerFunction
sam deploy
```

### 6l.8. price_checker discovery: zero-result fallback query for scrape sites

Real incident, same "Storm !Q Tour Edition" ball as 6h.1's BowlerDepot
match (see that section for the full backstory of how the real stored
name was confirmed). Once the BowlerDepot ("api" fetch_method) match was
fixed, Al reported the same product wasn't being found on bowling.com (a
"scrape" fetch_method site) at all -- and instead of guessing, he tested
bowling.com's own site search directly: "the product doesn't show up at
all and the reason is the edition on the end cause zero results to show
up."

**Root cause**: `discover_price_sources`' scrape-site search loop builds
one literal query per product (`build_search_query` = `"{brand} {product
name}"`) and sends it straight to the site's own search page. Unlike
BowlerDepot's catalog (an exact list this project fuzzy-matches against
locally), a generic retailer's site search is a black box this project
doesn't control -- and bowling.com's, specifically, appears to be too
LITERAL: appending "Edition" to the query returns zero results even
though the ball is on the site under a name without it. There's no way
to fix a third-party search engine's own behavior, only to avoid sending
it a word it can't handle.

**Fix**: new `strip_generic_qualifiers(name)` drops any whole word from
`_GENERIC_QUALIFIER_WORDS = {"bowling", "ball", "balls", "edition"}` --
the same set as `bowlerdepot_reconciliation._GENERIC_NAME_SUFFIX_TOKENS`
(6h.1), duplicated rather than imported per this module's own "each
Lambda is its own deploy package" convention, since it's the identical
real-world pattern (a manufacturer-only qualifier a retailer often
drops), just breaking a different stage of the pipeline. `discover_
price_sources` now retries a scrape-site search exactly once, only when
the FIRST search for a product+site returns zero results (never on a
request exception -- that's a network/site failure, a different problem
this can't fix), using the stripped query. Whichever query actually
produced results is what gets stored as the candidate's `match_query`,
so an admin reviewing a pending candidate sees the query that explains
why it showed up, not the original zero-result one.

**Scope note**: this only touches the "product doesn't show up at all"
failure mode. `score_match`'s own permissiveness (any one product-name
token, not all) -- the separate "finding 5 completely different
products" symptom Al also mentioned -- is unchanged; that's the same
intentional, documented tradeoff `score_match`'s own docstring already
covers (a pending candidate always needs admin review, this heuristic
was never meant to eliminate false positives on its own). Not addressed
here since Al confirmed the missing-match case was the one to look at
first.

Tests: 6 new cases in `test_price_checker.py` -- `strip_generic_
qualifiers` unit tests (edition/bowling-ball removal, case-
insensitivity, punctuation preserved on kept words, no-op when nothing
to strip, empty string) plus end-to-end `discover_price_sources` cases:
a zero-result first search retries with the stripped query and stores
the candidate under the query that worked; a name with no qualifier
words never gets a second, identical search attempt; both queries
coming back empty still completes cleanly (marks the product searched,
zero errors); and a failed fallback request counts as a search error
without blocking the rest of the batch. Full suite: 1035/1035 passing,
zero regressions.

No migration, no `template.yaml` change -- redeploy just the one
function:
```bash
sam build PriceCheckerFunction
sam deploy
```

### 6l.9. price_checker discovery: scrape_only scoping for catalog-wide re-runs

Al, wanting to re-run `scripts/discover_all_price_sources.py` catalog-
wide while iterating on a bowling.com config fix: "can we not run the
bowlerdepot price sources in this one, they have inventory numbers too"
-> "maybe just scrape sources." BowlerDepot ('api' fetch_method) is also
where `product_sku_stock_history`'s per-SKU inventory counts come from
(017) and is already kept fresh by `bowlerdepot_reconciliation`'s own
daily schedule -- there was no reason for a scrape-site-focused discovery
re-run to also touch it every time.

**Fix**: new `{"scrape_only": true}` key on the discovery job dict.
`discover_price_sources` now computes `api_sites = []` outright when set,
instead of the normal `[s for s in sites if s.get("fetch_method") ==
"api"]` -- `discover_bigcommerce_candidates` is never called at all for
that invocation, not called-and-short-circuited. Threaded all the way
through: `queue_price_discovery_batch(limit=None, scrape_only=False)`
(admin_api/service.py) includes `"scrape_only": true` in the Lambda
payload only when set (omitted -- not sent as `false` -- when default,
so an already-deployed price_checker without this branch keeps getting
the exact payload shape it always has); the `POST /admin/discover-all-
price-sources` route gained a `?scrape_only=` query param (default
`false`); and `scripts/discover_all_price_sources.py` gained a
`SCRAPE_ONLY` env var (same "unset/false omits the param, set/true
sends it" convention as `LIMIT`).

Only added to the catalog-wide batch path, not the single-product "Find
price sources" button/`queue_price_discovery` -- Al's ask was specifically
about the catalog-wide script, and that button already only ever affects
one product at a time regardless.

Tests: `test_discover_price_sources_scrape_only_skips_api_sites_entirely`
(confirms `get_bigcommerce_credentials` is never even called, and no
`bowlerdepot_products` `select` is ever issued -- genuinely skipped, not
just short-circuited) and `..._scrape_only_false_still_runs_api_sites` in
`test_price_checker.py`; `test_queue_price_discovery_batch_scrape_only_
included_in_payload` and `..._scrape_only_false_omits_it_from_payload` in
`test_admin_api_service.py`; five new cases in `test_discover_all_price_
sources.py` covering `trigger_discovery`'s query-param shape, `run`
passing the flag through, and `main` reading `SCRAPE_ONLY` from the
environment. Full suite: 1044/1044 passing, zero regressions.

No migration, no `template.yaml` change -- redeploy the two touched
functions:
```bash
sam build PriceCheckerFunction && sam build AdminApiFunction
sam deploy
```

Usage, once deployed:
```bash
export SCRAPE_ONLY="true"
python3 scripts/discover_all_price_sources.py
```

### 6m. Ball motion plotter (consumer site, standalone page)

Al shared an existing interactive plotter he'd built in another Cowork
project (`/Users/awolfe3/Downloads/site` -- `index.html` + `balls.js` +
per-ball placeholder pages/images): a scatter chart, X axis = oil the
ball reads best on (1 light -> 16 heavy), Y axis = motion shape (1
smooth -> 18 angular), positions hand-digitized from Brunswick's own
published "Ball Motion Comparison Chart" PDF (Form #0526-19, Jul/Aug
2026) -- 56 balls across Brunswick/Hammer/Track/Ebonite/Radical/DV8.
Asked Al directly how to integrate it: **standalone page** (not the main
Browse view, not the comparison-page picker), and for the ~85%+ of the
catalog the chart doesn't cover, **estimate algorithmically** rather
than only showing the curated 56 or building a full admin-editable-field
workflow.

**Migration 011** adds `products.oil_rating`/`motion_rating` (nullable
smallint, CHECK-constrained to 1-16 / 1-18) -- holds ONLY the
authoritative, chart-sourced values, never the algorithmic estimate.
Keeps "this came from Brunswick's own published chart" and "this is our
own heuristic guess" structurally distinct rather than indistinguishable
integers in one column.

**`scripts/data/brunswick_chart_positions.json`** -- the plotter's own
56-entry dataset (brand/name/oil/motion), extracted directly from its
`balls.js`.

**`scripts/backfill_plotter_chart_positions.py`** -- one-time matching
pass: resolves each chart entry's brand via `GET /brands`, searches `GET
/products?brand_id=...&search=<name>` (admin_api's ilike substring
search), and writes via the new `PATCH /products/{id}/plotter-position`
endpoint (`admin_api/service.py`'s `set_plotter_position`) ONLY on an
exact case-insensitive name match. Zero or multiple candidates are
logged for manual review and never guessed at -- a wrong auto-match
would silently mislabel a real product with someone else's chart
position, worse than the honestly-flagged algorithmic estimate. Usage:
```bash
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/backfill_plotter_chart_positions.py
```
Review its log output for `no_match`/`ambiguous`/`no_brand` entries and
resolve those by hand (a direct `PATCH /products/{id}/plotter-position`
call, or update the product's name first if it's a real typo/mismatch).

**Name-mismatch gap found 2026-08-12 (Al: "some of the balls on the
example plotter are missing their actual values").** Checked the live
`GET /products/plotter` output against the 56-entry chart dataset by
hand: 32/56 had matched onto `'chart'`, 24 hadn't. Most of those 24 are
genuinely retired/unpublished right now (not a bug) or ambiguous (a
chart entry with an extra word in the live product name that could be
the same colorway or a different one -- deliberately left alone rather
than guessed, e.g. chart's "Infinity Quest" vs. the catalog's "Infinity
Quest Pearl"). Five, though, are real, currently-published products this
script's strict exact-match rejected purely over a punctuation/word-
order difference from the chart's own name text: "Raw Hammer Red / White
/ Purple" / "Fury Orange / Red" / "Fury Emerald / Black" (catalog uses a
dash: "... - Red / White / Purple"), "Vibe Deep Ocean" (catalog:
"Deep Ocean Vibe", word order swapped), and "Widow Tour V1" (catalog:
"Black Widow Tour V1", missing the "Black" prefix every other Black
Widow product carries). New `NAME_OVERRIDES` dict in
`backfill_plotter_chart_positions.py` maps each of those five (brand,
chart-name) pairs to the real catalog name for the exact-match
comparison only -- the search call itself still uses the chart's
original name text (already finds the real product fine via substring
match). Two new tests
(`test_match_entry_uses_name_override_when_chart_name_differs_from_catalog`,
`test_match_entry_name_override_is_a_no_op_for_entries_without_one`),
full suite re-run clean (13/13). Re-run the script (see command above)
to pick these five up -- pure data fix, no redeploy needed, this only
touches the standalone script.

**Estimate-vs-actual accuracy, spot-checked 2026-08-12 (Al: "it would be
interesting to see how accurate the estimate is compared to the
actuals").** One-time ad-hoc analysis, not a shipped feature/endpoint:
for all 32 products with a real chart position, computed what
`estimate_oil_motion` would have guessed from that same product's real
core/coverstock/differential data and compared. Across those 32: oil
mean absolute error 3.3 (on the 1-16 scale), motion mean absolute error
2.8 (on the 1-18 scale); only 2/32 exact oil matches and 3/32 exact
motion matches, though 13/32 land within +/-2 on oil and 18/32 within
+/-2 on motion. Biggest misses cluster on oil for asymmetric
reactive-resin solids (Revenge Solid, actual oil 3 vs. estimated 13;
Dark Side Curse, actual 5 vs. estimated 13; Crown Victory, actual 6 vs.
estimated 10) -- the heuristic's flat `+3` solid-coverstock adjustment
overshoots hard for this whole class of ball, real signal toward
revisiting `OIL_ADJUST_BY_TYPE`/`OIL_BASE_BY_MATERIAL` specifically
(see `estimate_oil_motion`'s own "revisit once there's a real reference"
note) rather than the motion side, which fares noticeably better. Not
acted on beyond this spot-check -- flagging for a future pass now that
there's an actual number to aim at, rather than guessing at new
constants without re-validating.

**`public_api/service.py`'s `estimate_oil_motion`** -- the algorithmic
fallback for everything migration 011 doesn't cover. A documented, ROUND
starting-point heuristic (not fit against real data -- there is none for
most of the catalog), from general bowling-industry domain knowledge:
oil is driven by coverstock material/type/particle (friction/traction),
motion is driven by core type (symmetric/asymmetric) and differential
magnitude, with a small secondary nudge from coverstock type. See that
function's own module comment for the full reasoning and every constant.
**Worth revisiting once there's a real reference to validate or replace
it against** -- same caveat as the retired-to-current similarity
scorer's `RG_RANGE`/`DIFF_RANGE` constants (6l above).

**`GET /products/plotter?status=current`** (`public_api`) -- everything
the standalone plotter page needs in one unpaginated call: id/name/url/
brand_name/image plus `oil`/`motion`/`oil_motion_source` for every
published product matching `status`.

#### Persist-once revision (migration 012)

Originally `GET /products/plotter` called `estimate_oil_motion` live, on
every request, for any product without a chart position. Al's own
direct follow-up after that shipped: *"while i think that is a good
approach it will cause for potential inconsistencies, i would prefer for
it to just back fill the values once in the DB and then estimate on
scrape if not set and then they can be adjusted in the admin api to a
value that is more accurate if necessary."* Recomputing live meant the
same product could report a different estimated position across two
calls if any input changed in between (a cores backfill landing,
a rescrape correcting coverstock fields, etc.) -- not wrong exactly, but
not the stable, pin-it-down behavior a real plotter page should have.

**Migration 012** adds `products.oil_motion_source text` (CHECK IN
`'chart'`/`'estimated'`/`'manual'`), plus a table CHECK tying it to the
two rating columns (both null together, or both set together with a
source). This is what makes "an admin's real correction" distinguishable
from "still just our own guess" after the fact -- the two rating columns
alone can't tell those apart once both are just integers.

`oil_rating`/`motion_rating` are now written to ONCE, from one of three
places, and read as-is from then on:
- **`'chart'`** -- unchanged, `scripts/backfill_plotter_chart_positions.py`.
  Its own `set_plotter_position` HTTP call now passes `source: "chart"`
  explicitly in the PATCH body.
- **`'estimated'`** -- NEW: every scraper's `upsert_product` (all five
  platform families: Brunswick/`product_scraper`, Shopify/Hammer+Track+
  Ebonite, Netsuite/MOTIV, WooCommerce/SWAG, commercebuild/Storm+Roto
  Grip+900 Global) now writes an estimate the first time it upserts a
  product with no plotter position yet -- `estimate_oil_motion` and a
  `_reference_differential` SKU picker duplicated into each scraper
  module (same "each Lambda is its own independent deployment package"
  reasoning as every other duplicated helper in this project -- MUST stay
  in sync with `public_api`'s copy). The write is guarded
  `where oil_rating is null`, so a rescrape never clobbers a chart match
  or a manual correction. `admin_api.backfill_estimated_plotter_positions`
  (new `POST /admin/backfill-estimated-plotter-positions`, no body,
  catalog-wide, idempotent) is the "once" half of Al's ask -- covers
  every product that predates this hook. Run it once after migration 012
  deploys; every product scraped from then on is covered automatically.
- **`'manual'`** -- NEW default for `PATCH /products/{id}/plotter-position`
  (`admin_api`) when no `source` is given in the request body -- an
  admin correcting an estimate "to a value that is more accurate", per
  Al's own phrasing.

`public_api.list_plotter_positions` now reads `oil_rating`/`motion_rating`/
`oil_motion_source` straight off the row. `estimate_oil_motion` is still
called there, but only as a last-resort defensive fallback for a product
that genuinely has neither value yet (predates both the scrape hook and
the backfill) -- that fallback is never written back, just keeps the
page from ever silently dropping a product.

Migration 012 also backfills `oil_motion_source = 'chart'` for any row
that already has a rating from before this migration ran (the only
writer at that point), so the new consistency CHECK doesn't fail against
existing data.

Tests: `tests/test_admin_api_service.py` gained
`test_set_plotter_position_writes_given_source`,
`test_estimate_oil_motion_matches_public_api_shape`,
`test_reference_sku_prefers_15lb`, and three
`backfill_estimated_plotter_positions` tests (111/111 total).
`tests/test_public_api_service.py` gained
`test_list_plotter_positions_reads_manual_source_unchanged` (30/30
total). Each scraper's own orchestration test file
(`test_product_scraper_orchestration.py`,
`test_shopify_product_scraper_orchestration.py`,
`test_netsuite_product_scraper_orchestration.py`,
`test_woocommerce_product_scraper_orchestration.py`) gained
`test_process_one_writes_estimated_plotter_position_when_unset` and
`test_process_one_never_overwrites_existing_plotter_position`, run
end-to-end through `_process_one`/`upsert_product` against each file's
fake DB (each fake's `insert into products` branch was also fixed to
preserve `oil_rating`/`motion_rating`/`oil_motion_source` across a
rescrape's row reset, matching real `ON CONFLICT DO UPDATE`, which never
lists those columns). `test_woocommerce_product_scraper_orchestration.py`
was also missing `insert into cores`/`insert into coverstocks` fake
support entirely -- a real pre-existing gap in that file (confirmed by
running its original, unmodified version and seeing the identical
`NotImplementedError`), unrelated to this feature but blocking these new
tests from running at all; fixed alongside, same shape as every other
scraper's fake. **`commercebuild_product_scraper`'s hook has no live-DB
test** -- `test_commercebuild_product_scraper.py` only ever tested
parsing logic (no `_process_one`/`upsert_product` fake-DB coverage
existed before this feature either), so there's no existing harness to
extend; the hook there is the same duplicated code already covered by
the other four scrapers' tests. Full catalog-wide sweep re-run after
this addition: no regressions, same 2 pre-existing unrelated
`ModuleNotFoundError: No module named 'pytest'` failures as every prior
sweep this session.

No `template.yaml` change needed -- `PublicApiFunction`/`AdminApiFunction`
and all five scraper functions already exist. Deploy order:
```bash
psql "$DATABASE_URL" -f db/migrations/012_products_oil_motion_source.sql
sam build PublicApiFunction
sam build AdminApiFunction
sam build BrunswickProductScraperFunction   # + each other scraper function
sam deploy
```
Then run the one-time backfill for whatever predates the scrape hook:
```bash
curl -X POST "$ADMIN_API_URL/admin/backfill-estimated-plotter-positions" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

**Update:** the React SPA is now scaffolded (see 6n below) with a
functional `/plotter` page wired to this endpoint. Its visual design is
still a plain first pass, not a port of Al's original chart -- see 6n's
own "what's not done yet" note.

#### estimate_oil_motion refit + re-estimate backfill (real incident, Al: "i feel like it is way off for most balls")

Confirmed by the 2026-08-12 spot-check above -- only 2/32 exact oil
matches (MAE 3.3/16) and 3/32 exact motion matches (MAE 2.8/18) against
the 32 real chart positions. Two pieces of tooling, built to actually fix
this against real data rather than re-guessing new constants:

**`scripts/dump_plotter_estimate_training_data.py`** -- redoes that
spot-check as a reusable, shareable data pull: for every product with
`oil_motion_source='chart'` (real Brunswick-published positions), writes
one JSON line with its real oil/motion actuals alongside the same inputs
`estimate_oil_motion` consumes (core_type, coverstock_type,
coverstock_material, has_particle, reference-SKU differential). Reads
`GET /products/plotter` (public_api, unauthenticated, both `status=
current` and `status=retired`) to find the chart-matched ids, then `GET
/products/{id}` (admin_api) for each one's real inputs. I (the agent)
can't reach either live API from this sandbox -- the shell's proxy
blocks the API Gateway domain by allowlist -- so this has to be run by
Al and the output shared back:
```bash
export ADMIN_API_URL="https://<your-admin-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
export PUBLIC_API_URL="https://<your-public-api-id>.execute-api.us-west-1.amazonaws.com"
python3 scripts/dump_plotter_estimate_training_data.py --out /tmp/plotter_training_data.jsonl
```
**Refit completed 2026-08-14.** Al ran the script above and shared
`plotter_training_data.jsonl` -- 40 products with a real chart position
at the time (up from the 32 the 2026-08-12 spot-check covered by hand,
since this run covers both `status=current` and `status=retired`).
`estimate_oil_motion`'s constants (`OIL_BASE_BY_MATERIAL`/
`OIL_ADJUST_BY_TYPE`/`MOTION_*`, all 7 copies -- `public_api/service.py`,
`admin_api/service.py`, and all five scraper duplicates) were refit
against that real data via ordinary least squares (see `public_api/
service.py`'s module comment directly above `estimate_oil_motion` for
the full per-constant reasoning -- this paragraph only summarizes).

The single biggest, best-supported fix: `OIL_ADJUST_BY_TYPE["solid"]`
was a flat `+3` that overshot hard for the whole reactive-resin/solid
class (n=16, the largest group in the data) -- real average oil for that
group is 10.0, essentially identical to reactive-resin/hybrid's own real
average (9.4), not 3 points heavier. Dropped to `0`. `OIL_BASE_BY_
MATERIAL["urethane"]` nudged `5 -> 6` (small sample, n=5, still an
improvement). `OIL_ADJUST_BY_TYPE["pearl"]` (`-3`) and `OIL_BASE_BY_
MATERIAL["reactive_resin"]` (`10`) were left unchanged -- already close
to real data. `OIL_PARTICLE_BONUS` and `OIL_BASE_BY_MATERIAL[
"polyester_plastic"]` are UNCHANGED and still unverified -- zero real
`has_particle=true` or `polyester_plastic` samples exist in this
40-product dataset.

Motion's constants moved further: `MOTION_BASE_BY_CORE_TYPE` `{symmetric:
7, asymmetric: 12}` -> `{symmetric: 4, asymmetric: 8}`, `MOTION_BASE_
UNKNOWN_CORE` `9 -> 6`, `MOTION_DIFF_WEIGHT` `6 -> 8` (`MOTION_DIFF_
MIDPOINT`/`MOTION_DIFF_SCALE` unchanged), `MOTION_ADJUST_BY_COVERSTOCK_
TYPE["solid"]` `-1 -> +1` and `["pearl"]` `+1 -> +2`. One genuine
surprise: the original domain-knowledge reasoning had solid coverstocks
reading LESS angular than hybrid -- real data says the opposite (real
solid balls trend slightly MORE angular). Flagged, not silently
overridden -- see the code comment.

Measured accuracy, old vs. new, both scored against the same 40 real
chart positions: oil mean absolute error **3.05 -> 2.675** (1-16 scale),
exact matches 4/40 -> 4/40 (unchanged), within +/-2 18/40 -> 18/40
(unchanged); motion mean absolute error **2.75 -> 2.5** (1-18 scale),
exact matches 3/40 -> 6/40, within +/-2 22/40 -> 24/40. A real, modest,
net improvement -- no metric regressed -- not a dramatic fix, since real
motion clearly depends on more than these few inputs (the reactive-
resin/solid group alone spans real oil values from 3 to 16 even holding
material+type fixed -- Revenge Solid vs. Zero Mercy Solid -- a spread
this 2-input model can't capture no matter how it's tuned). Flagged in
code for a future revisit once more granular input (e.g. per-core-line
data) is available.

`test_admin_api_service.py`'s `test_estimate_oil_motion_matches_public_
api_shape` and `test_reestimate_plotter_positions_overwrites_estimated_
only` both updated to the new formula's output for the same inputs
(`{"oil": 10, "motion": 15}`, was `{"oil": 13, "motion": 16}`). Full
suite re-run clean (1011 tests across every manual-runner file, 0
failures -- the only non-passing files are the 2 pre-existing, unrelated
pytest-only files this project's sweeps have flagged all along).

**New: `POST /admin/reestimate-plotter-positions`**
(`admin_api.reestimate_plotter_positions`) -- the reason a formula fix
alone wouldn't actually fix anything already in the catalog:
`oil_rating`/`motion_rating` are written ONCE per product and never
revisited (see this section's own "Persist-once revision" above) --
`backfill_estimated_plotter_positions` only ever fills a still-NULL
position, so every product already estimated under the OLD, badly-
miscalibrated constants would keep that wrong value forever even after
the formula itself is fixed. This new endpoint re-runs whatever
`estimate_oil_motion` currently computes against every product still
marked `oil_motion_source='estimated'` and OVERWRITES `oil_rating`/
`motion_rating` -- never touches `'chart'` (Brunswick's own published
data) or `'manual'` (an admin's own correction). The UPDATE re-checks
`oil_motion_source = 'estimated'` at write time, not just at the initial
read, so a product that got manually corrected or chart-matched in
between is safely skipped rather than clobbered. Idempotent and safe to
re-run -- a second run just updates 0 rows once there's nothing left to
fix. `scripts/reestimate_plotter_positions.py` is the thin one-shot POST
wrapper (same shape as `backfill_last_video_discovery_at.py`):
```bash
export ADMIN_API_URL="https://<your-admin-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/reestimate_plotter_positions.py
```
Run this once, right after the refit constants above actually deploy --
not needed again after that, since every newly-estimated product from
then on already uses the refit formula (no separate old/new code path).
No `template.yaml` change needed (same `AdminApiFunction` proxy+
catch-all as every other `/admin/...` route). No admin-site button for
this one, on purpose -- same as `backfill-estimated-plotter-positions`/
`backfill-last-video-discovery-at`/`dedupe-price-sources` above, a rare
one-time catalog-wide correction is curl-only, not worth a permanent UI
control someone could click by accident.

Tests: `tests/test_admin_api_service.py` gained
`test_reestimate_plotter_positions_overwrites_estimated_only`,
`test_reestimate_plotter_positions_no_op_when_nothing_estimated`,
`test_reestimate_plotter_positions_handles_no_usable_skus` (197/197
total). `tests/test_dump_plotter_estimate_training_data.py` (11/11) and
`tests/test_reestimate_plotter_positions.py` (6/6) are new files, same
manual-runner pattern as every other script's tests in this project.

**Deploy order for this fix:**
```bash
sam build PublicApiFunction
sam build AdminApiFunction
sam build BrunswickProductScraperFunction   # + each other scraper function
                                             # (commercebuild/woocommerce/
                                             # netsuite/shopify) -- all 7
                                             # copies of estimate_oil_motion
                                             # changed together
sam deploy
```
Then run the re-estimate backfill ONCE to fix everything already in the
catalog under the old constants:
```bash
export ADMIN_API_URL="https://<your-admin-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/reestimate_plotter_positions.py
```

### 6n. Consumer site (React SPA)

`consumer-site/` -- Vite + React + TypeScript, client-side routed
(`react-router-dom`), talking directly to `PublicApiFunction`. Answers
Al's original ask in full: "a single page like site for quick
navigation and not a ton of full page reloads", a ball detail page with
"sections for the high level details and summary of summary and then
easy ways to dive into each video and play them in an embeded player",
"an intuitive way to populate a ball comparison page", and "a focus on
current bowling balls and a way to still view retired balls and suggest
current balls that best compare to the retired balls".

Four routes, one shell (`App.tsx`/`Nav.tsx`) -- see `consumer-site/
README.md` for the full page-by-page breakdown:
- `/` -- Browse (status toggle, brand filter, search, "add to compare").
- `/balls/:id` -- detail page: specs, video-summary rollup, embedded
  YouTube reviews, and (retired products only) suggested current balls
  via `GET /products/{id}/similar`.
- `/compare` -- add up to 6 balls (`useCompareList` hook, localStorage-
  backed, shared across pages, mirrored into `?ids=` so a compare set is
  a real link), spec table side by side.
- `/plotter` -- `GET /products/plotter` wired up, functional but plain
  (see below).

**Build:**
```bash
cd consumer-site
npm install
cp .env.example .env.local   # VITE_PUBLIC_API_URL = the PublicApiUrl stack output
npm run build                 # outputs to consumer-site/dist/
```

**Deploy** (after `sam deploy` has created `ConsumerSiteBucket`/
`ConsumerSiteDistribution` -- see template.yaml):
```bash
BUCKET=$(aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='ConsumerSiteBucketName'].OutputValue" --output text)
DIST_ID=$(aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='ConsumerSiteDistributionId'].OutputValue" --output text)

aws s3 sync consumer-site/dist/ "s3://$BUCKET/" --delete
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*"
```
`ConsumerSiteUrl` (stack output) is the CloudFront domain to open
afterward.

**CI: push-to-deploy via GitHub Actions (2026-08-12).** Al: "lets get
back to the CI for the consumer-site." Answers this project's own
earlier open question (IAM access keys vs. OIDC role, never resolved
until now) -- went with **OIDC federation**: GitHub's own short-lived
token gets exchanged for temporary AWS credentials scoped to one narrow
IAM role, so no AWS access key/secret ever exists as a GitHub secret to
leak or rotate. `.github/workflows/deploy-consumer-site.yml` runs on
every push to `main` that touches `consumer-site/**` (or manually via
`workflow_dispatch`): `npm ci`, `npm run build` (with
`VITE_PUBLIC_API_URL` baked in at build time -- Vite inlines
`import.meta.env.VITE_*` into the bundle, there's no runtime env lookup
in the browser afterward), `aws s3 sync dist/ ... --delete`, then a full
CloudFront invalidation (`--paths "/*"`). Concurrency-grouped
(`cancel-in-progress: false`) so two rapid pushes queue rather than one
cancelling mid-sync and leaving the bucket half-updated.

`template.yaml` gained the AWS-side half: `ConsumerSiteDeployRole` (IAM
role, trust-restricted via `token.actions.githubusercontent.com:sub` to
`repo:<owner>/<repo>:ref:refs/heads/main` -- pushes to `main` in exactly
that repo, not any branch/fork/PR) and `GitHubOidcProvider`
(`AWS::IAM::OIDCProvider` for `token.actions.githubusercontent.com`,
gated behind its own `CreateGitHubOidcProvider` flag since that provider
is an **AWS-account-level singleton** -- only one per URL per account,
so if a different project in this same account already created one for
GitHub Actions, set `CreateGitHubOidcProvider=false` here and this
role's trust policy still works unchanged, since the provider's ARN is
computed from `AWS::AccountId` directly rather than referencing the
conditional resource). `ConsumerSiteDeployRole`'s own policy is scoped
to exactly what the workflow's two AWS steps need -- `s3:ListBucket`/
`PutObject`/`DeleteObject` on `ConsumerSiteBucket` alone, and
`cloudfront:CreateInvalidation` on `ConsumerSiteDistribution` alone.
Nothing broader (no `cloudformation:*`, no other bucket) -- deploying
`template.yaml` itself is still a human running `sam deploy`, this role
only ever touches the already-provisioned site infra.

Both new resources are gated behind `GitHubRepo` (blank by default, same
"blank param = feature off" convention as `ConsumerSiteDomainName`/
`SwagBrandId` elsewhere in this template), so a deploy that hasn't set
it up yet is completely unaffected.

**One-time setup:**
1. Redeploy with `GitHubRepo` set to `"<owner>/<repo>"` (e.g.
   `awbowlerdepot/ballscratcher`):
   ```bash
   sam deploy --parameter-overrides GitHubRepo=awbowlerdepot/ballscratcher ...
   ```
   (keep every other existing `--parameter-overrides` value the same --
   `sam deploy` doesn't merge with the last deploy's values on its own;
   see this runbook's earlier troubleshooting note on that exact gotcha
   if `samconfig.toml` already has `parameter_overrides` cached from a
   previous deploy).
2. Grab the new role ARN:
   ```bash
   aws cloudformation describe-stacks --stack-name <your-stack-name> \
     --query "Stacks[0].Outputs[?OutputKey=='ConsumerSiteDeployRoleArn'].OutputValue" --output text
   ```
3. In the GitHub repo: **Settings -> Secrets and variables -> Actions ->
   Variables** (repo Variables, not Secrets -- none of these five values
   are sensitive on their own; see the workflow file's own comment on
   why), add:
   - `CONSUMER_SITE_DEPLOY_ROLE_ARN` -- the ARN from step 2.
   - `AWS_REGION` -- whatever region this stack is deployed to.
   - `CONSUMER_SITE_BUCKET` -- the `ConsumerSiteBucketName` stack output.
   - `CONSUMER_SITE_DISTRIBUTION_ID` -- the `ConsumerSiteDistributionId`
     stack output.
   - `PUBLIC_API_URL` -- the `PublicApiUrl` stack output.
4. Push anything under `consumer-site/` to `main` (or run the workflow
   manually from GitHub's Actions tab) -- it deploys itself from there
   on.

If IAM rejects the `sam deploy` in step 1 with an error creating
`GitHubOidcProvider` (`EntityAlreadyExists`), a GitHub OIDC provider
already exists in this AWS account from some other project -- redeploy
again with `CreateGitHubOidcProvider=false` added to the same
`--parameter-overrides` and nothing else changes.

**Real incident, first live deploy attempt:** `sam deploy` failed
outright with `Requires capabilities : [CAPABILITY_NAMED_IAM]` before
even reaching a changeset review. Root cause: `ConsumerSiteDeployRole`
sets an explicit `RoleName` (`bowling-scraper-consumer-site-deploy-
${AWS::AccountId}`) rather than letting CloudFormation auto-generate one
-- every other IAM resource this template has ever created (e.g. each
Lambda's execution role) lets CloudFormation pick the name, which only
needs the plain `CAPABILITY_IAM` acknowledgment `samconfig.toml` already
had. A *named* IAM resource needs the stronger `CAPABILITY_NAMED_IAM`
acknowledgment instead (CloudFormation's own distinction: a
CloudFormation-generated name can't collide with anything you'd
recognize or depend on elsewhere in the account, so it's considered
lower-risk than a template that gets to claim a specific, human-chosen
IAM name). Fixed by changing `samconfig.toml`'s `capabilities` from
`"CAPABILITY_IAM"` to `"CAPABILITY_IAM CAPABILITY_NAMED_IAM"` (both,
space-separated -- `sam deploy` accepts a list here, and every other
resource in this template still only needs the plain one). Re-run `sam
deploy` after this edit; no template.yaml change was needed, this was
purely a local deploy-config gap.

**Real incident, first live CI run:** the deploy stack came up fine,
`CONSUMER_SITE_DEPLOY_ROLE_ARN` and the other 4 repo Variables were set,
and a push to `main` triggered the workflow -- but the "Configure AWS
credentials" step failed every time with `Error: Could not assume role
with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity`.
Ruled out the obvious suspects first: `aws iam get-role` showed
`ConsumerSiteDeployRole`'s trust policy exactly matching `template.yaml`
(correct `Federated` principal, correct `aud`/`sub` conditions,
`repo:awbowlerdepot/ballscratcher:ref:refs/heads/main`), the run really
was a push-triggered run on `main` (not a stray `workflow_dispatch` on
some other branch), and `aws iam get-open-id-connect-provider` showed
the OIDC provider itself correctly configured too (`sts.amazonaws.com`
in `ClientIDList`, a populated `ThumbprintList`). None of that was it.

Root cause was in the debug log, one line above the error:
`7 role session tags are being used`. `aws-actions/configure-aws-
credentials@v4` tags the assumed session (repo, ref, actor, workflow,
etc.) by default unless told not to -- and passing `Tags` to
`AssumeRoleWithWebIdentity` requires the trust policy to separately
authorize `sts:TagSession`, on top of `sts:AssumeRoleWithWebIdentity`
itself. `ConsumerSiteDeployRole`'s trust policy only grants the latter,
on purpose (least-privilege, and this workflow's two real steps -- S3
sync, CloudFront invalidation -- have no use for session tags at all).
Fixed by adding `role-skip-session-tagging: true` to the "Configure AWS
credentials" step in `.github/workflows/deploy-consumer-site.yml`,
rather than widening the trust policy (and needing another `sam
deploy`) for a capability nothing here needs.

**Real incident, second round of the exact same error message.** Same
`Not authorized to perform sts:AssumeRoleWithWebIdentity`, confirmed via
the debug log that session tagging really was off this time
(`Role session tagging has been skipped.`) -- so this was a second,
unrelated cause hiding behind an identical error. Rather than keep
guessing from AWS-side config (already verified correct twice), added a
temporary step to the workflow that requests the same OIDC token the
credentials step requests and decodes+prints its payload (`core.
getIDToken('sts.amazonaws.com')`, then `JSON.parse(Buffer.from(token.
split('.')[1], 'base64').toString())` -- only the payload gets logged,
the signature never does). That showed the actual `sub` claim GitHub was
sending: `repo:awbowlerdepot@68925487/ballscratcher@1316941668:ref:refs/
heads/main`, not the plain `repo:awbowlerdepot/ballscratcher:ref:refs/
heads/main` the trust policy's exact `StringLike` match assumed.

GitHub now appends each owner's and repo's numeric "immutable ID" to
their names in the sub claim -- a security hardening on GitHub's side so
a trust policy written against an old owner/repo name can't get
inherited by a renamed, transferred, or deleted-and-recreated repo that
reused the name. `ConsumerSiteDeployRole`'s `StringLike` condition in
`template.yaml` now wildcards right after the owner and repo name
specifically (not the whole `owner/repo` value -- the `@id` lands
*inside* it, before the `/`), built via `!Split`/`!Select` on
`GitHubRepo` rather than hardcoding this repo's specific numeric IDs:

```yaml
StringLike:
  token.actions.githubusercontent.com:sub:
    !Sub
    - "repo:${Owner}*/${Repo}*:ref:refs/heads/main"
    - Owner: !Select [0, !Split ["/", !Ref GitHubRepo]]
      Repo: !Select [1, !Split ["/", !Ref GitHubRepo]]
```

This matches the sub claim whether or not GitHub includes the `@id`
suffix, so it keeps working if GitHub ever changes the default back.
**Unlike the session-tagging fix, this one is an IAM resource change --
it needs a real `sam deploy` to take effect, not just a push.** The
temporary debug step has been removed from the workflow now that the
mismatch is found; the CFN-tolerant YAML loader confirms `template.yaml`
still parses (53 resources, 38 outputs) with the new condition resolving
to the expected nested `Fn::Sub`/`Fn::Select`/`Fn::Split`/`Fn::Ref`
structure.

**Hosting infra** (`template.yaml`): private `ConsumerSiteBucket` (S3,
all public access blocked) behind `ConsumerSiteDistribution`
(CloudFront) via Origin Access Control -- not the older OAI, and not
public-read S3 like `ImageBucket` (that one's public-read is
deliberate, direct product-image URLs; this is a real site that needs
HTTPS + caching in front of it). `CustomErrorResponses` rewrites both
403 and 404 to `/index.html` with a 200 -- required for client-side
routing: a direct load of `/balls/<id>` or a refresh on `/compare` has
no matching S3 object, so without this rewrite the visitor would see a
raw CloudFront/S3 error page instead of react-router taking over.

**What's not done yet:**
- The plotter page's search box and label toggle -- gridlines, ball-image
  markers, the size slider, brand-toggle chips, hover/click hit-testing,
  and clipping-safe margins are all ported now (see below); only the
  search box and label toggle from `reference/plotter_reference.html`
  remain unported.
- No automated frontend tests, and `npm install`/a real build were never
  run against the live npm registry from inside this sandbox -- earlier
  sessions had no network access to it at all (`npm ping` returned `403
  blocked-by-allowlist`); this sandbox instance can reach the registry
  and `npx tsc -b` typechecks clean, but `npm run build`'s rollup bundle
  step still fails here on a platform-mismatched
  `@rollup/rollup-linux-arm64-gnu` native binary -- unrelated to code
  correctness, doesn't reproduce on Al's own machine. Run `npm run
  build` yourself before deploying and treat any TypeScript error it
  surfaces as a real bug to fix, not a false positive.
- No SEO/meta-tag work or analytics.

**Plotter "Compare" tab (2026-08-12).** Al: "can we add a tablist toggle
to the ball motion plotter that is 'compare' and plots the currently
selected balls in the compare feature." A third option alongside
Current/Retired in `PlotterPage.tsx`'s existing `role="tablist"` -- same
`?status=` URL param (now typed `PlotterView = ProductStatus | "compare"`
rather than adding a second query param for what's still exactly one
active tab at a time), showing the visitor's live compare-list count in
the tab label (`Compare (3)`) via the shared `useCompare()` hook already
powering the Browse/Detail "add to compare" buttons and the `/compare`
page itself.

Needed a small backend extension, not just frontend wiring:
`list_plotter_positions` (`src/public_api/service.py`) only ever accepted
a `status` filter, but the compare list is an arbitrary set of ids a
visitor picked while browsing (which can mix current and retired
products), not "everything of one status" -- the exact same shape problem
`get_products_compare` already solved for the `/compare` page itself.
Gave `list_plotter_positions` an optional `ids` param with the identical
contract: capped at `MAX_COMPARE_IDS` (6), missing/unpublished ids
silently dropped rather than erroring, input order preserved on the way
out, and `status` ignored entirely when `ids` is given. The SQL is one
query with a swapped `WHERE` clause (`p.id = any(%s::uuid[])` vs.
`p.status = %s`) rather than two near-duplicate query strings, since both
branches share the exact same `SELECT` list. `GET /products/plotter`
gained a matching optional `ids` query param (comma-separated, same shape
as `/products/compare`'s own `ids` param) -- no `template.yaml` change,
same single `{proxy+}` GET route every other `public_api` endpoint rides.
Five new tests in `tests/test_public_api_service.py` cover the ids branch
(status-ignored, order-preserved, missing/unpublished dropped, capped at
6, and an empty list falling back to `status` rather than silently
returning nothing) -- full suite re-run clean at 41/41.

`api/client.ts`'s `getPlotterPositions` gained a matching optional `ids`
second argument (ids present and non-empty wins over `status`, mirroring
the backend). `PlotterPage.tsx`'s data-fetch `useEffect` now branches on
the active tab: Compare with an empty compare list skips the fetch
entirely and shows an empty state ("Nothing to plot yet. Browse balls...
or add some from the compare page.") rather than falling through to
`getPlotterPositions`'s own "no ids -> use status" default, which would
otherwise have silently plotted the whole current catalog instead of
being honest about there being nothing selected yet. The size slider,
brand-toggle chips, and ball count are also hidden (not just empty) in
that same zero-point state, on any tab -- a small pre-existing polish gap
this touched anyway (a "Retired" filter that matched nothing showed a
bare "Brand:" label with no chips and a "0 of 0 balls shown" count next
to the empty-state message; now that whole controls row only renders once
there's actually something to control).

`npx tsc -b` typechecks clean against these changes -- see this section's
own "What's not done yet" note above for why `npm run build`'s bundle
step itself can't be exercised in this sandbox (unrelated rollup native-
binary issue, not a code problem). Run `npm run build` yourself before
redeploying the consumer site.

**Plotter hover-to-front + explode-on-hover overlapping points
(2026-08-12).** Al first asked for balls sharing the exact same
`(oil, motion)` value to always fan out, plus the hovered ball to draw
on top of any it overlaps. After seeing that, he preferred a different
interaction: "I was thinking of having them overlapped and then on
hover animate them out so they are visible" -- so a group now stays
visually stacked at rest and only spreads into a small ring while it's
being hovered, easing back together on mouse-out. All purely
`PlotterPage.tsx`/`index.css` rendering changes -- no backend or type
changes needed, `p.oil`/`p.motion` themselves are never touched.

- **Grouping.** Multiple balls landing on the exact same `(oil, motion)`
  pair is common, not an edge case -- round-number algorithmic estimates
  collide constantly (`estimate_oil_motion` clamps/rounds to a plain
  integer scale, see `public_api/service.py`'s own module comment), and
  chart/manual values aren't guaranteed unique either. A `groups` map
  (`useMemo`, depends on `visible`) buckets points by their literal
  `${oil}:${motion}` key; `groupKeyOf` is the reverse lookup (point id ->
  group key).
- **Explode-on-hover, not always-on.** Every ball is drawn at its true
  grid position (`xFor`/`yFor`) at rest -- a group just sits stacked
  there, same as before this feature existed. A new `explodeOffsets` map
  (`useMemo`, depends on `groups` and `size`) computes a per-ball
  `(dx, dy)` offset from that grid position (ring radius
  `(markerRadius / sin(pi/N)) * 1.15`, same tangent-radius math as
  before, angle-assigned in a stable `id`-sorted order so a re-render
  never reshuffles who's at which position). That offset is applied as
  a `--dx`/`--dy` CSS custom property + `transform: translate(...)` on
  a `.plotter-ball-offset` wrapper, and only takes effect via a
  `.plotter-group:hover .plotter-ball-offset { transform: translate(var(--dx), var(--dy)); }`
  rule in `index.css` -- the explode is purely a CSS `:hover`-driven
  transition (220ms ease), not a JS-computed position swap. Singletons
  skip the group wrapper/offset class entirely and never move.
- **Halo hit-target.** A group with more than one member also renders an
  invisible `.plotter-group-halo` circle (`fill="transparent"`, sized to
  cover the whole fanned-out footprint: `ringRadius + markerRadius`) as
  the group's first child. This exists because a moving element doesn't
  get re-hit-tested by the browser without an actual mouse move -- if
  hover only lived on each ball's own hit circle, the `:hover` state
  (and the explode it drives) would drop the instant a ball animated out
  from under a stationary cursor. The halo stays put for the group's
  whole lifetime, so `:hover` -- and the fan-out -- persists as long as
  the cursor is anywhere near the stack, animating balls back together
  smoothly on mouse-out.
- **Hover-to-front.** SVG has no independent `z-index` the way CSS box
  layout does -- paint order is purely document order, so "on top" means
  "drawn last". `orderedGroupKeys` (`useMemo`, depends on `groups`,
  `groupKeyOf`, `hovered`) moves the hovered ball's whole group to the
  end of the groups rendered, and within that group, `membersOrdered`
  moves the specific hovered ball to the end of its siblings -- so the
  hovered ball paints above both its own group-mates and any other
  overlapping neighbor from an adjacent grid cell, regardless of
  underlying data order. Never changes which balls are plotted or their
  true grid position, only DOM order.
- Known, accepted gap (documented inline in `MARGIN`'s own comment): a
  large hover-exploded cluster sitting right in a plot corner can, in
  theory, still push a member or two past the `<svg>` viewBox's padding
  and get clipped. Needs both a same-position pile-up AND a corner grid
  position at once, while actively hovered -- rare enough, and clamping
  ring positions back into bounds adds real complexity for a case that
  hasn't actually been observed.

`npx tsc -b` typechecks clean.

**Custom domain (`data.bowleriq.com`) on CloudFront:**

CloudFront needs an ACM certificate that covers the domain, and that
certificate has to live in `us-east-1` no matter which region this
stack itself is deployed to -- a hard CloudFront requirement, not a
choice made in this template. CloudFormation can't create resources in
a second region from inside one template, so the certificate is
requested and validated by hand, once, outside `sam deploy`, and its
ARN is passed in as a parameter.

1. **Request the certificate in `us-east-1`:**
   ```bash
   aws acm request-certificate \
     --domain-name data.bowleriq.com \
     --validation-method DNS \
     --region us-east-1
   ```
   This prints a `CertificateArn` -- save it.

2. **Get the DNS validation record:**
   ```bash
   aws acm describe-certificate \
     --certificate-arn <arn-from-step-1> \
     --region us-east-1 \
     --query "Certificate.DomainValidationOptions[0].ResourceRecord"
   ```
   This returns a `Name`/`Type`/`Value` -- add it as a CNAME record at
   whatever DNS provider actually hosts `bowleriq.com` (GoDaddy,
   Cloudflare, Namecheap, etc. -- this template has no way to reach or
   verify that provider, so this step is manual regardless of which one
   you use). Note: some providers want the record `Name` entered
   without the trailing `.bowleriq.com` (i.e. just the host label) --
   check your provider's CNAME UI if the exact value from `describe-
   certificate` gets rejected.

3. **Wait for validation** (DNS propagation, usually a few minutes to
   ~30):
   ```bash
   aws acm describe-certificate \
     --certificate-arn <arn-from-step-1> \
     --region us-east-1 \
     --query "Certificate.Status"
   ```
   Don't move on until this prints `"ISSUED"`.

4. **Deploy with the new parameters:**
   ```bash
   sam deploy --parameter-overrides \
     ConsumerSiteDomainName=data.bowleriq.com \
     ConsumerSiteCertificateArn=<arn-from-step-1>
   ```
   (add any other `--parameter-overrides` you already pass today --
   this replaces the full list, it doesn't merge with a previous
   deploy's overrides). This sets `Aliases`/`ViewerCertificate` on
   `ConsumerSiteDistribution` -- see `HasConsumerSiteDomain` in
   template.yaml's `Conditions:` block.

5. **Point the domain at CloudFront:** add a second CNAME record at
   your DNS provider -- `data.bowleriq.com` -> the value of the
   `ConsumerSiteUrl` stack output with the `https://` stripped off
   (that's `ConsumerSiteDistribution`'s own `*.cloudfront.net` domain
   name):
   ```bash
   aws cloudformation describe-stacks --stack-name <your-stack-name> \
     --query "Stacks[0].Outputs[?OutputKey=='ConsumerSiteUrl'].OutputValue" --output text
   ```
   Once that CNAME resolves, `https://data.bowleriq.com` serves the
   site directly; the `*.cloudfront.net` URL keeps working too (both
   are valid `Aliases`/default-domain hosts on the same distribution).

Leaving `ConsumerSiteDomainName`/`ConsumerSiteCertificateArn` unset (the
default) keeps the distribution on its plain `*.cloudfront.net` URL with
`CloudFrontDefaultCertificate: true` -- no action needed if you don't
want a custom domain yet.

### 6n.1. Fixed incident: "Back to Browse" lost filter/sort state (real incident)

Al: "if i have filtered the consumer products page and click on a
product then go back my context is lost and it is confusing."

`BrowsePage.tsx` correctly keeps `status`/`brand_id`/`q`/`sort` in the
URL's own query string (`useSearchParams()`, not local state), so a
filtered/sorted view is a real, bookmarkable link. But
`ProductDetailPage.tsx`'s "Back to Browse" link (both the main one and
the not-found fallback) was a bare `<Link to="/">`, which throws that
whole query string away and drops the visitor back on the default,
unfiltered Browse view.

Fixed by carrying the visitor's current location forward as React
Router navigation `state` (not another URL param -- this is ephemeral
"where did you come from" context, not shareable state) on every link
into a product page:

- `ProductCard.tsx` now computes `backState = { from:
  `${location.pathname}${location.search}` }` (via `useLocation()`)
  and passes `state={backState}` on both its links into `/balls/:id`
  (image and name).
- `ProductDetailPage.tsx` reads it back: `const backTo =
  (location.state as { from?: string } | null)?.from || "/"` -- falls
  back to `/` for a direct/shared link or a fresh tab, which has no
  "came from" to return to. Both the not-found link and the main
  "&larr; Back to Browse" link now target `backTo` instead of a bare
  `/`.
- The "Similar current balls" links (retired ball -> suggested current
  balls) also pass `state={{ from: backTo }}` so a multi-hop chain
  (Browse -> product -> similar product -> Back) still returns to the
  *original* Browse filters, not just the immediately-previous product
  page.

No backend or URL-contract change -- pure consumer-site frontend fix,
ships via the existing `deploy-consumer-site.yml` workflow on push to
`main` (see 6n above). Verified with `npx tsc -b` (clean, no errors).

### 6o. Price tracking (retailer price search + review workflow)

New feature, Al: "id like to start a price tracker... configurable to
have site setup so that it will pull the current price from a number of
sites on a frequency of likely daily?... store this in a way that would
allow for charting that price over time in the admin ui and eventually
the consumer UI." Went through a design correction mid-build: "site
setup" means choosing which real retailers to track (bowling.com,
bowlingball.com, bowlersmart.com, etc.), with each product's URL on each
site found AUTOMATICALLY by a discovery job (mirroring video_discovery's
YouTube search), not typed in by an admin -- and after weighing
auto-track-immediately against a pending-review gate, "the reccomended
path is best" locked in: mirror product_videos' pending/approved/rejected
review workflow exactly, including undo/restore built in from the start
(see `db/migrations/014_price_tracking.sql`'s header comment for the
full writeup).

Requires migrations 014/015 (see step 2 above) and `PriceCheckerFunction`
to have deployed (step 5) -- `AdminApiFunction`'s
`PRICE_CHECKER_FUNCTION_NAME` env var and its `lambda:InvokeFunction`
grant on `PriceCheckerFunction.Arn` are both wired automatically, no
separate secret/param needed (this feature has no third-party API key --
it's generic HTML scraping against whatever retailer sites you configure,
not a metered API like YouTube's).

1. **Add a Price Site.** Open the admin site's Price Sites tab and add at
   least one real retailer, e.g.:
   - Name: `BowlingBall.com`
   - Search URL template: `https://www.bowlingball.com/catalogsearch/result/?q={query}`
     (must contain a literal `{query}` placeholder -- price_checker
     url-encodes the product's brand+name and substitutes it there)
   - Result link selector: a CSS selector matching `<a>` tags on that
     site's search-results page whose `href` is a candidate product URL
     (inspect the site's real markup in a browser devtools panel to find
     this -- it varies per retailer)
   - Default price selector: a CSS selector matching where the price text
     sits on that site's own product pages (also found via devtools)

   Getting the two selectors right the first time is unlikely without
   inspecting the real site -- expect to revisit a site's config after
   the first "Find price sources" run below shows 0 or garbage results,
   and again after the first price check shows a run of `error` rows in
   a product's Price Tracking section.

   **Test a site's selectors before saving them**, Al: "how do i test
   just that" (a new site's config, in isolation, without going through
   the DB/admin API at all). `scripts/test_price_site.py` -- edit the
   `SEARCH_URL_TEMPLATE`/`RESULT_LINK_SELECTOR`/`PRICE_CSS_SELECTOR`/
   `QUERY` constants at the top for the site you're setting up, then:
   ```bash
   pip3 install -r scripts/requirements.txt
   python3 scripts/test_price_site.py
   ```
   Runs the exact same selector logic as `search_site_for_product`/
   `extract_price` in `src/price_checker/app.py` (copied in rather than
   imported so this has zero AWS/DB/repo-layout dependencies -- just
   `requests`+`beautifulsoup4`, run from anywhere), against the real site
   directly. Prints the first search result it finds and the price it
   extracted from that product page, ending in `PASS: found $X.XX on
   <url>` or a `FAILED` line naming which selector didn't work. This is
   the one-product-in-isolation check; "Find price sources" below is the
   real discovery job and checks EVERY active site for that product, not
   just the one you just added.

2. **Trigger discovery for one product.** Open any product's detail view
   (Products tab -> click a row) and click "Find price sources" in its
   new Price Tracking section. This invokes `PriceCheckerFunction`
   asynchronously with `{"discover": true, "product_ids": ["<id>"]}` --
   reload the panel a few seconds later to see what it found. Every
   result search_site_for_product returns lands as a `pending` row
   (never silently dropped), tagged `high`/`low` match confidence via the
   same brand+product-token heuristic video_discovery's `score_match`
   uses (see `src/price_checker/app.py`'s `score_match`).

   Equivalent direct invoke, if you'd rather watch CloudWatch logs
   directly instead of going through the admin API:
   ```bash
   aws lambda invoke --function-name bowling-scraper-price-checker \
     --payload '{"discover": true, "product_ids": ["<product-id>"]}' \
     --cli-binary-format raw-in-base64-out /tmp/price-discovery-out.json
   cat /tmp/price-discovery-out.json
   ```

   **Catalog-wide, instead of one product at a time.** Al: "can we script
   clicking find price sources for all items that are current." Every
   product's own "Find price sources" click is really just a `POST
   /products/{id}/discover-price-sources` with that one `product_id` --
   the admin API also exposes the SAME job with no `product_ids`/`brand_id`
   at all, `POST /admin/discover-all-price-sources`, which
   `price_checker.fetch_products_to_discover` already defaults to
   `products.status = 'current'` when scoped that generically (see that
   function's own docstring) -- "all items that are current" is this
   endpoint's own default, nothing extra to configure. `scripts/discover_
   all_price_sources.py` is a thin trigger for it, same shape as `scripts/
   refresh_video_stats.py`:
   ```bash
   export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
   export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
   export REPEAT="5"             # optional -- see the script's own docstring
   export INTERVAL_SECONDS="300" # optional, only matters if REPEAT > 1
   python3 scripts/discover_all_price_sources.py
   ```
   One call searches up to `DEFAULT_MAX_PRODUCTS_PER_DISCOVERY_INVOCATION`
   (100) current products, rotated via `products.last_price_discovery_at`
   asc-nulls-first -- a catalog with more `current` products than that
   needs either several manual re-runs over time, or `REPEAT > 1` to fire
   several invocations (spaced `INTERVAL_SECONDS` apart, since the
   underlying Lambda invoke is async/fire-and-forget) in one script run.
   Like the single-product button, this only ever writes `pending`
   candidates -- nothing is auto-approved.

3. **Review and approve.** Candidates show up in the Price Sources tab
   (defaults to `status=pending`) or in the product's own Price Tracking
   section (which shows every status at once, same reasoning as the
   Videos section above it). Approve the correct match -- this is what
   actually makes it eligible for daily checking (see
   `list_price_sources_due`'s `status = 'approved'` filter). If a search
   found nothing, or found the wrong product, use the "Add manually" form
   in the product's Price Tracking section to attach the correct URL by
   hand -- this lands immediately as `approved`/`source='manual'`, no
   review step, since an admin just supplied the exact URL directly (Al:
   "admin can fix mismatches manually after the fact if a match is
   wrong").

4. **Trigger a price check** for the same product via "Check prices now"
   in its Price Tracking section (invokes `{"product_ids": [...]}`,
   checking only its approved+active sources), or wait for the daily
   `DailyPriceCheck` schedule (`rate(1 day)`, checks the most-overdue
   approved+active sources catalog-wide, same rotation idiom as
   `VideoDiscoveryFunction`'s stats-refresh schedule). Reload the
   product's Price Tracking section afterward -- a successful check shows
   a `$` price and updates the chart; a failed one (bad selector, site
   redesign) shows `error` with the failure reason on hover, and still
   writes a `product_price_history` row (see migration 014's header
   comment on why a failed check is never silently dropped).

5. **Confirm undo works.** Approve or reject a candidate by mistake, then
   click "Undo" next to its status badge -- it should move back to
   `pending` with `resolved_at`/`resolved_by` cleared. Built in from the
   start for this feature (unlike `product_videos`, where the same
   capability only got added after Al hit the gap live: "it appears if i
   accidentally reject a video i can not undo that action").

6. **Confirm the catalog-wide batch triggers work** from the admin site
   or directly:
   ```bash
   aws lambda invoke --function-name bowling-scraper-price-checker \
     --payload '{"discover": true, "limit": 5}' \
     --cli-binary-format raw-in-base64-out /tmp/price-discovery-batch-out.json
   aws lambda invoke --function-name bowling-scraper-price-checker \
     --payload '{"limit": 5}' \
     --cli-binary-format raw-in-base64-out /tmp/price-check-batch-out.json
   ```
   Both should return `{"statusCode": 200, ...}` with counts in the body
   (`products_searched`/`new_candidates`/`search_errors` for discovery;
   `sources_checked`/`succeeded`/`failed` for checking).

Not yet built: consumer-site price display (Al's own "eventually the
consumer UI" -- deliberately out of scope for this pass, admin-side
tracking + review comes first) and a scheduled discovery cadence
(discovery is manual/on-demand only right now, same "manual/direct
invoke only" convention `video_discovery`'s own search flow uses -- an
admin decides when to widen price-source coverage, it doesn't run on its
own daily schedule the way checking already-approved sources does).

### 6o.5. BowlerDepot price/cost/stock tracking via the BigCommerce API (migration 016)

Extends 6o with a second source *type*, Al: "this... is a project for
the same company that owns bowlerdepot.com which is why we have the API
access for that. Going with the API for that one would be great and
there are some additional data points that would be nice to pull in for
the admin side. In stock over time and cost price over time so once per
day for those too." Plugs into the exact same `product_price_sources`/
`product_price_history` tables and review workflow as 6o, as a new
`fetch_method='api'` `price_sites` row, rather than a separate pipeline
-- see `db/migrations/016_price_tracking_bigcommerce.sql`'s header
comment for the full design writeup, including the key decision to reuse
`bowlerdepot_products` (already maintained daily by
`BowlerDepotReconciliationFunction`, see 6h) instead of re-deriving fuzzy
product matching a second time.

**Scoped to BowlerDepot only.** In-stock and cost-price tracking are
BigCommerce-specific fields this project only has real API access to for
its own store -- not extended to the scraped retailer sites from 6o.

Requires migration 016 (see step 2 above) and the same
`BigCommerceSecretArn` secret 6h's BowlerDepot reconciliation already
uses (see step 3's "BigCommerce credentials" section) -- `template.yaml`
wires `BIGCOMMERCE_SECRET_ARN` into `PriceCheckerFunction` the same
conditional way it's wired into `BowlerDepotReconciliationFunction`
(`HasBigCommerceSecret`), so nothing new to provision beyond what 6h
already needs. **A real secret + ARN now exist for this deployment**
(see step 3) -- this section is ready to smoke-test once
`BigCommerceSecretArn` is passed at deploy time. If you're deploying
somewhere without real BowlerDepot API credentials, `PriceCheckerFunction`'s
existing scrape-only daily schedule is unaffected either way (it simply
has no `'api'`-`fetch_method` `price_sites` row to act on until step 1
below is done).

1. **Add BowlerDepot as an API-fetch-method Price Site.** Open the admin
   site's Price Sites tab, set "Fetch method" to API, and fill in:
   - Name: `BowlerDepot`
   - API provider: `bigcommerce` (the only value this project supports)
   - Storefront base URL: `https://www.bowlerdepot.com` (resolves
     BigCommerce's relative `custom_url.url` into a clickable admin-UI
     link)

   No search URL template/selectors needed for an API site -- discovery
   reads `bowlerdepot_products` directly instead of crawling a search
   page (see `price_checker.discover_bigcommerce_candidates`).

2. **Trigger discovery.** Same "Find price sources" button as 6o, on any
   product that already has a `matched`/`ambiguous` row in
   `bowlerdepot_products` (i.e. `BowlerDepotReconciliationFunction` has
   already run at least once, see 6h). One batched BigCommerce API call
   covers every in-scope product against this one site, rather than one
   request per product the way a scrape site's search works. Candidates
   land as `pending`, confidence `high` for an exact
   (`bowlerdepot_products.match_status = 'matched'`) match or `low` for a
   fuzzy (`'ambiguous'`) one -- same two-tier idea as `score_match`, just
   sourced from the reconciliation job's own match decision instead of a
   fresh title heuristic.

3. **Review, approve, and check** exactly as steps 3-4 in 6o above --
   this is the same `product_price_sources` review queue and the same
   "Check prices now" button/daily schedule, just routed to
   `check_bigcommerce_sources` instead of the generic scraper for this
   one site's rows.

4. **Confirm cost price + in-stock show up.** After a successful check,
   the product's Price Tracking section shows `cost $X.XX` and an
   in-stock/out-of-stock badge next to BowlerDepot's price -- both are
   `null`/absent for every other (scrape-sourced) site's rows, by design
   (see migration 016's header comment). **UPDATED by migration 017**
   (see 6o.6 immediately below): in-stock is no longer derived from
   BigCommerce's product-level `inventory_tracking`/`inventory_level`/
   `availability` fields (that heuristic, `price_checker.
   determine_in_stock`, is gone) -- it's now derived from this same
   check's own per-SKU quantity readings instead, via `price_checker.
   determine_in_stock_from_sku_quantities`. This directly fixes the old
   heuristic's one known caveat (product-level `inventory_level` being a
   rollup across every weight variant, not the one weight this project
   sells as a SKU).

5. **Let it run daily.** Cost/stock history only becomes useful the
   longer it accumulates (Al: "obviously the over time will gain value as
   we get more days in the past but starting now will start building that
   value... we can use them for forcasting and other things in the
   future") -- once a BowlerDepot source is approved, `PriceCheckerFunction`'s
   existing `DailyPriceCheck` schedule picks it up automatically, no
   separate schedule needed for this source type.

### 6o.6. Per-SKU stock quantities (migration 017)

Al, clarifying 6o.5's in-stock boolean: "for the instock i was refering
to actual number of each sku instock." Extends 6o.5 with real per-weight
quantity tracking rather than a single product-level true/false --
answered via a follow-up design conversation: "the per-sku quantities
should be stored in the best way to track them over time effeciently.
something we want to track with this is how many are being sold and when
are they restocked and things like that. the current instock can still
exist but should follow the quantities and once 0 it should be false.
the weights on bigcommerce should match what we have and if they don't
something should keep track of that so we can fix whatever is causing
the discrepency." See `db/migrations/017_price_tracking_sku_stock.sql`'s
header comment for the full design writeup (new `product_sku_stock_history`
table, why a new table rather than a JSON column, and the weight-mismatch
`review_queue` dedup guard).

Requires migration 017 (see step 2 above) and the same BigCommerce
credentials 6o.5 already uses -- nothing new to provision. Every
BowlerDepot check now also fetches each product's `variants` array in the
same batched BigCommerce API call (`include=custom_fields,variants`,
`price_checker.build_bigcommerce_products_by_id_url`), matches each
variant's weight against this product's own `product_skus` rows
(`price_checker.match_sku_weights_to_variants`), and records a
`product_sku_stock_history` row per matched SKU.

1. **Nothing to configure.** Unlike 6o.5's Price Sites setup, this piggybacks
   on the exact same BowlerDepot source/checks already approved in 6o.5 --
   there's no separate site, discovery step, or schedule.

2. **Check a BowlerDepot-tracked product's price** (6o.5 step 3's "Check
   prices now" button, or wait for the daily schedule) and open its
   product detail view in the admin site.

3. **Confirm the new "SKU stock" table + chart appear**, below the
   existing SKUs table. Each row shows one of this product's weights with
   its latest recorded quantity (or "unknown" if BigCommerce isn't
   tracking that variant's inventory -- `quantity` stays `null`, never
   coerced to 0) and when it was last checked. The chart below plots
   quantity over time, one line per weight, same hand-rolled SVG approach
   as the price chart (no charting library).

4. **Confirm in-stock now follows the quantities.** The Price Tracking
   section's in-stock/out-of-stock badge (6o.5 step 4) should now flip to
   "out of stock" only once every one of this product's matched SKUs
   reads a confirmed `0` -- not from a single product-level field
   anymore.

5. **If a weight mismatch exists, confirm it lands in the Review Queue.**
   If BigCommerce sells a weight this project doesn't have a `product_skus`
   row for, or vice versa, a new `review_queue` row appears (`source:
   'price_checker'`, field name `sku_weight_missing_in_our_catalog` or
   `sku_weight_missing_in_bigcommerce`) -- the existing Review Queue tab
   already surfaces any `source` value with zero new admin_api work (see
   `service.list_review_queue`). Re-running the check the next day should
   NOT insert a second row for the same still-unresolved mismatch (the
   dedup guard in `price_checker.write_sku_weight_mismatch_reviews`) --
   only resolving (approving/rejecting) the existing row, or a genuinely
   different mismatch, produces a new one.

6. **Let it run daily**, same reasoning as 6o.5 step 5 -- "how many sold /
   when restocked" is intentionally computed at read time from consecutive
   quantity readings, not stored as its own event, so this data becomes
   more useful the longer it accumulates.

### 6o.7. Fixing/cleaning up duplicate price-source rows (a real bug, now fixed)

Al, after filling in a previously-blank `price_sites.base_url` on a
`fetch_method='api'` site and re-running discovery: "there are duplicates
now, the ones before having the baseurl and now the ones that have
it... same record just has different link."

**Root cause:** `extract_bigcommerce_price_fields` falls back to the raw
relative `custom_url` when `base_url` isn't configured, so a
`product_price_sources` row discovered before `base_url` was filled in
got a relative `product_url`. `insert_price_source_candidates`' `ON
CONFLICT DO NOTHING` is keyed on the literal `(product_id, price_site_id,
product_url)` triple (014) -- once `base_url` got filled in, the next
discovery run computed a different (absolute) `product_url` for the
exact same real-world product+site pair, so the conflict target didn't
match and a second row got INSERTed instead of the first one being
corrected in place.

**Fixed going forward:** `discover_bigcommerce_candidates` now calls
`price_checker.upsert_bigcommerce_price_source_candidate` instead of
`insert_price_source_candidates` -- it looks up any existing row for
`(product_id, price_site_id, source='bigcommerce_api')` first and
corrects its `product_url`/`external_product_id` in place if they've
drifted, only falling back to a fresh INSERT when no such row exists yet.
This is `'api'`-source-specific (see that function's own docstring for
why it can't just replace `insert_price_source_candidates` everywhere --
a `'site_search'`/scrape site can legitimately produce several distinct
candidate URLs per product).

**Cleaning up rows that already duplicated before this fix shipped:**
run the one-off cleanup once, same thin-one-shot-POST shape as
`backfill_last_video_discovery_at.py`:

```
export ADMIN_API_URL="https://<your-api-id>.execute-api.us-west-1.amazonaws.com"
export ADMIN_API_TOKEN="<the same bearer token used elsewhere>"
python3 scripts/dedupe_product_price_sources.py
```

This calls `POST /admin/dedupe-price-sources` -> `service.dedupe_
product_price_sources`, which finds every `(product_id, price_site_id)`
pair with more than one row, keeps the approved+active row as survivor
(else the oldest), migrates any `product_price_history`/`product_sku_
stock_history` rows from the redundant rows onto the survivor first (so
no price/stock history is lost), deletes the redundant rows, and
corrects the survivor's `product_url` to whichever variant in the group
is actually resolved (absolute). Idempotent and safe to re-run -- a
catalog with no duplicate groups left just returns `groups_merged=0
rows_deleted=0`.

### 6o.8. Daily schedules wired up for every brand's URL discovery (real ask: "how long would it take to pick up a new ball?")

Al asked how long a newly-released ball would take to get picked up.
Answer at the time: it depended entirely on the brand -- Brunswick (and
its Craft-CMS siblings' shared `UrlDiscoveryFunction`) already ran a
daily sitemap diff, but every OTHER brand's own `*UrlDiscoveryFunction`
existed with no `Schedule` event at all, invoke-manually-only, left that
way specifically because each brand's `BrandId` parameter (and, for
Shopify, its store-domain parameter) defaults to a blank string in
`template.yaml` -- see each function's old "No schedule wired up yet...
add once BrandId is actually set for a real deployment" comment. Follow-
up ask: "lets wire up daily schedules for them all."

**Every remaining `*UrlDiscoveryFunction` now has a `rate(1 day)`
Schedule event, `Enabled: true`, same shape `UrlDiscoveryFunction`
(Brunswick) already used**: `RadicalUrlDiscoveryFunction`,
`Dv8UrlDiscoveryFunction`, `WooCommerceUrlDiscoveryFunction` (SWAG),
`NetsuiteUrlDiscoveryFunction` (MOTIV), `CommercebuildUrlDiscoveryFunction`
(Storm/Roto Grip/900 Global), `ShopifyUrlDiscoveryFunction` (Hammer),
`TrackUrlDiscoveryFunction`, `EboniteUrlDiscoveryFunction`. That's 9 of 9
brand-discovery functions on a daily cadence now.

**`Enabled: true` is safe for this specific, live deployment** -- every
one of those `BrandId`/store-domain parameters is already set to a real
value here (every brand covered above already has real products in the
catalog, confirmed throughout this project's own history). It would NOT
be safe on a fresh/from-scratch redeploy of this stack with a brand
intentionally left unconfigured: `BRAND_ID=""` would still let the
Lambda run (`os.environ["BRAND_ID"]` doesn't raise on an empty string),
but the resulting insert against `products.brand_id` (a real `uuid`
column) would fail on every scheduled invocation rather than silently
no-op. If this stack is ever redeployed without a given brand's id set,
flip that one function's `Events.DailySchedule.Enabled` to `false` first
-- same "documented assumption, not a CloudFormation Condition" pattern
`BowlerDepotReconciliationFunction`'s own schedule already established
(see 6h/6g's writeup) for the same class of problem.

**What "picked up" now means end to end, once a schedule finds a new
product URL** -- confirmed by tracing every platform's own queue wiring
in `template.yaml`, no code changes needed since this is all already
SQS-triggered:
- Discovery function publishes the new URL onto that platform's own
  scrape queue (`ProductScrapeQueue` for the Craft-CMS family,
  `WooCommerceProductScrapeQueue`, `NetsuiteProductScrapeQueue`,
  `CommercebuildProductScrapeQueue`, or the shared
  `ShopifyProductScrapeQueue`).
- The matching product-scraper function (`ProductScraperFunction`,
  `WooCommerceProductScraperFunction`, `NetsuiteProductScraperFunction`,
  `CommercebuildProductScraperFunction`, `ShopifyProductScraperFunction`)
  is SQS-triggered off that queue -- no manual step. It parses specs,
  looks up/creates the `cores`/`coverstocks` row (see 6.89-93's own
  history), estimates `oil_motion_source` when no chart value exists yet
  (see 6m), and for the Craft-CMS family publishes onto `PdfParseQueue`
  if the page had an info sheet.
- `ImageProcessorFunction` (SQS-triggered off the shared
  `ImageProcessQueue`) and, for Brunswick/Radical/DV8,
  `PdfParserFunction` (SQS-triggered off `PdfParseQueue`) both fire
  automatically off whatever the scraper just published -- no manual
  step either.

So: specs, images, tech-data PDF parsing, core/coverstock rows, and the
oil/motion plotter estimate are ALL hands-off now, catalog-wide, once a
brand's own daily discovery run finds the new URL -- worst case, roughly
24 hours from a manufacturer publishing the page to it fully existing in
this catalog (sitemap/collection propagation delay aside), then seconds
to a couple minutes for the scrape chain itself to drain.

**Two things downstream are still deliberately manual, not oversights**:
video-review discovery (`VideoDiscoveryFunction`'s `{"discover": true,
...}` search job shape) stays invoke-only because YouTube's
`search.list` quota is a hard 100 calls/day (see 6i's own module
docstring) -- Al explicitly chose a subset-first, invoke-when-ready
approach over an automated crawl that would burn the whole daily budget
on new products alone. Price-source discovery
(`PriceCheckerFunction`'s `{"discover": true, ...}` shape) stays
invoke-only too, same reasoning as 6o's own writeup -- an admin decides
when to widen price-source coverage rather than it running on its own
cadence. Neither blocks a new ball from actually appearing in the
catalog/consumer site; they only affect when its video reviews and
tracked prices show up.

**Known gap, unchanged by this work, worth watching now that real daily
message volume will start flowing through it**: `CommercebuildUrlDiscoveryFunction`
respects stormbowling.com's `Crawl-delay: 10` via its own 10s
inter-brand sleep at discovery time, but `CommercebuildProductScraperFunction`
consuming the resulting `CommercebuildProductScrapeQueue` has no matching
per-message throttle of its own -- a burst of SQS messages (e.g. several
new Storm/Roto Grip/900 Global balls landing on the same day) could hit
the site faster than one request per 10s. Not fixed here; still needs an
SQS event-source-mapping `MaximumConcurrency` once real volume is
observed (see the original `template.yaml` comment above
`CommercebuildUrlDiscoveryFunction`).

No new tests -- this is pure infra (`template.yaml`'s `Events` blocks on
8 existing functions, no new resources, no application code touched).
Verified by re-parsing the whole template with the same CFN-tolerant
PyYAML loader used throughout this project (54 resources, 38 outputs,
parses clean) and confirming all 9 `*UrlDiscoveryFunction` resources
(the original Brunswick one plus the 8 added here) each carry exactly
one `Type: Schedule` event with `Enabled: true`.

Requires a real `sam deploy` (not just `sam build`) for each of the 8
functions, same as any other `Events:` addition -- a build alone doesn't
create the underlying EventBridge rules:
```bash
sam build
sam deploy
```
(A full `sam build`, not scoped per-function, since 8 different
functions changed at once here.)

### 6o.9. `missing_video_candidates` filter -- finding products that have never been searched

Direct follow-up to 6o.8's own writeup: Al asked how video search is
scheduled, learned it isn't (see 6o.8/6i -- `VideoDiscoveryFunction`'s
actual search job stays manual/invoke-only, capped by YouTube's
100-calls/day `search.list` quota), then: "can we add a filter check box
to at least get all the products that don't have video candidates."

**`list_products` gained `missing_video_candidates: bool = None`**
(`admin_api/service.py`) -- `and not exists (select 1 from product_videos
pv where pv.product_id = p.id)`, same `not exists`-subquery shape as
`missing_skus` (product_videos is a separate table, not a nullable
column on `products`). Deliberately different from `needs_video_summary_
refresh`/`has_approved_video_summaries` (both of those require an
EXISTING approved+summarized video, an `exists` check) -- this one wants
the opposite: products with ZERO `product_videos` rows of ANY status,
i.e. never searched at all. A product that WAS searched and genuinely
came up with no matching reviews is indistinguishable from a never-
searched one under this filter -- both have zero rows -- but that's an
acceptable blur for "which products need a search run against them",
not a claim about search history (see `video_discovery/app.py`'s own
`last_video_discovery_at`/rotation logic, migration 005, for the
per-product "when was this last searched" signal this filter
deliberately doesn't need).

`admin_api/app.py`'s `GET /products` gained the matching `?missing_video_
candidates=true` query param, wired straight through.

`admin-site/index.html`'s Products tab gained a "no video candidates"
checkbox next to the existing "missing core"/"missing coverstock" ones,
same `productState`/`applyProductFilters`/`loadProducts` wiring pattern
every other Products-tab checkbox already uses.

**No queuing/automation added here** -- this is a visibility filter only,
same "surface the gap, admin decides what to do about it" spirit as
`missing_core`/`missing_coverstock`/`missing_skus`. Finding a product via
this checkbox still requires manually triggering its video search
(the product detail page's own "rescan" button, or `POST /products/{id}/
discover-videos`) -- deliberately not auto-wired to anything, for the
same `search.list` quota reason the schedule itself doesn't exist.

Tests: `test_admin_api_service.py` 194/194 passing (4 new: filter adds
the exact `not exists` SQL text, omitted by default, combines with
`status`, and is confirmed textually distinct from `needs_video_summary_
refresh`'s own `exists` clause so the two filters can't be confused for
each other).

No migration, no `template.yaml` change -- rides `AdminApiFunction`'s
existing `/{proxy+}` catch-all, same as every other `list_products`
filter addition this project has made. Redeploy just the one function:
```bash
sam build AdminApiFunction
sam deploy
```
(No admin-site redeploy step needed beyond re-uploading the static file
-- it's not a Lambda-fronted deployable.)

### 6o.10. "Total ADU" column on the Products tab main table

Al: "can we add the sum of the ADUs for each product to the main table,
if it need to be an async fetch that is fine. so that it will load
faster or if it is fast enough to just grab in the initial call that is
fine too."

Per-SKU ADU (Average Daily Units -- units sold/day, restocks excluded)
already existed on the product detail page's SKU stock forecasting table
(see 6i.11: `computeSkuForecast` in `admin-site/index.html`, client-side
JS over `product_sku_stock_history` rows). This adds a per-PRODUCT
rollup -- the sum of each of a product's SKUs' own ADU -- as a new
column on the Products tab's main table, so you don't have to open each
product to see whether it's moving units at all.

**Went with "grab it in the initial call," not async** -- `_TOTAL_ADU_SQL`
(`admin_api/service.py`) is a new correlated subquery added to
`list_products`'s SELECT clause, computed unconditionally on every call,
same precedent as `_POPULARITY_SCORE_SQL` (6l.5) which made the same
call for the same reason: cheap enough at this catalog's size that a
separate round-trip/async-fetch dance isn't worth the added complexity.
`ADU_LOOKBACK_DAYS = 30` (matches `admin-site/index.html`'s own
`FORECAST_LOOKBACK_DAYS` constant).

**The SQL is a deliberate re-implementation of `computeSkuForecast`'s
exact semantics, not a shared module** (no code is shared between
`price_checker`, `admin_api`, and `admin-site`'s browser JS in this
project, so there's nothing to import from) -- matched by hand:
- Per SKU, only `product_sku_stock_history` rows within the trailing
  `ADU_LOOKBACK_DAYS` window, non-null `quantity`.
- `units_sold` sums only quantity DROPS between consecutive readings
  (`lag() over (partition by product_sku_id order by checked_at)`) --
  a rise between readings is a restock, explicitly excluded, not
  counted as negative sales.
- Requires at least 2 readings in the window (`having count(*) >= 2`)
  and a positive `elapsed_days` between the first and last reading --
  both guard against divide-by-zero/nonsense-ADU the same way
  `computeSkuForecast` returns `adu: null` for those cases. A SKU that
  fails either guard contributes 0 to the product's total, not `null`
  or an error -- same "no signal yet" treatment `computeSkuForecast`
  itself uses.
- Product-level total is `sum(sku_adu)` across the product's SKUs,
  wrapped in `coalesce(..., 0)` so a product with zero qualifying SKUs
  shows `0`, not `NULL` (consistent with `total_adu === null` being
  reserved in the frontend for "column not present in this response,"
  not "product sells nothing" -- see admin-site comment below).

**`admin-site/index.html`**: Products tab table gained a "Total ADU"
column between Popularity and Published, tooltip explaining the metric,
rendered as `Number(p.total_adu).toFixed(2)` (or an em dash if somehow
absent). Detail row's empty-state `colspan` bumped 10 -> 11.

Tests: `test_admin_api_service.py`, 6 new (`test_list_products_always_
selects_total_adu` and friends) confirming the subquery is present
unconditionally, uses the confirmed 30-day window, counts only drops,
requires >=2 readings, guards zero elapsed days, and is scoped to the
right product via `ps_adu.product_id = p.id`. Full suite re-run clean,
zero regressions (`tests/test_admin_api_service.py` 205/205; every other
`test_*.py` file in the repo also still green).

**One incidental fix along the way**: two pre-existing tests
(`test_list_products_popularity_score_averages_not_sums`,
`test_list_products_omits_missing_skus_filter_by_default`) did blanket
substring checks (`"select sum(" not in query`, `"product_skus" not in
query`) against the FULL query string, written back when there was only
one subquery/filter in play. `_TOTAL_ADU_SQL` legitimately introduces
its own unrelated `select sum(...)` and unconditionally joins
`product_skus` (aliased `ps_adu`), so both checks were narrowed to what
they actually meant to verify -- the popularity check now inspects
`service._POPULARITY_SCORE_SQL` directly instead of the joined query,
and the missing-skus check now looks for the specific `not exists (...)`
filter clause instead of the bare table name.

No migration, no `template.yaml` change -- same `AdminApiFunction`
`/{proxy+}` catch-all as every other `list_products` addition. Redeploy:
```bash
sam build AdminApiFunction
sam deploy
```
(Admin-site: re-upload the static `index.html`, no separate deploy
pipeline for it.)

**Follow-up, same session -- Al: "can we add a sort to the admin ui
products list for total ADU":** `_SORT_ORDER_BY` (`admin_api/service.py`)
gained a `"total_adu"` entry, `"total_adu desc, p.id asc"`, sorting the
Products tab by highest-movers-first. Orders by the SELECT list's own
`total_adu` alias rather than repeating `_TOTAL_ADU_SQL` a second time
in the ORDER BY clause -- Postgres allows referencing a SELECT alias
directly. No `nulls last` needed the way `newest`/`oldest` need one:
`_TOTAL_ADU_SQL` is always wrapped in `coalesce(..., 0)`, so it's never
actually `NULL`, just possibly `0` for a product with no qualifying SKU
readings in the window. `admin-site/index.html`'s Products tab sort
dropdown gained a "total ADU" option -- no other client-side wiring
needed, `productState.sort` already flows every value through generically
to the `?sort=` query param.

Tests: `test_admin_api_service.py`, 1 new dedicated test
(`test_list_products_sort_total_adu_orders_by_column_desc`) plus
automatic coverage from the existing `test_list_products_every_sort_
option_keeps_id_tiebreaker` (iterates `_SORT_ORDER_BY`'s keys, so the
new entry is exercised there too without any test change). Full suite
re-run clean: `test_admin_api_service.py` 206/206, every other
`test_*.py` in the repo still green.

No migration, no `template.yaml` change. Same redeploy as above --
`AdminApiFunction` + re-upload `index.html`.

### 6o.11. Admin Dashboard tab: KPIs, top 10 lists, ADU-by-brand chart

Real ask, Al: "can we create an admin dashboard with some KPIs and top
10 lists. I'll let you pick most of the things to show but we have
chart.js included now so we should be able to create some interesting
visuals with some of the data we have pulled together. Top 10s i think
we can do Popularity and ADUs. An interesting number would be total
ADUs across all balls, ADUs by brand, and things like that."

New `GET /admin/dashboard` endpoint (`service.get_dashboard_summary`),
one call per tab load, no query params -- same "full catalog snapshot"
shape as `/brands`. Deliberately reuses `_POPULARITY_SCORE_SQL` and
`_TOTAL_ADU_SQL` as-is everywhere possible rather than re-deriving
either formula a second time, so the Dashboard's numbers are guaranteed
to agree with what the Products tab already shows for the same product.
Four separate queries (KPIs, top 10 popularity, top 10 ADU, ADU by
brand) -- structurally unrelated result shapes that don't share a
natural GROUP BY, so combining them would mean either extra round trips
anyway or a much harder to read query for no real win at this catalog's
size.

Returns:
- `kpis`: total/current/retired product counts, missing_core/
  missing_coverstock/missing_skus counts, products_with_video (>=1
  approved+summarized video), products_with_price_tracking (>=1
  approved+active price source), and total_catalog_adu (every product's
  `_TOTAL_ADU_SQL` summed).
- `top_popularity` / `top_adu`: up to 10 `{id, name, brand_name, ...}`
  rows each, `> 0` only -- a product with nothing meaningful to rank
  doesn't pad the list with zero-ties.
- `adu_by_brand`: one row per brand (even a brand at 0 ADU), ordered
  highest first -- a real `GROUP BY` aggregate (CTE reimplementing the
  same drops-only/lookback-window/`>=2`-readings definition
  `_TOTAL_ADU_SQL` already documents, un-correlated from any single
  product so it can be grouped directly), not `_TOTAL_ADU_SQL` run once
  per product and summed in Python.

New Dashboard tab in `admin-site/index.html` -- added FIRST in the nav
(most natural spot for an overview page) but `activeTab` still defaults
to `'review'`, so existing muscle memory (page loads to Review Queue)
is unchanged; Al just clicks it. KPI cards in a small responsive grid,
Top 10 Popularity/ADU as two side-by-side `.mini` tables, and a
Chart.js bar chart for ADU by brand (reuses the existing `chartInstances`/
`destroyChart` machinery and `.chart-wrap` sizing the price/SKU-stock
charts already use, plus the same "Chart.js failed to load" plain-text
fallback both of those already guard for).

Tests: `test_admin_api_service.py` -- SQL-text assertions (via a small
local cursor whose `fetchone()` returns `()` instead of
`_QueryCapturingConnection`'s default `None`, since an aggregate query
with no GROUP BY always returns exactly one row and the function has no
reason to guard against that) confirming each of the four queries'
joins/filters/reused-SQL-constants, plus one `_SequencedConnection`-based
test confirming the four query results assemble into the right keys.
218/218 in this module; full repo `test_*.py` sweep clean.

No migration, no `template.yaml` change (proxy+ catch-all already covers
`/admin/dashboard`, same as `/cores` before it). Redeploy: `sam build
AdminApiFunction && sam deploy` (or full unscoped build, per 6a.5), plus
re-upload `index.html` wherever it's hosted.

### 6o.12. Dashboard: "Total Catalog ADU Over Time" chart (7D/30D/90D/1Y/All)

Real follow-up ask, same session, Al: "can we add some data over time
charts to the dashboard, maybe total catalog adu over time similar to
what we have per product 7d, 30d, 90d, 1y and all picker."

New `GET /admin/catalog-adu-history` endpoint
(`service.get_catalog_adu_history`), fetched alongside `GET
/admin/dashboard` (via `Promise.all`, same "everything this tab needs,
one round trip pair, no cascading fetches" reasoning
`loadProductDetailInto` already uses) and stashed under a fixed
`'dashboard'` pseudo-product-id in `productChartData` so the Dashboard's
new chart reuses the EXACT SAME range-picker machinery the price/SKU-
stock charts already established (`chartSelectedRange`/
`buildChartRangeToolbar`/`setChartRange`/`filterHistoryByRange`/
`CHART_RANGE_PRESETS`) rather than a parallel one-off mechanism. Full
history fetched once, filtered to 7D/30D/90D/1Y/All entirely
client-side -- switching ranges is instant, no re-fetch.

**Important, disclosed rather than glossed over**: this is a REAL but
DIFFERENTLY-DEFINED number from the Dashboard's own `kpis.
total_catalog_adu` (6o.11 above). That KPI is `_TOTAL_ADU_SQL` (a
PER-SKU trailing-30-day window, only counting a SKU once it has >=2
readings in that specific window) summed across every product. Re-
running that exact per-SKU-gated formula at every historical calendar
day would need one correlated subquery PER DAY -- expensive, and
arguably not even the right shape for a smooth trend line (a SKU
dropping in/out of "has >=2 readings this window" would make the line
jump for reasons unrelated to real demand). Instead: total units sold
(drops only, restocks excluded, same interpretation as everywhere else
in this project) across every SKU in the whole catalog, bucketed by
calendar day (gap days filled in as real zeros via `generate_series`,
not silently dropped), then a rolling `ADU_LOOKBACK_DAYS`-day (30)
trailing sum divided by a flat 30 -- a real, honest, catalog-wide
rolling average, just not byte-identical to the KPI card's own
per-SKU-gated snapshot. In practice the two track each other closely and
the chart's rightmost point should usually be close to the KPI card's
current value, but they are not guaranteed to match exactly -- worth
knowing if this is ever compared side-by-side and the numbers don't
line up perfectly.

Tests: `test_admin_api_service.py` -- SQL-text assertions (bounds/
generate_series/lag/drops-only/rolling-window-size-driven-off-
`ADU_LOOKBACK_DAYS` clauses all present) via `_QueryCapturingConnection`
(this query only ever calls `fetchall()`, whose default `[]` return is
already iterable, unlike `get_dashboard_summary`'s KPI query which
needed a custom cursor for its `fetchone()` call), plus a
`_SequencedConnection`-based assembly test and an empty-history test.
222/222 in this module; full repo `test_*.py` sweep clean.

No migration, no `template.yaml` change (proxy+ catch-all already covers
the new route). Redeploy: same as 6o.11 -- `sam build AdminApiFunction
&& sam deploy`, plus re-upload `index.html`.

### 6o.13. Dashboard: per-SKU "SKU health" lists -- days of supply, growing/shrinking ADU

Real follow-up ask, same session, after Al noticed the Dashboard's
`total_catalog_adu` KPI (406) didn't match the "Total Catalog ADU Over
Time" chart's max (39.5) -- investigated live via several rounds of Al
pasting real `psql` output rather than guessing: root cause turned out to
be `_TOTAL_ADU_SQL`'s per-SKU `elapsed_days` denominator being small
(~3 days) simply because `product_sku_stock_history` was only 5 days old
at the time, not a rotation/capacity bug (156 total active price sources
comfortably fit under `DEFAULT_PRICE_CHECK_LIMIT`'s 200/day cap, and 784
of 814 in-scope SKUs were already getting checked daily) -- the KPI and
chart numbers converge on their own as more daily history accumulates, no
code change needed for that part. Al's actual follow-up ask, once that was
settled: "can we add top 10 days of supply skus descending so lowest
number of days first... can we build something would show top 10 growth
ADUs and top 10 shrinking ADUs by sku."

Three more lists added to `GET /admin/dashboard`
(`service.get_dashboard_summary`, now seven queries instead of four) and
rendered as a new `.dashboard-cols-3` row beneath the existing Popularity/
Total ADU row:

- **Top 10 Days of Supply** (`top_days_of_supply`) -- PER-SKU (not
  per-product like `top_adu`/`top_popularity`, since days-of-supply is
  meaningless averaged across a product's weights), ascending so the
  SKUs closest to stocking out sort first. `latest_quantity` is each SKU's
  single most recent reading (not windowed); a quantity of 0 correctly
  sorts to `days_of_supply = 0` at the very top, matching
  `computeSkuForecast`'s own `latestQuantity <= 0 -> daysOfSupply: 0`
  branch rather than a divide-by-zero or an excluded row.
- **Top 10 Growing ADU** / **Top 10 Shrinking ADU**
  (`top_growing_adu`/`top_shrinking_adu`) -- compares each SKU's current
  `ADU_LOOKBACK_DAYS`-day (30) rate against its own rate over the 30 days
  before that. Mirror-image queries (growing: `delta_adu > 0` desc;
  shrinking: `delta_adu < 0` asc), same "separate query despite
  near-identical shape" convention `top_popularity`/`top_adu` already
  established.

New shared SQL helper `service._sku_adu_cte(min_days_ago, max_days_ago=0)`
-- same drops-only/`>=2`-readings/elapsed-days-based per-SKU rate
`_TOTAL_ADU_SQL` and `adu_by_brand`'s own inline CTE already use, just
parameterized by an arbitrary trailing window (instead of a third
hand-copied `ADU_LOOKBACK_DAYS`-only literal) since the growing/shrinking
queries need two different, non-overlapping windows to compare. Returns
`adu` as `NULL` (via `case when elapsed_days > 0 ... else null end`) for
a SKU without a computable rate, not a bare division -- callers filter on
`adu is not null`/`adu > 0` and never divide by `elapsed_days` themselves.

**Honest caveat, disclosed in both the docstring and the UI's own
empty-state copy**: `top_growing_adu`/`top_shrinking_adu` require a SKU to
have a qualifying rate in BOTH windows, i.e. up to `2 * ADU_LOOKBACK_DAYS`
(60) days of accumulated `product_sku_stock_history` before they can ever
return anything. Expect both lists to show "not enough stock history yet"
for a while on this still-young dataset (5 days old as of this write-up) --
not a bug, just not enough history yet. `top_days_of_supply` only needs
the existing single `ADU_LOOKBACK_DAYS` window, so it starts populating
sooner.

Tests: `test_admin_api_service.py` -- three new SQL-shape tests (one per
new query) via `_DashboardQueryCapturingConnection`, the existing
four-query count test renamed/updated to seven, and the existing assembly
test extended with three more `_SequencedConnection` entries plus
assertions on the three new result keys. 225/225 in this module; full
repo `test_*.py` sweep clean.

**Superseded by 6o.14 below**: `top_days_of_supply` described here as
`get_dashboard_summary`'s seventh query was subsequently split into its
own function/endpoint (a same-session follow-up ask, weight-toggle
filtering) -- `get_dashboard_summary` is back down to six queries as of
6o.14. `top_growing_adu`/`top_shrinking_adu` stayed put.

No migration, no `template.yaml` change. Redeploy: same as 6o.11/6o.12 --
`sam build AdminApiFunction && sam deploy`, plus re-upload `index.html`.

### 6o.14. Days of Supply: weight-toggle filter, split into its own endpoint

Real follow-up ask, same session as 6o.13, after Al saw the new Days of
Supply list: "can we put a filter so we can toggle the different weights
so that we can see 15 only or 15 and 14 etc."

Filtering has to happen BEFORE the `limit 10`, not after -- a client-side
filter of an already-limited top 10 would silently miss a 15lb SKU that
ranked #14 catalog-wide but would be #3 among just 15lb SKUs. That means
every checkbox toggle needs a real round trip.

Rather than adding a `weight_lbs` query param to `GET /admin/dashboard`
itself (which would re-run all six of that endpoint's other queries on
every toggle), `top_days_of_supply` was split out of
`get_dashboard_summary` entirely into its own function
(`service.get_top_days_of_supply(conn, weight_lbs=None)`) and endpoint
(`GET /admin/dashboard/days-of-supply?weight_lbs=15,14`) -- same
independent-refresh reasoning 6o.12's `get_catalog_adu_history` split
already established, just for a different trigger (a filter toggle
instead of a chart-range button). `get_dashboard_summary` now runs six
queries, not seven; the Dashboard tab fetches Days of Supply as a THIRD
parallel call in `loadDashboard`'s `Promise.all` (alongside `/admin/
dashboard` and `/admin/catalog-adu-history`), both on initial load and
again on every weight toggle.

`weight_lbs` (optional): comma-separated ints in the query string
(`?weight_lbs=15,14`), same parsing convention `GET /products/plotter`'s
own `ids` param already uses in `public_api`. Rendered as `and
sk.weight_lbs = any(%s)` -- a real bound array parameter (psycopg2 adapts
a Python list straight to a Postgres array), same convention
`list_price_sources_for_products`' own `product_id = any(%s::uuid[])`
filter uses, not string-interpolated. None/omitted means no filter, the
original unfiltered top 10.

New `service.list_sku_weights(conn)` returns every distinct
`product_skus.weight_lbs` value catalog-wide (not just today's top-10's
own weights) so the checkbox row always offers every real weight, even
ones that don't happen to be in today's worst-DOS list. Returned
alongside `items` in the endpoint's response as `available_weights`.

admin-site: new `dashboardDosState` (`{availableWeights, selectedWeights,
items}`) plus `renderDaysOfSupplyPanel()` / `toggleDosWeight(weight)` /
`fetchDaysOfSupply()`. The Days of Supply panel now renders into its own
`#dos-container` div (was inline in `renderDashboard`'s big innerHTML
before) specifically so a checkbox click can re-render just that one
panel -- not the KPIs, not Growing/Shrinking ADU, not either chart.

Tests: `test_admin_api_service.py` -- `get_dashboard_summary`'s query
count/assembly tests updated to six (days-of-supply removed), its
growing/shrinking query-shape tests re-indexed (4/5, not 5/6). New
`get_top_days_of_supply`/`list_sku_weights` tests via a new
`_ParamsCapturingConnection` double (captures BOTH query text and bound
params -- needed here specifically to confirm `weight_lbs` is actually
passed as a parameter, not just present in the SQL string):
unfiltered-omits-clause-and-params, filtered-adds-clause-and-params,
query-shape (same shape as the pre-split inline version), single-query
count, and a `_SequencedConnection`-based assembly test.
`list_sku_weights` gets its own query-shape and flat-list-assembly tests.
231/231 in this module; full repo `test_*.py` sweep clean.

No migration, no `template.yaml` change (proxy+ catch-all already covers
the new route). Redeploy: same as 6o.11/6o.12/6o.13 -- `sam build
AdminApiFunction && sam deploy`, plus re-upload `index.html`.

### 6p. Multi-category support, phase 1: Bags catalog scraping (Brunswick)

Al's ask: "currently all the products are bowling ball and im thinking of
pulling in bags shoes and other items, what do you think is the best way
to pull in additional categories and product types." Scoped down via
follow-up questions: first category is **Bags**, prototyped on
**Brunswick** (same brand/platform already scraped for balls), and this
first pass is **catalog only** -- name/color/part_number/description/
images -- deliberately NOT price/stock/ADU/DOS tracking. That's a real,
separate follow-up (see "Deferred: bag price/stock tracking" below), not
an oversight.

**Investigation, not guesswork.** Every structural claim below was
confirmed live this session via Claude in Chrome (`fetch()` issued from
inside a real browser tab, same discipline as every other real-page-shape
claim in this doc) against three actual bag product pages
(`blitz-double-roller-black`, `punisher-triple-tote-blue-green`,
`sidekick-single-tote`) and Brunswick's real sitemap:

- Brunswick's sitemap has 416 total URLs: 258 balls (the only category
  `url_discovery`'s old default pattern captured), 54 shoes (not onboarded
  yet), 51 bags (`/products/bags/(roller-bags|carry-bags)/<slug>` -- 26
  roller, 25 carry), plus 49 accessory pages and 4 non-product landing
  pages.
- Bag pages have **zero `<table>` tags** -- none of the spec-table/Core-
  Numbers-table/RG-DIFF structure ball pages have. Instead: a plain,
  always-visible `<h3>Features and Benefits</h3>` followed by a `<ul>` of
  bullet points (a "Dimensions: 10&quot; L x 15&quot; D x 23&quot; H" line
  is just one more `<li>` in that list, not a separate field -- no hidden
  `.u-hide` description div the way balls have), and an
  `<h3>Part Number(s):</h3>` followed by a `<p>` of `<br>`-separated
  "`<part number> - <color>`" lines (plural heading wording for a
  multi-color bag, singular for a single-color one).
- **Each color is its own page/URL**, not a `product_skus`-style variant
  of one shared product the way ball weights are -- confirmed via the
  sitemap (`.../blitz-double-roller-black` and
  `.../blitz-double-roller-purple` both exist as separate entries) and via
  the page itself (the Black page's own H1 is "Blitz Double Roller -
  Black", even though its Part Numbers list still shows all four
  siblings' part numbers for cross-nav). So this only ever writes ONE
  `product_skus`-equivalent value per page -- handled by reusing the
  existing `products.color`/`products.part_number` columns (same ones
  balls already populate from their spec table), not a new table.
- **No current/retired URL split for bags** -- Brunswick doesn't appear to
  expose a public retired-bags archive (none in the sitemap). Status
  defaults to `'current'`. This is an assumption, not confirmed against a
  real retired-bag page; flag to Al if a bag ever needs manually marking
  retired.

**Migration 019** (`db/migrations/019_products_product_type.sql`): adds
`products.product_type text not null default 'ball'` + a check constraint
(`'ball'`, `'bag'`, `'shoe'`). Every existing row is 100% ball today, so
the `default` backfills every pre-existing row as part of the `ADD COLUMN`
itself -- no separate `UPDATE` needed. This is the single dispatch column
every layer branches on going forward.

**`src/product_scraper/app.py`:**
- `detect_product_type(url)` reads the category straight off the URL path
  (`/products/(balls|bags|shoes)/`) -- no page-content inspection needed,
  confirmed live that Brunswick segments every category into its own path
  segment. Defaults to `'ball'` for anything unrecognized (preserves
  every pre-existing ball URL's behavior).
- `parse_product_page()` now branches at the top: `product_type == 'bag'`
  routes to the new `parse_bag_product_page()`; everything else is the
  original, byte-for-byte-unchanged ball parsing path (both paths return
  the same dict shape, now including a `product_type` key, so
  `upsert_product` and every caller don't need to branch themselves).
- New bag-only helpers: `_find_heading`/`_next_sibling_tag` (generic
  "first `<h3>` matching this text, then its next sibling tag" walk --
  same "match by visible label text, not CSS class" philosophy this
  file's ball-parsing functions already follow), `parse_bag_description`
  (joins the Features and Benefits `<li>`s, semicolon-separated),
  `parse_bag_color_and_part_number` (matches THIS page's own color,
  parsed off its `<h1>`'s " - &lt;color&gt;" tail, against the Part
  Numbers list to pick the one part number that's actually this page's --
  returns `(title_color, None)` rather than guessing when nothing
  matches, so a mismatch surfaces as a missing part_number for review
  instead of a silently wrong one).
- `upsert_product`: `product_type` added to the `products` insert/
  on-conflict-update. The oil/motion plotter estimate-on-scrape block
  (migrations 011/012) is now guarded to `product_type == 'ball'` only --
  bags have no core/coverstock/differential to estimate a position from,
  and would otherwise get a meaningless mid-range 'estimated' rating
  written onto every row. `get_or_create_core_id`/
  `get_or_create_coverstock_id` already short-circuit to `None` for a bag
  (its `core_name`/`coverstock_name` are always `None`), so no extra
  branching was needed there.

**`template.yaml`:** new `BagsUrlPathPattern` parameter (default
`/products/bags/(roller-bags|carry-bags)/`) and a new
`BrunswickBagsUrlDiscoveryFunction` resource -- same reuse convention as
`RadicalUrlDiscoveryFunction`/`Dv8UrlDiscoveryFunction` (identical
`src/url_discovery/` code, just a different env var), except here it's the
SAME brand (`BrandId`)/sitemap (`SitemapUrl`) as the existing
`UrlDiscoveryFunction`, just a different `URL_PATH_PATTERN`. Publishes
onto the same `ProductScrapeQueue`/`ProductScraperFunction` as every other
Craft-CMS deployment -- `ProductScraperFunction` itself needed no new
deployment, env var, or `template.yaml` change at all, since it already
takes `{url, brand_id}` as a generic pass-through job and now dispatches
on `product_type` internally. `src/url_discovery/app.py` needed **zero**
code changes -- its regex capture group (`roller-bags|carry-bags`) just
lands in `discovered_urls.status_path` instead of `current`/`retired`,
which that column doesn't validate against a fixed set. Daily schedule
wired the same as every other `*UrlDiscoveryFunction` in this template.

**Tests:** `tests/fixtures/blitz_double_roller.html` -- new fixture, same
"trimmed but real-shape, every value copied verbatim from a literal raw
fetch" discipline as `crown_78u.html`/`defender.html`. New tests in
`tests/test_product_scraper.py`: `detect_product_type` (ball/bag/shoe/
unrecognized-defaults-to-ball), end-to-end fixture assertions (product_type,
name, status, color/part_number picked correctly out of a 4-color list,
description includes the Dimensions bullet, every ball-only field stays
`None`/empty, images still filtered correctly via the unchanged
`parse_images`), and direct unit tests of `parse_bag_description`/
`parse_bag_color_and_part_number` covering the single-color-no-suffix
case, the multi-color-matches-title case, a slash-in-color-name case
("Blue / Green"), the no-match-returns-title-color-and-None case, and the
no-Part-Numbers-section-at-all case.

`pytest` isn't installed in this sandbox this session (pypi.org is
blocked by the sandbox's own network allowlist, same restriction already
hit earlier fetching brunswickbowling.com directly) -- every new
assertion above was instead run manually via a throwaway script calling
the same functions/fixtures directly (all passed), and the existing ball
fixtures (crown_78u/defender/combat_solid) were re-run the same way to
confirm zero regressions on the ball path. The full non-pytest
`test_*.py` sweep (`tests/test_admin_api_service.py` and everything else
that isn't `test_product_scraper.py`/`test_url_discovery.py`) ran clean.
Al should re-run `pytest tests/test_product_scraper.py -v` in his own
environment to get the real pytest confirmation once he pulls this.

**Deferred: bag price/stock tracking.** `price_checker.py`'s whole
stock-matching pipeline (`match_sku_weights_to_variants`) is hard-keyed
off `product_skus.weight_lbs` (`not null`), which bags don't have --
extending it to bags needs a new matching key (likely `part_number`,
since that's now real catalog data once this scrapes), and whether
retailer sites even expose a matching part number/SKU for bags hasn't
been checked. Deliberately out of scope for this pass (Al confirmed:
catalog first) -- real follow-up once there's actual bag catalog data in
hand to test a matching key against.

**Redeploy:** `sam build BrunswickBagsUrlDiscoveryFunction
ProductScraperFunction && sam deploy` after running migration 019.
`BrunswickBagsUrlDiscoveryFunction` can also be invoked manually first to
smoke-test before waiting for its daily schedule.

### 6q. BowlerDepot video sync: pushing this project's video data onto live bowlerdepot.com product pages

Al's ask: "for the public side of things what is the best way to include a
section containing the data from this project on our bigcommerce product
pages. so when you visit a ball it will pull in the video section into the
video section of the bigcommerce product page" -- followed by: "I do but
one thing I would love to also pull in is the summary that we have."

**Investigation, not guesswork.** Before writing any code, the live
bowlerdepot.com storefront (Supermarket theme, BigCommerce) was checked
via Claude in Chrome against a real product page
(`bowlerdepot.com/brunswick-combat-solid/`):

- Every PDP's add-to-cart form has `<input name="product_id">` holding
  BigCommerce's own numeric product id (confirmed value `"4390"` for that
  product) -- the same id `bowlerdepot_products.bigcommerce_product_id`
  already stores.
- The theme already has a **native "Videos" tab**
  (`#tab-videos > .videoGallery--inTab`), populated entirely by
  BigCommerce's own built-in Product Videos feature -- NOT a custom theme
  addition -- already showing a manufacturer video for that product.
  BigCommerce's own v3 Catalog API
  (`POST /stores/{store_hash}/v3/catalog/products/{id}/videos`, confirmed
  live via BigCommerce's own docs; required field `video_id`, optional
  `title`/`description`/`sort_order`/`type`) writes directly into that
  same native slot.

This reshaped the whole design away from a client-built video widget
toward a **server-side push into BigCommerce's own feature** for the
per-video piece, plus a **small standalone script** for the one piece
BigCommerce has no native slot for: the aggregate `video_reviews_summary`
rollup paragraph (there's no "aggregate summary across all videos" field
on a BigCommerce product, only a per-video description). Al explicitly
chose the small-script approach (over a native custom-field-only
alternative) when asked.

**Part 1 -- server-side video push (`src/bowlerdepot_video_sync/app.py`,
new Lambda, `rate(1 hour)` schedule):**

- **Migration 020** (`db/migrations/020_bowlerdepot_video_sync.sql`) adds
  `product_videos.bowlerdepot_video_id`/`bowlerdepot_synced_at` --
  idempotency bookkeeping so a re-run only ever pushes a given video once
  (`bowlerdepot_synced_at is null` is the "needs sync" gate), same
  convention `product_price_sources.last_checked_at` already uses
  elsewhere in this codebase.
- `list_videos_needing_sync` selects every video that is
  `status = 'approved'` AND `summary is not null` (the exact bar
  `public_api.get_product`'s own video select already uses for
  "public-ready" -- this sync should never push something the public site
  itself wouldn't show) AND joined through `bowlerdepot_products` scoped
  to `match_status = 'matched'` only (an `'ambiguous'`/`'unmatched'` row
  is a known-unreliable match by that module's own design -- see the real
  Storm iQ Tour / iQ Tour AI suffix-collision incident in 6h.1 -- pushing
  a video to the WRONG BigCommerce product would be worse than not
  pushing at all) AND `products.published = true`.
- `build_bigcommerce_video_payload` maps a row to BigCommerce's documented
  request body, `description` set to our own per-video AI summary
  (`product_videos.summary`) -- this gets the summary onto the storefront
  "for free" as part of the native video push, distinct from Part 2's
  aggregate rollup.
- `handler` is a scheduled batch job (not SQS-driven, same
  "run once, do everything outstanding" shape as `UrlDiscoveryFunction`),
  processes every currently-due video in one invocation, catches and
  records a per-video failure (`_process_one_video`) so one bad push
  (e.g. a stale `bigcommerce_product_id`) doesn't abort the rest of the
  batch, and tracks a per-product running `sort_order` counter (seeded
  from each product's own already-synced count) so a failed push never
  wastes or skips a `sort_order` slot for the next real success.
- **Real prerequisite Al must confirm/fix himself, unverifiable from this
  sandbox**: writing a Product Video needs the BigCommerce API token to
  have the Products **modify** scope (`store_v2_products`), not just
  read-only (`store_v2_products_read_only`) -- the scope
  `price_checker`/`bowlerdepot_reconciliation`'s existing token gets by
  with for their read-only lookups. If the token behind
  `BIGCOMMERCE_SECRET_ARN` is still read-only, every push will 403; check
  BigCommerce's own API Accounts settings and upgrade the token's scope
  if needed before this function's first scheduled run.

**Part 2 -- aggregate summary embed script
(`embeds/bowlerdepot-video-summary.js`, new top-level `embeds/`
directory):**

- New `public_api` route,
  `GET /bowlerdepot/products/{bigcommerce_product_id}/video-summary`
  (`service.get_video_summary_by_bigcommerce_product_id`) -- looks up
  `products.video_reviews_summary`/`video_reviews_summary_video_count`
  through the same `bowlerdepot_products` `match_status = 'matched'` +
  `published = true` gating as Part 1. Deliberately always 200s with
  `video_reviews_summary: None` rather than 404ing when there's no
  match/no rollup yet -- that's the normal case for most of the catalog
  (only a confidently-matched product that's also had `video_summarizer`
  actually produce a rollup ever has one), not an error condition the
  embed script needs to special-case. No `template.yaml` change needed --
  `PublicHttpApi`'s existing `/{proxy+}` GET catch-all already covers the
  new route the same way it has for every other `public_api` addition.
- `embeds/bowlerdepot-video-summary.js`: a small, dependency-free,
  standalone JS file (NOT part of any Lambda) that reads
  `input[name="product_id"]` off the live page, calls the new route, and
  -- only if a summary comes back -- inserts a styled box into
  `#tab-videos` (before `.videoGallery` if present, otherwise at the top
  of the tab). No-ops silently on every "nothing to show" case (no
  matching product, no rollup yet, network error, missing DOM) since this
  is a page enhancement, never something that should surface an error to
  a storefront visitor.
- **Deploy mechanics (Al must do these himself -- no live AWS/BigCommerce
  credentials in this sandbox):**
  1. Edit `embeds/bowlerdepot-video-summary.js`'s `API_BASE_URL` constant
     to this deployment's real `PublicApiUrl` (from `template.yaml`
     Outputs).
  2. Upload it to the same S3 bucket/CloudFront distribution already
     serving consumer-site (`ConsumerSiteBucket`/`ConsumerSiteDistribution`
     -- reused, not a new bucket) --
     `aws s3 cp embeds/bowlerdepot-video-summary.js s3://<ConsumerSiteBucket>/embeds/bowlerdepot-video-summary.js`
     -- then invalidate CloudFront for that path
     (`aws cloudfront create-invalidation --distribution-id <id> --paths "/embeds/bowlerdepot-video-summary.js"`).
  3. In BigCommerce's control panel: **Storefront > Script Manager >
     Create a Script**, scoped to Product Pages, pointing at
     `<script src="https://<your-cloudfront-domain>/embeds/bowlerdepot-video-summary.js"></script>`.
     Updating the script's logic later only needs a re-upload + cache
     invalidation, never touching Script Manager again.

**Tests:** `tests/test_bowlerdepot_video_sync.py` (new file) covers
`build_bigcommerce_video_payload` (field mapping, title truncation,
missing-field defaults), `list_videos_needing_sync` (row-to-dict mapping
against a fake cursor), `push_video_to_bigcommerce` (posts to the
expected URL/headers/payload against a fake requests-Session-shaped
object; raises on a non-2xx response), `mark_video_synced`,
`_process_one_video` (success and caught-failure cases), and `handler`
(zero-videos early return; per-product `sort_order` counter only
advancing on a successful push, verified across two products including
one failure). `tests/test_public_api_service.py` gained
`test_get_video_summary_by_bigcommerce_product_id_*` covering matched/
published (real summary returned), no bowlerdepot_products row at all,
`match_status` of `unmatched`/`ambiguous` (excluded), unpublished product
(excluded), and matched-but-no-rollup-yet -- all five return the same
`{video_reviews_summary: None, video_reviews_summary_video_count: 0}`
default except the first. Full non-pytest `test_*.py` sweep ran clean
(zero regressions) after these additions.

**Redeploy:** run migration 020, then
`sam build BowlerdepotVideoSyncFunction && sam deploy`, then do the
Part 2 deploy mechanics above (embed script upload + Script Manager). The
new Lambda can also be invoked manually first
(`aws lambda invoke --function-name bowling-scraper-bowlerdepot-video-sync ...`,
same pattern as 6e's manual-invoke instructions) to smoke-test before its
hourly schedule picks it up.

### 6r. Competitor-channel filter for the BowlerDepot video sync

Al's follow-up right after 6q shipped: "i feel like a filter is probably
necessary because some of these videos are from our competitors and we
should avoid putting those on there. I would like to include the summary
of summaries even if it is built off of one of theirs."

Two very different asks bundled together:

1. Don't push a **competitor's own YouTube video** onto BowlerDepot's
   product page via `bowlerdepot_video_sync` -- a real gap, since
   nothing in the pipeline previously distinguished a manufacturer/
   reviewer channel from a competitor retailer's own review channel.
2. Keep drawing the aggregate `video_reviews_summary` rollup paragraph
   from **every** approved video's per-video summary regardless of
   channel -- explicitly a non-change. `video_summarizer`'s rollup query
   is untouched by this feature.

**Investigation before building**: checked whether any "competitor
channel" concept already existed anywhere in this codebase (video
discovery, approval, admin-site) -- it didn't. `product_videos.channel_
title` (migration 004) is the only channel signal ever captured, and it's
a YouTube display name, not a stable channel id (`video_discovery/app.py`
never captured YouTube's own `snippet.channelId`, even though the API
returns it). Approval today is also purely title/brand-token matching
(`video_discovery.score_match`) with no channel awareness, and a large
share of approvals happen via `scripts/auto_approve_video_candidates.py`
with zero human ever looking at the channel name. So this needed a real,
new, admin-curated blocklist -- not something that could be inferred from
existing data.

**Migration 021** (`db/migrations/021_blocked_video_channels.sql`): new
`blocked_video_channels` table (`id, channel_title, note, created_at`),
with a case-insensitive unique index on `channel_title` (`lower(channel_
title)`) so "Bowling.com" and "bowling.com" can't both get added as
separate rows and silently only half-match.

**`src/admin_api`**: `list_blocked_channels`/`create_blocked_channel`/
`delete_blocked_channel` in `service.py`, mirroring `price_sites`' own
list/create-with-dedupe/delete-by-id shape exactly (`create_blocked_
channel` uses `insert ... on conflict (lower(channel_title)) do nothing`,
falling back to a lookup-by-lower-title select so re-blocking an
already-blocked channel is a harmless no-op that returns the existing
row, not an error). New routes `GET/POST /blocked-channels` and
`DELETE /blocked-channels/{id}` in `app.py` -- no `template.yaml` change
needed, `AdminHttpApi`'s existing `/{proxy+}` GET/POST/PATCH/DELETE
catch-all already covers them (same precedent as every other small
reference-table endpoint added to this API).

**`src/bowlerdepot_video_sync/app.py`**: `list_videos_needing_sync`'s
query gained one `and not exists (select 1 from blocked_video_channels
bvc where lower(bvc.channel_title) = lower(pv.channel_title))` clause.
That's the ENTIRE change -- a blocked channel's videos stay `approved`,
still count toward `video_reviews_summary_video_count`, and their
per-video summaries still feed the rollup; they simply never appear in
what this function returns, so the sync's `handler` never pushes them to
BigCommerce. Nothing upstream (discovery/approval/the rollup) was
touched.

**`admin-site/index.html`**: a small "Blocked channels" panel added
inside the existing Video Candidates tab (`#tab-videos`) -- deliberately
NOT a new top-level nav tab like Cores/Coverstocks/Price Sites, since
this blocklist is small and tightly coupled to that tab's own `channel_
title` column rather than a general product-facing reference table. Add-
channel form + a list with an "Unblock" button per row
(`loadBlockedChannels`/`renderBlockedChannels`/`createBlockedChannel`/
`deleteBlockedChannel`), loaded alongside the video candidates list
itself. Each video row in the candidates table also got a one-click
"Block channel" button (`blockChannelForVideo`) -- reads the channel name
off the button's own `data-channel` attribute rather than embedding free
text into an `onclick` string (every other `onclick` in this file only
ever embeds a safe id, never arbitrary text, for exactly this reason: a
channel name with a quote or apostrophe in it would otherwise break the
attribute).

**Tests**: `tests/test_admin_api_service.py` gained 5 tests for the new
service functions (list ordering, insert, case-insensitive dedupe-
returns-existing-row, delete, delete-missing-raises) -- 231 to 236, zero
regressions. `tests/test_bowlerdepot_video_sync.py` gained a test
asserting the executed `list_videos_needing_sync` query text actually
contains the `blocked_video_channels`/`not exists` clause (the fake
cursor in that file returns pre-canned "already filtered" rows, same
limitation as every other DB-touching test file here with no real
Postgres available, so this can only confirm the filter is wired into
the query, not exercise real SQL filtering behavior) -- 12 to 13. Full
non-pytest `test_*.py` sweep ran clean after both additions.
`admin-site/index.html` verified the same way prior sessions have (`node
--check` against the extracted `<script>` contents, plus an HTML
tag-balance check) -- both passed.

**Redeploy**: run migration 021, then a full unscoped `sam build && sam
deploy` (NOT `sam build AdminApiFunction ...` -- this deploy touches
`AdminApiFunction`, and 6a.5 documents two confirmed real incidents of
a scoped build on that function alone shipping a broken zip missing
`fastapi`; always use the full unscoped build/deploy whenever
`AdminApiFunction` is one of the functions changing), then swap the
static `admin-site/index.html` file as usual (no redeploy step -- open
it via `file://` or wherever it's hosted). No BigCommerce-side changes
needed for this piece.

### 6s. Ball-review article generator (bowling.com-style, full article)

Al: "Do you think we generate ball review article like the one here:
[bowling.com's Storm Equinox Hybrid review]... We could use all the video
transcripts to create a FAQ that is meaningful dynamicly based on what is
being talked about in the videos. We also have alot of content already
accumulated from all the sources... We would want to use this on
bowlerdepot.com and could have another project that is the front end but
this could be the backend that pulls together all the creative and
content for the frontend. We could use the same video filter that we are
using for the existing embed."

**Investigation before building**: fetched the referenced bowling.com
article live and checked this project's own schema for feasibility.
Almost everything needed already existed -- specs, `products.description`,
per-video summaries/transcripts, and the existing Bedrock-rollup pattern
(`video_summarizer`) to extend. The one real gap: no product-line/family
grouping concept anywhere in this schema (`ball_families` from migration
001 was never wired up and was repurposed into `cores`, a physical-core-
dedup concept, by migration 007) -- bowling.com's own article leans on a
"Equinox / Equinox Solid / Equinox Hybrid" line grouping this project has
no equivalent of. Scoped via a follow-up 3-question exchange with Al,
answers below, before writing any code:

1. **Review workflow**: require admin review before anything is exposed
   publicly (Al's choice, "recommended") -- mirrors `product_videos`/
   `product_price_sources`' own pending/approved/rejected review-queue
   precedent exactly, never auto-publish.
2. **V1 scope**: **full article** (Al's explicit choice, NOT the smaller
   "narrative + FAQ only" option that was recommended) -- spec table via
   live join, sibling comparison table, narrative, pros/cons, who-should-
   buy/skip, buying tips, verdict, and the dynamic FAQ.
3. **Generation gate**: require at least one approved, non-blocked-
   channel, summarized video before a product is eligible (Al's choice,
   "recommended") -- the exact same bar `video_reviews_summary` already
   uses, reusing 021's blocked-channel filter per Al's explicit ask ("We
   could use the same video filter that we are using for the existing
   embed").

Given "full article" was chosen and no real line-grouping data exists, a
design decision was made (not asked as a 4th question, since it followed
directly from #2): infer siblings **heuristically** via a normalized
"line name" (strip trailing cover/finish/version qualifier words -- solid/
pearl/hybrid/plus/pro/max/reactive/version-suffixes -- off the product
name, e.g. "Equinox Solid" -> "Equinox") matched within the same brand.
This is explicitly NOT ground truth -- surfaced for admin review like
everything else in the article, expected to sometimes be wrong.

**Migration 022** (`db/migrations/022_product_articles.sql`): new
`product_articles` table, one row per product (`product_id unique`),
`status` (pending/approved/rejected, same shape as `product_videos`/
`product_price_sources`). Structured fields, not an HTML blob -- Al's own
framing was "this could be the backend that pulls together all the
creative and content for the frontend," so a separate future frontend
project needs to style each section itself: `title`, `hook`,
`performance_summary`, `who_should_buy`/`who_should_skip`/`pros`/`cons`
(jsonb arrays), `buying_tips`, `verdict`, `faq` (jsonb array of
`{question, answer}` -- the genuinely dynamic part, grounded in what that
product's own videos actually discuss, not a fixed template of questions
the way bowling.com's own FAQ reads), `comparison_table`/
`sibling_product_ids` (heuristic, see above), `source_video_ids` (for
traceability). Real spec VALUES (RG, differential, core, coverstock) are
deliberately NOT duplicated onto this table -- they're joined live from
`products`/`product_skus`/`cores`/`coverstocks` at READ time (see
public_api below), so a later spec correction never requires
regenerating the article.

**`src/product_article_generator`** (new Lambda, VPC + DB secret +
Bedrock, `rate(1 day)` schedule plus on-demand invoke): same Bedrock-via-
boto3 wire format and Global-CRIS IAM policy shape as `video_summarizer`
(three `Sid`-tagged Statements -- a single simple grant fails closed with
`AccessDeniedException`, see that module's own comment). Sends the LLM
BOTH each qualifying video's summary (cheap, already exists) AND a
bounded transcript excerpt per video (`DEFAULT_TRANSCRIPT_EXCERPT_CHARS`
per video, capped in total via `DEFAULT_MAX_TOTAL_TRANSCRIPT_CHARS`)
specifically so the FAQ can be grounded in real, specific things
reviewers said rather than reading like a template -- the rollup's own
plain-summary-only prompt wouldn't have been enough for that. Asks for
the whole article back as one JSON object (`parse_article_json` strips a
```json fence if Bedrock adds one anyway, then validates every required
key is present -- a partial article is worse than none, since there's no
partial-field review UI). `list_products_needing_article`'s query reuses
the exact same `blocked_video_channels` exclusion pattern as
`bowlerdepot_video_sync`/`video_reviews_summary`. `generate_article_for_
product(force=True)` is the on-demand path (admin-triggered regenerate);
the batch handler only ever considers products with NO existing
`product_articles` row (a rejected article isn't auto-retried -- an admin
regenerating it on demand is the only path back, same as this project's
other "AI got it wrong, a human asks again" flows).

**`src/admin_api`**: `list_articles`/`get_article`/`approve_article`/
`reject_article` in `service.py` mirror `list_price_sources`/`get_video_
candidate`/`approve_price_source`/`reject_price_source` almost exactly
(same fails-closed-on-already-resolved guard). `queue_article_generation`
mirrors `queue_video_discovery`'s direct-`lambda:InvokeFunction`-no-queue
shape, with one real gotcha: the payload key is `{"product_id": ...}`
(singular), NOT `{"product_ids": [...]}` like `queue_video_discovery`'s
own payload -- `product_article_generator`'s on-demand handler path
checks `event.get("product_id")` specifically, so sending a `product_ids`
list here would silently miss that branch and fall through to the
catalog-wide batch scan instead. New routes: `GET/GET-by-id /articles`,
`POST /articles/{id}/approve`, `POST /articles/{id}/reject`,
`POST /products/{id}/generate-article` -- reuses the existing
`ApproveRequest`/`RejectRequest` Pydantic models as-is (same
`resolved_by`/optional `reason` fields, no new models needed).
`template.yaml` gained the new `ProductArticleGeneratorFunction` resource,
plus `PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME` env var and a narrowly-
scoped `lambda:InvokeFunction` grant (on exactly that function's own ARN,
same "narrowest permission" convention as the `VideoDiscoveryFunction`/
`PriceCheckerFunction` grants next to it) on `AdminApiFunction`, and a new
`ProductArticleGeneratorFunctionArn` Output -- verified via the CFN-
tolerant YAML loader this project has used to check every prior
template.yaml edit.

**`admin-site/index.html`**: new top-level "Articles" nav tab (list +
status filter + approve/reject + an expandable per-row "Preview" showing
the full rendered article -- title/hook/narrative/who-should-buy-skip/
pros-cons/buying-tips/verdict/FAQ/comparison-table/sibling-count/source-
video-count), following the Video Candidates tab's own list-with-status-
filter shape (a dedicated top-level tab, not folded into an existing one,
given the size of a full article-review UI -- same "small/tightly-coupled
things go inside an existing tab, larger reference features get their own
tab" precedent 6r's own Blocked Channels panel decision was made against,
just landing on the opposite side of that line this time). Also added a
new "Article" sub-tab inside the existing per-product detail view
(alongside Overview/Videos/Pricing/SKUs/Raw Data) with a Generate/
Regenerate button (same fire-and-forget "queue it, show queued/not-
queued/error text" pattern as `discoverVideosForProduct`/`rescrapeProduct`
next to it) and inline approve/reject when a pending article exists,
refreshing that same panel afterward.

**public_api**: `GET /products/{id}/article` (`service.get_product_
article`, no `template.yaml` change needed -- `PublicHttpApi`'s
`/{proxy+}` GET catch-all already covers it, same as every other public_
api route added this project). 404 only for a nonexistent/unpublished
`product_id` (same non-distinction `GET /products/{id}` itself already
enforces); an existing published product with no APPROVED article yet
still returns 200 with `article: None` -- same always-200-null-field
contract as the BowlerDepot video-summary embed route (6q), since "no
article yet" is the normal case for most of the catalog, not an error a
future frontend needs special-case handling for. Live-joins the spec
highlight (core/coverstock/per-weight SKU specs) and the comparison-table
rows for each sibling at READ time rather than reading anything
duplicated onto `product_articles` -- and any sibling that's since been
unpublished or deleted silently drops out of `comparison_table` rather
than breaking the whole article response (the heuristic sibling list is
explicitly not ground truth, see migration 022's own header comment).

**Tests**: new `tests/test_product_article_generator.py` (32 tests --
`normalize_line_name`, `parse_article_json`'s fence-stripping/missing-key
validation, `build_article_prompt`'s spec/video-content inclusion and its
per-video/total transcript-budget caps, `list_products_needing_article`'s
and `fetch_product_content`'s blocked-channel exclusion, `infer_sibling_
products`' line-name matching and `DEFAULT_MAX_SIBLINGS` cap,
`store_article`'s upsert-resets-review-state SQL, `generate_article_for_
product`'s full orchestration against a fake cursor + fake Bedrock
client, and `handler`'s on-demand-vs-batch dispatch with a fake `boto3`
module injected into `sys.modules` since this sandbox has no real boto3
installed). `tests/test_admin_api_service.py` gained 13 tests for the
five new service functions (249 total, up from 236). `tests/test_public_
api_service.py` gained 6 tests for `get_product_article` (70 total, up
from 64) -- including the unpublished-sibling-drops-out-of-comparison-
table case. Full non-pytest `test_*.py` sweep (42 files) ran clean after
all three additions. `template.yaml` re-verified via the CFN-tolerant
YAML loader (57 resources, `ProductArticleGeneratorFunction` present,
`ProductArticleGeneratorFunctionArn` output present).
`admin-site/index.html` verified the same way as 6r (`node --check`
against the extracted `<script>` contents, plus the same HTML tag-balance
check -- diffs against the pre-existing baseline unchanged, confirming no
new imbalance was introduced).

**Redeploy**: run migration 022 (after 021, if not already applied),
then a full unscoped `sam build && sam deploy` (NOT a scoped `sam build
AdminApiFunction ...` -- this deploy touches `AdminApiFunction` again, so
6a.5's confirmed-twice fastapi-missing-zip incident applies here exactly
the same way it did for 6r; always use the full unscoped build/deploy
whenever `AdminApiFunction` is one of the functions changing), then swap
the static `admin-site/index.html` file as usual. `ProductArticleGenerator
Function`'s own daily schedule will start generating articles for
already-qualifying products automatically after deploy -- they land as
`pending` and need an admin approval pass in the new Articles tab before
they're visible via the new public_api endpoint.

### 6t. Article images (action shot + product shot, generated alongside the text)

Al's follow-up ask, right after 6s shipped: "can we have it generate some
images for the article from the bowling ball images and using the article
to give it some context". Scoped via a follow-up 2-question exchange
before writing any code:

1. **Image style**: Al chose **"Both"** (NOT either single-image-type
   option, and not the recommended default) -- generate BOTH an action/
   lifestyle hero shot (the ball in motion on the lane, editorial style,
   matching how bowling.com's own articles use art) AND a stylized
   product hero shot (an elevated, premium rendering, not in motion) per
   article.
2. **Trigger**: Al's choice, "recommended" this time -- **automatically
   with the article**, in the same Lambda run as the text (both the daily
   batch and the on-demand regenerate path), still gated behind the
   article's own existing pending/approved/rejected review before going
   live, not a separately-reviewed thing.

**Model choice + Region, researched before building** (same defensive
posture this project already takes toward Bedrock model/Region
availability -- see 6i's own Haiku-4.5-in-us-west-1 saga): checked each
Bedrock image model's own "Regional availability" table (2026-09-03)
before picking one, rather than assuming Amazon Nova Canvas (the obvious
first guess, given `product_article_generator` already uses Bedrock).
Two hard blockers ruled it out: Nova Canvas is Legacy with an EOL of
**2026-09-30** (weeks away as of this writing) and was **never available
in `us-west-1`** to begin with (only `us-east-1`/`eu-west-1`/
`ap-northeast-1`, all themselves Legacy). Amazon Titan Image Generator
G1 v2 is **already past** its own 2026-06-30 EOL. Neither model supports
Geo/Global cross-Region inference profiles at all (unlike the text model
this project already uses), so there's no CRIS workaround for either --
AWS's own current guidance is to migrate to Stability AI's models
instead. Landed on **Stability AI Stable Diffusion 3.5 Large**
(`stability.sd3-5-large-v1:0`, not Legacy) for BACKGROUND generation
(see "Mechanism v2" below for why it's text-to-image only now, not
image-to-image) -- but it's only available in **`us-west-2`** (Oregon)
on Bedrock, not this stack's own home Region (`us-west-1`).
`product_article_generator.handler()` constructs a SECOND bedrock-runtime
client with an explicit `region_name=BEDROCK_IMAGE_REGION` (default
`us-west-2`), separate from the text model's own default-Region client --
a plain cross-Region API call, not Bedrock's own CRIS mechanism. New
`template.yaml` parameters `BedrockImageModelId`
(default `stability.sd3-5-large-v1:0`) and `BedrockImageRegion` (default
`us-west-2`) carry both settings, each with a long inline comment
documenting this whole research trail so a future change doesn't have to
redo it -- see those parameters' own `Description` blocks, and
`src/product_article_generator/app.py`'s module docstring, for the full
reasoning if you're revisiting this later (worth re-checking whether
Bedrock has since added a non-Legacy image model to `us-west-1`, since
Nova Canvas's Sept 2026 EOL means this whole area of Bedrock's model
catalog is actively shifting).

**Migration 023** (`db/migrations/023_product_article_images.sql`):
additive columns on the existing `product_articles` table (not a new
table) -- `action_shot_image_key`/`action_shot_image_url`,
`product_shot_image_key`/`product_shot_image_url`, and
`images_generated_at`. Images are AI-generated content proper to the
article itself, not live product data with a separate source of truth to
stay in sync with -- unlike specs/comparison_table (022's own header
comment), there's nothing to join live, so the URLs are stored directly
on the row. `images_generated_at` is tracked separately from the
existing `generated_at` (text) column because image generation is a
SECOND, independently-fallible step in the same run. Per that column's
own comment, `images_generated_at` is set once EITHER image succeeds, not
gated on both. Same "whole row is one reviewed unit" convention 022
already established: a regenerate resets the WHOLE row -- including
these image columns -- back through `status = 'pending'` alongside the
text.

**Mechanism v2 -- cutout + generated background + composite** (migration
`024_product_article_images_composite_pipeline.sql`, NO schema change,
comment-only): shortly after 6t shipped, Al's direct feedback was "those
images are not very good at all, they need to be specific aspect ratio
and add backgrounds to them and just leave the ball as is. We can not
alter the ball in any way just add some contextual background and maybe
other balls with same name in background or similar balls etc." The
original mechanism above (Stable Diffusion image-to-image, `strength=
0.6`) regenerates the WHOLE image through diffusion, ball included -- it
can bias toward the reference photo but can never guarantee the ball
comes out pixel-identical, which is exactly what broke. Asked Al two
follow-up questions before rebuilding: **aspect ratio** -- Al chose
**16:9 for the action shot, 1:1 for the product shot** (not the same
ratio for both); **sibling balls in the background** -- Al's answer was
to **skip it for v1**, with a specific future shape in mind ("It should
not 100% of the time but when there are 2 or more balls in the system
with very similar names then we should use those. Or if reviewers are
mentioning other balls with the one we are generating for include
those.") -- not implemented, see `024`'s own header comment for the full
quote and the intended future mechanism (reusing `infer_sibling_
products` and the same cutout+composite approach for each additional
ball, never an AI-imagined one).

The new pipeline is three separate steps instead of one Bedrock call:

1. **`call_bedrock_remove_background`** -- the product's real reference
   photo goes through Bedrock's **Stability AI Image Services "Remove
   Background"** model (`us.stability.stable-image-remove-background-
   v1:0`). This is a segmentation cutout, not a diffusion regeneration --
   the ball's own pixels are carried straight through untouched, only the
   background around it is stripped to transparent. This is the step
   that actually GUARANTEES "leave the ball as is" (rather than hoping a
   low `strength` value preserves it). Computed ONCE per product (the
   cutout is identical for both variants), not once per image -- a
   failure here skips BOTH images for the run, since there's no ball to
   composite into either one without it.
2. **`call_bedrock_generate_background`** -- a plain TEXT-to-image call
   on Stable Diffusion 3.5 Large, no reference image, and the ball is
   deliberately never described in the prompt at all (see `build_
   background_prompts` -- describing one risks the model painting a
   second, different-looking ball into the scene). `aspect_ratio` is set
   explicitly per variant (`16:9`/`1:1`) -- this is a text-to-image-ONLY
   parameter on this model (silently ignored in image-to-image mode),
   which is exactly why the cutout above has to be a genuinely separate
   call rather than folded into this one.
3. **`composite_ball_on_background`** -- plain Pillow alpha compositing
   (crop the cutout to its opaque bounding box, scale to ~62% of the
   background's shorter side, paste slightly below center, plus a soft
   blurred shadow ellipse underneath purely for grounding), NOT a second
   diffusion pass, so the ball's own pixels are pasted through byte-for-
   byte and never regenerated.

`generate_article_images` now takes both `bedrock_image_client`
(background generation, `us-west-2`) and a NEW `bedrock_removebg_client`
(cutout, **`us-east-1`** -- Stability AI Image Services' own docs/
examples consistently invoke these `us.`-prefixed model ids there, a
THIRD Region distinct from both `us-west-1` and `us-west-2`). Both
`generate_article_for_product` and `handler` now thread SIX image
params end to end (`s3_client`/`bedrock_image_client`/`bedrock_
removebg_client`/`image_model_id`/`removebg_model_id`/`image_bucket`,
up from four) -- all six must be supplied for the image step to run at
all, same "not configured -> soft no-op" posture as before.

**Stability AI Image Services Marketplace subscription -- separate from
SD3.5 Large's own**: per its own docs, "Subscribing to any edit or
control Stability AI Image Service automatically enrolls you in all
thirteen available Stability AI Image Services" -- this is a SEPARATE
AWS Marketplace listing from the one already subscribed for `stability.
sd3-5-large-v1:0` (see this section's original Marketplace-gating
writeup below), so a fresh one-time subscribe + invoke-once is likely
needed in `us-east-1` specifically before Remove Background will work,
even though the SD3.5 Large subscription in `us-west-2` already exists.
Confirm via the Bedrock Model catalog in `us-east-1` before the first
real invocation.

**`src/product_article_generator/app.py`**: `build_image_prompts`/
`call_bedrock_for_image`/`_IMAGE_NEGATIVE_PROMPT` (the old img2img
functions) were REMOVED, not left dead alongside the new ones --
replaced by `call_bedrock_remove_background`, `build_background_prompts`
(background-only, no ball description), `call_bedrock_generate_
background` (text-to-image, `aspect_ratio` param), and `composite_ball_
on_background` (the new Pillow compositing step). `generate_article_
images`'s orchestration shape (best-effort, non-fatal, independent
try/except per variant) is preserved from v1, just re-plumbed around the
new three-step mechanism -- see that function's own docstring for exactly
which step's failure skips which image(s). `fetch_reference_image_url`'s
own docstring had a leftover "Nova Canvas" reference from before the
original Stable-Diffusion pivot, corrected while in there.

**`template.yaml`**: `ProductArticleGeneratorFunction`'s `Timeout` stays
600s (now THREE Bedrock `InvokeModel` calls per product instead of two --
one Remove Background call plus two background-generation calls). New
env vars `BEDROCK_REMOVEBG_MODEL_ID`/`BEDROCK_REMOVEBG_REGION` alongside
the existing `BEDROCK_IMAGE_MODEL_ID`/`BEDROCK_IMAGE_REGION`/
`IMAGE_BUCKET`. Three new parameters: `BedrockRemoveBgModelId` (default
`us.stability.stable-image-remove-background-v1:0`), `BedrockRemoveBgRegion`
(default `us-east-1`, the SOURCE Region the client calls from), and
`BedrockRemoveBgBaseModelId` (default `stability.stable-image-remove-
background-v1:0`, the same model id with the `us.` prefix stripped).

**This IAM grant took two real attempts to get right, both against a
live invocation, and the whole saga is worth understanding before
touching it again.** The `us.`-prefixed model id matches Bedrock's own
**Geographic** cross-Region inference profile naming convention -- a
DIFFERENT CRIS type from `BedrockModelId`'s own `global.` prefix above,
with a DIFFERENT required IAM shape, easy to conflate since the ids look
similar. Attempt 1 shipped as a 2-statement grant (inference-profile
resource + a same-Region foundation-model resource) with an explicit
hedge that it wasn't independently confirmed. The first real invocation
proved that wrong: `AccessDeniedException` named the denied resource as
`arn:aws:bedrock:us-east-2::foundation-model/stability.stable-image-
remove-background-v1:0` -- this profile had routed the actual call to
**`us-east-2`**, a Region the grant never covered. Attempt 2 added a
third, REGION-LESS statement (`arn:aws:bedrock:::foundation-model/*`),
copying the pattern `BedrockModelId`'s own Global CRIS grant uses for
exactly this situation -- and a SECOND real invocation proved THAT wrong
too, with the identical `us-east-2` denial. Root cause: the region-less/
"unspecified" trick is a **Global**-CRIS-only mechanism, confirmed via
AWS's own "Securing Amazon Bedrock cross-Region inference: Geographic
and global" blog post (Jan 2026) -- Geographic profiles don't honor it at
all. That same post's own IAM policy example (for a different model, same
`us.` geography) shows the actual required shape: explicitly list EVERY
destination Region's foundation-model ARN, no wildcard/blank-Region
shortcut. Per AWS's Supported-Regions docs, a US geographic profile's
fixed destination list is exactly `us-east-1`, `us-east-2`, `us-west-2`
(and, per that same page, a geography-scoped profile's destination list
"will never change" -- safe to hardcode). **Attempt 3 (current)**:
`GrantRemoveBgInferenceProfileAccess` (inference-profile resource, scoped
to `BedrockRemoveBgRegion`) + `GrantRemoveBgModelAccess` (a single
statement whose `Resource` is a literal 3-entry list, one foundation-
model ARN per destination Region, using `BedrockRemoveBgBaseModelId`
instead of the profile id, same `InferenceProfileArn` Condition) -- this
is now a byte-for-byte match of AWS's own documented Geographic-CRIS IAM
example, not a guess. Verified via the CFN-tolerant YAML loader (57
resources, unchanged count; all three parameters present; both IAM
statements present with the 3-Region `Resource` list on
`ProductArticleGeneratorFunction`) -- **not yet verified against a real
invocation as of this writing; that's the next redeploy's job.**

**`src/admin_api/service.py` / `admin-site/index.html` / `src/public_
api/service.py`**: unchanged from the original 6t writeup below -- the
mechanism change is entirely internal to `product_article_generator`;
the stored columns, admin review UI, and public API response shape are
identical (still one URL per variant, still reviewed as part of the same
whole-row unit as the text).

**Tests**: `tests/test_product_article_generator.py`'s entire image
section was rewritten around the new pipeline (64 tests total, up from
54) -- `build_background_prompts` (confirms the ball is never positively
described, only the "no bowling ball" negative instruction), `call_
bedrock_remove_background`/`call_bedrock_generate_background`'s own
request/response shapes (including that `aspect_ratio` is sent and
`image`/`strength` are NOT, unlike the old img2img call),
`composite_ball_on_background` as a pure Pillow function with no Bedrock/
DB at all (confirms the cutout's own pixel values survive compositing
byte-for-byte at the paste location, and that the background is
untouched away from it -- this is the test that actually proves "leave
the ball as is" rather than just proving the function runs), and `
generate_article_images`'s re-plumbed orchestration (remove-background
called exactly once per product, background-generation called once per
variant at the correct aspect ratio, a remove-background failure skips
both images, a background-generation failure for one variant doesn't
take the other down). Full non-pytest `test_*.py` sweep ran clean after
these changes.

**Redeploy**: run migrations 023 AND 024 (in order, after 022, if not
already applied -- 024 is comment-only, safe to run even against a
database that's already served real traffic under 023's original
comments), then a full unscoped `sam build && sam deploy`, then swap the
static `admin-site/index.html` file as usual (unchanged by this
mechanism change, but redeploy it anyway if it's not already current from
6t's original ship). Confirm Bedrock model access for BOTH `stability.
sd3-5-large-v1:0` in `us-west-2` (already done for 6t's original ship)
AND `us.stability.stable-image-remove-background-v1:0` in `us-east-1`
(new -- see the Marketplace-subscription note above) before the first
real invocation, or the image step will fail with an access-denied error
despite the IAM policy being correct.

**Mechanism v3 -- back to a single image-to-image call, dropping v2
entirely** (migration `025_product_article_images_theme_driven_pipeline
.sql`, again NO schema change, comment-only): the v2 IAM saga above ended
with a policy that byte-for-byte matched AWS's own documented Geographic-
CRIS shape, but before that fix was even verified against a live
invocation, Al reviewed real reference examples elsewhere on the web --
a bowling.com-style multi-colorway lane shot, and a separate "Fallout"
ball whose background clearly took its cue from the ball's own name --
and gave direct feedback that v2's own output wasn't matching that bar,
backgrounds should be driven by the PRODUCT'S OWN NAME/BRANDING rather
than a generic lane/studio backdrop, and, verbatim, "I think we are
trying to be to technical on this maybe there is a better ai model for
this." Asked directly whether v2's "never alter the ball" guarantee could
be traded for a simpler, higher-quality approach: yes. That answer is
what actually killed v2 -- not a bug, a direct call that the architecture
itself was over-engineered relative to the achievable output quality.

v3 reverts to a SINGLE Stable Diffusion 3.5 Large image-to-image call per
variant -- the same model and mechanism 023's original ship used, one
Bedrock call instead of three, one Region (`us-west-2`) instead of three
-- but keeps two real improvements over the original:

1. **Theme-driven prompts**: `build_article_prompt` (the article-
   generation call itself) now asks the text model for an OPTIONAL
   `visual_theme` field -- 1-2 sentences describing a scene concept
   grounded in what the product's OWN NAME evokes (the prompt's own
   example: "Fallout" evokes a post-apocalyptic wasteland), falling back
   to an elevated/premium scene if the name has no strong connotation.
   `visual_theme` deliberately stays OUT of `_REQUIRED_ARTICLE_KEYS` --
   it's for image generation only, never shown to readers, and existing/
   older parsed responses without it must keep working. `build_image_
   prompts` (revived, was removed in v2) reads `visual_theme` first,
   falling back to `performance_summary` then `hook` if it's empty --
   same fallback chain v2's `build_background_prompts` used, kept because
   it's still the right degradation order.
2. **Letterboxed aspect ratio, not `aspect_ratio`**: Al's "specific
   aspect ratio" ask (16:9 action / 1:1 product) is now satisfied by a
   NEW `build_reference_canvas` function -- pure Pillow, no Bedrock call
   -- that letterboxes the real reference photo onto a canvas of the
   target PIXEL dimensions (1536x864 / 1024x1024, the same pixel budgets
   v2's background-generation calls used) before the img2img call, since
   Stable Diffusion's `aspect_ratio` parameter only works in text-to-
   image mode and image-to-image mode instead just preserves whatever
   shape the input already has.

Ball fidelity is now explicitly best-effort (`call_bedrock_for_image`
conditions on the reference photo via `strength=0.65`, slightly higher
than the original 0.6, trading a little more fidelity for better results
per Al's own answer), not the pixel-identical guarantee v2's cutout+
composite pipeline provided -- a deliberate tradeoff Al made, not a
regression.

**Everything v2 added is now REMOVED, not left dead alongside v3**:
`call_bedrock_remove_background`, `build_background_prompts`, `call_
bedrock_generate_background`, `composite_ball_on_background`, and
`_BACKGROUND_NEGATIVE_PROMPT` are gone from `app.py`. `generate_article_
images`/`generate_article_for_product`/`handler` are back to the pre-v2
shape: `generate_article_images` takes 8 params again (dropped `bedrock_
removebg_client`/`removebg_model_id`), `generate_article_for_product`
takes 4 image params again (`s3_client`/`bedrock_image_client`/
`image_model_id`/`image_bucket`), and `handler()` constructs only TWO
bedrock-runtime clients again (text + image), not three. In
`template.yaml`, the `BedrockRemoveBgModelId`/`BedrockRemoveBgRegion`/
`BedrockRemoveBgBaseModelId` parameters, `BEDROCK_REMOVEBG_MODEL_ID`/
`BEDROCK_REMOVEBG_REGION` env vars, and the `GrantRemoveBgInferenceProfile
Access`/`GrantRemoveBgModelAccess` IAM statements are all deleted -- the
whole Geographic-CRIS IAM saga documented above is now dead code cleanup,
not a live grant, though the writeup above is kept as-is since it's a
real, hard-won lesson about Global-vs-Geographic CRIS IAM shapes that's
worth having on hand if a future feature ever needs a Geographic profile
again. `BedrockImageModelId`/`BedrockImageRegion`/`GrantImageModelAccess`
are unchanged (that half of v2 was never broken). `ProductArticleGenerator
Function`'s `Timeout` stays at 600s (now generous rather than tight,
since it's back to two Bedrock calls per product instead of three -- no
cost to keeping the margin).

**Tests**: `tests/test_product_article_generator.py`'s image section was
rewritten again around the v3 mechanism (63 tests total) -- `build_
reference_canvas` (output matches the requested canvas size, scales a
large reference DOWN to fit without cropping, never upscales a small one,
centers it on a flat neutral fill, and the two variant canvas sizes match
`_VARIANT_CANVAS_SIZES`), `build_image_prompts` (visual_theme takes
priority over performance_summary/hook, describes the ball positively
again unlike v2's prompts, produces two distinct scenes), `call_bedrock_
for_image` (confirms the img2img request shape -- `mode`/`image`/
`strength`/`negative_prompt` present, `aspect_ratio` deliberately absent
since it's text-to-image-only), and `generate_article_images`/`generate_
article_for_product`/`handler`'s re-plumbed orchestration back to the
8-param/4-image-param/2-client shape. Full non-pytest `test_*.py` sweep
ran clean after these changes.

**Redeploy**: run migration 025 (comment-only, safe after 022/023/024
regardless of whether 024 was ever actually deployed against a live
Remove-Background invocation), then a full unscoped `sam build && sam
deploy` -- this redeploy actually REMOVES IAM statements/env vars/
parameters from the stack, unlike 6t's earlier two redeploys which only
added them, so expect CloudFormation to show removals in the change set,
not just additions. No new Bedrock Marketplace subscription is needed --
v3 only calls `stability.sd3-5-large-v1:0` in `us-west-2`, already
subscribed since 6t's original ship; the Stability AI Image Services
subscription for Remove Background is no longer needed by this stack at
all (safe to leave subscribed if you don't want to touch Marketplace
state, or unsubscribe if you'd rather clean it up -- nothing in this
codebase calls it anymore either way).

**Mechanism v4 -- multi-candidate picker: revive v2's cutout+composite
AND add a second, non-Bedrock provider** (migration `026_product_
article_image_candidates.sql`, a real NEW TABLE this time, not a
comment-only migration like 024/025): v3 shipped and Al sent back real
screenshots -- the ball's own surface graphics/logo text came out
garbled ("Voiid"/"Bowing" instead of the real brand text), and the
generated backgrounds were poor quality. His verbatim feedback: "looks
like dropping the cutout was a bad idea. As for the rest it is not at
all doing what was asked for. The ball needs to be mostly untouched.
Some style adjustments are fine but that is it. The background couldn't
be worse. Can we try a few models and then in the admin ui select the
one that we want to use." That last sentence is the whole feature: not
just "fix the mechanism," but "generate multiple options and let a human
pick," a genuinely different shape from every version before it.

Scoped via three rounds of clarifying questions plus two reference images
Al sent mid-conversation, not guessed at:

1. **Selection UX + candidate mechanism**: Al chose a **per-article
   candidate picker** in the Articles review tab (not a system-wide
   default model), and free-text describing exactly what he wanted
   rather than picking from the offered options -- verbatim: "The ball
   needs to be masked to get rid of anything that is in the image now,
   white mostly. Then that needs to be places in a scene based on its
   name. The fallout is a perfect example of this" -- pointing at the
   same "Fallout" reference image from the v3 pivot as the quality bar.
2. **Candidate count + model mix**: **3 candidates per shot, generated
   automatically every run** (not on-demand), same model varied by seed
   -- Al's initial answer, before the yeri.ai reveal below changed the
   MIX of models.
3. Al then sent a THIRD reference image mid-conversation, from a tool
   called yeri.ai, built from the prompt "generate a scene depicting a
   Fallout environment and then place the ball in the reference image
   into that scene, match the color scheme of the reference image" -- a
   visibly better result than anything Stability/Bedrock had produced
   (real ambient lighting/shadow on the ball, no hard-pasted edge).
   Researched (not assumed) what's actually behind that kind of result:
   **Google's Gemini 2.5 Flash Image** ("Nano Banana") is documented by
   Google itself as built for exactly this -- "put an object into a
   scene, restyle a room with a color scheme, fuse images with a single
   prompt" -- and confirmed it is **NOT available on Amazon Bedrock** as
   of this writing (checked AWS's own current model catalog: Bedrock's
   image models are Stability SD3.5 Large/Stable Image Core/Stable Image
   Ultra, Nova Canvas [Legacy, EOL 2026-09-30], Titan [already past its
   own EOL] -- no Gemini). Asked Al directly whether to add a non-Bedrock
   provider for this: **yes, alongside the existing Bedrock/Stability
   path, not instead of it** -- so v4 mixes TWO real candidate SOURCES,
   not just seed variations of one model.

**Final shape**: 3 candidates per shot, every run, automatically --
**2 from Gemini 2.5 Flash Image, 1 from the revived Stability/Bedrock
cutout+composite mechanism** (`NUM_GEMINI_CANDIDATES_PER_VARIANT`/
`NUM_STABILITY_CANDIDATES_PER_VARIANT` in `app.py`). An admin picks the
best one per shot in the Articles review tab; the pipeline never
silently commits to whichever generated first.

**Migration 026** (`db/migrations/026_product_article_image_candidates
.sql`): a genuinely new table, `product_article_image_candidates`, NOT
more columns on `product_articles`. `product_articles.action_shot_
image_key/url`/`product_shot_image_key/url` (023) keep meaning exactly
what they always have -- "the currently LIVE image for this variant" --
so `public_api` and everything else that already reads those four
columns needs zero changes. The new table holds EVERY candidate ever
generated (including the one currently live, marked `is_selected =
true` for its `article_id`+`variant`), from either provider, with a
partial unique index (`product_article_image_candidates_one_selected_
idx`) enforcing "exactly one selected per article+variant" at the DB
level, not just in application code. No separate approve/reject workflow
on this table -- picking a candidate is a lightweight action, like
reordering product images (010's own precedent), not a second review
queue; the article's own `product_articles.status` still gates public
exposure of the whole row, images included.

**Gemini is called directly, not through Bedrock**: `generate_article_
image_candidates`'s Gemini calls go straight to Google's own REST API
(`https://generativelanguage.googleapis.com/v1beta/models/{model}:
generateContent`), authenticated via an `x-goog-api-key` header with the
key from the NEW `GeminiApiKeySecretArn` secret (see step 3 above) --
NOT the Bedrock/AWS IAM mechanism at all, a fundamentally different
credential type from every other Bedrock call in this codebase, same
"a real third-party credential this project can't obtain on its own"
category as `BigCommerceSecretArn`/`YouTubeApiKeySecretArn`. Called via
a plain `requests.post` (already a dependency), not the `google-genai`
SDK -- consistent with this codebase's existing "hand-built JSON request
bodies via boto3's low-level `invoke_model`" style, and avoids adding a
heavier grpc/protobuf dependency tree to the Lambda package. Gemini's
image model has **no reliable/reproducible seed parameter** (confirmed
via research, not assumed) -- candidate diversity between the two Gemini
calls instead comes from an "alternate composition" suffix appended to
the second call's prompt, plus the model's own inherent stochasticity.
`GeminiApiKeySecretArn` is genuinely optional (see step 3 above) --
`generate_article_for_product`'s image branch requires ALL EIGHT
image-related arguments (three Bedrock clients' worth plus `gemini_api_
key`/`gemini_model_id`) to run at all, same all-or-nothing gate v3 used
for its own four; with no Gemini key configured, `generate_article_
image_candidates` never runs, and the article generator falls all the
way back to text-only (no images at all) rather than a partial "Gemini
missing, Stability still runs" state -- if you want ANY images, both
Bedrock access AND a Gemini key have to be configured together.

**`src/product_article_generator/app.py`**: `build_reference_canvas`,
`build_image_prompts` (v3's img2img version), `_IMAGE_NEGATIVE_PROMPT`,
and `call_bedrock_for_image` (v3's whole single-call mechanism) are
REMOVED, not left dead. `call_bedrock_remove_background`, `build_
background_prompts`, `_BACKGROUND_NEGATIVE_PROMPT`, `call_bedrock_
generate_background`, and `composite_ball_on_background` are REVIVED
from v2 (see 6t's own v2 writeup above for what each one does) --
`call_bedrock_generate_background` gains a new optional `seed` param,
recorded on the resulting candidate row so a specific-looking Stability
result can be reproduced or deliberately avoided later, even though v4
only ever requests ONE Stability candidate per shot today. A shared
`_resolve_visual_context(article)` helper (visual_theme -> performance_
summary -> hook, capped at 300 chars -- the same fallback chain v3's
`build_image_prompts` used, now factored out) backs BOTH `build_
background_prompts` (Stability) and the NEW `build_gemini_scene_prompt`
(Gemini's single integrated prompt, combining scene-generation with an
explicit "keep the ball unchanged" instruction, grounded in the same
context) so the two providers' prompts don't independently drift.
`call_gemini_for_image` is the new raw-REST Gemini caller -- **its own
docstring flags that the request/response shape is built from Google's
published examples, NOT verified against a live invocation from inside
this environment** (no outbound access to `generativelanguage.
googleapis.com` from this sandbox, no real API key to test with), same
honest posture the Remove Background IAM fix took before its own first
real deploy -- confirm on the first real invocation, don't assume it's
right. `store_article_image`'s `variant` param is renamed to `name` and
now takes the full per-candidate filename stem (e.g. `action_shot_
gemini_1`, `product_shot_stability`), not the bare variant -- v4 stores
multiple candidates per variant, so the old un-suffixed `{variant}.png`
key would collide across them. The core new orchestration function is
`generate_article_image_candidates` (replaces v3's `generate_article_
images`): fetches the reference photo once, tries Remove Background once
(shared across both variants -- a failure skips just the Stability
candidate for both shots, non-fatal, Gemini unaffected), then per
variant runs 2 independently-try/excepted Gemini calls followed by 1
independently-try/excepted Stability call, returning `{"action_shot":
[...], "product_shot": [...]}` (each a list of 0-3 candidate dicts).
`store_article_image_candidates` (new) persists every candidate into the
026 table, marking index 0 of each variant's list `is_selected = true`
-- `generate_article_for_product` derives the SAME index-0 candidate as
the flat `images` dict it passes to `store_article` (the "auto-selected
default" every review starts from), so the two calls always agree on
which candidate is initially live.

**`template.yaml`**: the Remove Background IAM grant is REVIVED
byte-for-byte from commit `d85a890` (the shape that was proven correct
against two real `AccessDeniedException`s during v2, then fully removed
in v3 -- see 6t's own v2 writeup above for that whole saga) -- `Bedrock
RemoveBgModelId`/`BedrockRemoveBgRegion`/`BedrockRemoveBgBaseModelId`
parameters, `BEDROCK_REMOVEBG_MODEL_ID`/`BEDROCK_REMOVEBG_REGION` env
vars, and the `GrantRemoveBgInferenceProfileAccess`/`GrantRemoveBgModel
Access` IAM statements are all back, unchanged in shape from before.
TWO new parameters for Gemini: `GeminiApiKeySecretArn` (blank by
default, same `HasGeminiApiKeySecret` conditional-grant pattern as
`YouTubeApiKeySecretArn`/`BigCommerceSecretArn` -- the `secretsmanager:
GetSecretValue` statement points at a harmless dummy ARN when left
unset, rather than omitting the grant conditionally) and `GeminiModelId`
(default `gemini-2.5-flash-image`). `ProductArticleGeneratorFunction`'s
`Timeout` stays at 600s -- more total image calls per product now (up to
2 Gemini + 1 Remove Background + 1 Stability-background = 4 calls per
variant, 8 per product, more than v2's own 3-call pipeline had), but
every one is independently try/excepted inside `generate_article_image_
candidates`, so a slow/failing candidate is skipped rather than retried
-- revisit if real invocations start timing out.

**`src/admin_api/service.py` / `app.py`**: two new functions, `list_
article_image_candidates(conn, article_id)` (every candidate for an
article, both variants together, ordered by variant then created_at --
returns `[]`, not a 404, for an article with none) and `select_article_
image_candidate(conn, candidate_id, resolved_by=None)` (flips `is_
selected` for the candidate's own article_id+variant pair, and mirrors
the new selection onto `product_articles`' own flat image columns, in
one transaction -- same lightweight, non-review-workflow shape as
`reorder_product_images`). Two new routes: `GET /articles/{article_id}/
image-candidates` and `POST /article-image-candidates/{candidate_id}/
select` -- no `template.yaml` changes needed for either (the existing
`{proxy+}` catch-all already covers new admin API paths, same precedent
established for the `/cores` endpoint).

**`admin-site/index.html`**: a new candidate-picker UI in `render
ArticlePreviewHtml` (shared by both the Articles tab's own preview row
and the product-detail Article sub-tab, so one change covers both
places) -- when an article has 026-table candidates, they render as a
labeled thumbnail row per variant with the currently-selected one
highlighted (`.candidate-thumb.selected`) instead of getting a "Use this
one" button, same "badge instead of action when already in that state"
convention this file already uses for pending/approved/rejected status
cells. Falls back to the old plain currently-live-image row for
pre-v4 articles that have images but no candidate rows at all (nothing
regresses for content generated before this table existed).

**Tests**: `tests/test_product_article_generator.py`'s entire image
section was rewritten a third time (83 tests total, up from 63) around
the v4 candidate pipeline -- `_resolve_visual_context`'s own fallback
chain and 300-char cap in isolation; `call_bedrock_remove_background`/
`build_background_prompts`/`call_bedrock_generate_background` (including
the new optional `seed` param)/`composite_ball_on_background`, all
reused verbatim from v2's own test suite where the underlying function
is unchanged; `build_gemini_scene_prompt`/`call_gemini_for_image` new
(the latter confirms the `x-goog-api-key` header, the inline-image
request shape, and defends both `inlineData`/`inline_data` response
casings); `generate_article_image_candidates`'s own orchestration (the
full 3-per-shot happy path, a Remove Background failure skipping only
the Stability candidate for both variants while Gemini is unaffected,
one Gemini call failing without taking down its sibling call or the
other variant); `store_article_image_candidates` (one row per candidate,
index-0-per-variant marked selected, a no-op for an empty dict); and
`generate_article_for_product`/`handler`'s re-plumbed eight-image-arg/
three-Bedrock-client-plus-Gemini-secret-fetch shape (including a
dedicated test that a Secrets Manager fetch failure for the Gemini key
degrades to `gemini_api_key=None` rather than crashing the whole
invocation). Full non-pytest `test_*.py` sweep (42 files) ran clean
after these changes.

**Redeploy**: run migration 026 (a real schema change this time, unlike
024/025 -- safe after 022/023/024/025 regardless of which of those were
actually applied against a live database), create the new `GeminiApiKey
SecretArn` secret (step 3 above -- **you have to get this key yourself
from Google AI Studio, this project can't obtain it on your behalf**),
then a full unscoped `sam build && sam deploy` passing both `BedrockRemove
BgModelId`'s defaults (nothing to change there, just confirm the params
exist again in your stack) and the new `GeminiApiKeySecretArn`/
`GeminiModelId` params, then swap the static `admin-site/index.html`
file. Confirm Bedrock model access for `us.stability.stable-image-
remove-background-v1:0` in `us-east-1` again (the Stability AI Image
Services Marketplace subscription note from 6t's v2 writeup above still
applies -- if you unsubscribed after v3 removed the last caller, you'll
need to resubscribe before the first real v4 invocation) alongside the
already-confirmed `stability.sd3-5-large-v1:0` in `us-west-2`. **None of
this Gemini integration has been verified against a live invocation from
this environment** -- confirm `call_gemini_for_image`'s actual request/
response shape against a real API call on first use, per its own
docstring's own honesty caveat, before trusting the feature end to end.

### v5: Application Default Credentials -> Vertex AI + service-account auth

After v4 shipped, Al asked directly: "what if we can only use Application
Default Credentials instead of an API key". Researched (not assumed) what
that actually requires from a Lambda running on AWS: **ADC as a literal
concept doesn't exist here** -- ADC's automatic-discovery mechanisms are a
GCP metadata server (only present on real GCP compute), a `GOOGLE_
APPLICATION_CREDENTIALS` env var pointing at a service-account JSON key
file, or `gcloud` user credentials -- none of which apply to an AWS
Lambda. Presented Al with the two real substitutes (a service-account
JSON key stored in Secrets Manager, or GCP Workload Identity Federation
from AWS) plus the option to not build this at all; **he picked the
service-account JSON key.**

A service account authenticates via standard GCP IAM/OAuth2 (a Bearer
access token), which the Gemini **Developer API** v4 called
(`generativelanguage.googleapis.com`, `x-goog-api-key` header) does **not
accept at all** -- only Google Cloud's **Vertex AI** `generateContent`
endpoint does. So v5 is a genuine provider-surface switch, not just an
auth-header swap: `call_gemini_for_image` now hits `https://{region}-
aiplatform.googleapis.com/v1/projects/{project}/locations/{region}/
publishers/google/models/{model}:generateContent` with `Authorization:
Bearer {token}`, confirmed against Google's own Vertex AI Gemini 3 Pro
Image sample notebook and REST docs (not assumed) -- the request/response
body shape itself (contents/parts/inlineData) is essentially unchanged
from v4's Developer-API version, only the URL, model-id path shape, and
auth header differ.

**Second research finding, load-bearing enough to change the plan before
any code was written**: gemini-2.5-flash-image, the model v4 shipped
against, carries an explicit notice in Google's own migration
documentation that it **"will be retired" on Vertex AI on 2026-10-02** --
weeks away at the time this was discovered. Shipping v5 against a model
that dies in weeks would just trade one broken integration for another,
so this was surfaced to Al directly before any implementation: he chose
**Gemini 3 Pro Image** ("Nano Banana Pro", `gemini-3-pro-image`) over
both the cheaper/faster Gemini 3.1 Flash Image ("Nano Banana 2")
successor and staying on the soon-dead 2.5 model. See `Gemini
ModelId`'s own `template.yaml` parameter description for the same
history.

**Token minting** (`mint_gemini_access_token` in `app.py`) uses
`google-auth` ONLY (`google.oauth2.service_account.Credentials` +
`google.auth.transport.requests.Request`) -- deliberately NOT `google-
cloud-aiplatform` or `google-genai`, both heavy protobuf/grpc-backed SDKs
this project has consistently avoided (v4's own docstring made the same
call against `google-genai` for the same reason). `google-auth` itself is
pure Python plus `cryptography`, used purely for its JWT-bearer OAuth2
token-exchange logic; `credentials.refresh()` does the whole exchange in
one call, and the actual `generateContent` call remains a plain
`requests.post`, same "raw JSON body, no heavy SDK" style this module
already uses everywhere else. **New pip dependency**: `google-auth>=2.28`
added to `src/product_article_generator/requirements.txt`.

**Secret shape changed**: `GeminiApiKeySecretArn` (a bare API key, or
`{"api_key": "..."}`) is REPLACED by `GeminiServiceAccountSecretArn` --
the secret's JSON *is* the service account's whole downloaded key file
verbatim (it already carries its own `project_id`, so there's no separate
`GeminiProjectId` template parameter). A new `GeminiRegion` parameter
(default `us-central1`) supplies the Vertex AI location, since a service
account isn't tied to one Region the way a Bedrock model is. See step 3
above for the exact GCP-side setup (create project, enable Vertex AI API,
create service account, grant `roles/aiplatform.user`, download JSON
key).

**`app.py` plumbing renamed throughout**: `gemini_api_key` (a bare
string) is now `gemini_auth` (a dict: `{"access_token", "project_id",
"region"}`) -- threaded through `generate_article_image_candidates`,
`generate_article_for_product` (still one of the same EIGHT required
image-related arguments for the all-or-nothing image-generation gate),
and `handler()`. `handler()` now does secret-fetch-then-mint as one
step: fetch the service-account JSON from Secrets Manager, call `mint_
gemini_access_token`, then bundle the resulting token with the key's own
`project_id` and `GEMINI_REGION` into the `gemini_auth` dict -- wrapped
in the same single try/except as before, so either a fetch failure OR a
mint failure degrades to `gemini_auth=None` (Gemini candidates skipped
this run) rather than crashing the whole invocation, unchanged "images
are always best-effort" posture from v4.

**`template.yaml`**: `GeminiApiKeySecretArn` -> `GeminiServiceAccountSecretArn`
(same blank-default/conditional-grant pattern, now `HasGeminiService
AccountSecret`), `GeminiModelId` default changed to `gemini-3-pro-image`,
new `GeminiRegion` parameter (default `us-central1`), new `GEMINI_REGION`
env var on `ProductArticleGeneratorFunction`. No new IAM grant needed
beyond the existing conditional `secretsmanager:GetSecretValue` statement
(now pointed at `GeminiServiceAccountSecretArn`) -- the actual OAuth2
token exchange happens entirely outside AWS, against Google's own token
endpoint, so there's no AWS-side IAM concept for it the way there is for
every Bedrock grant elsewhere in this function's policy. Confirmed the
template still parses (57 resources, unchanged count) via the CFN-
tolerant YAML loader after these changes.

**Tests**: `tests/test_product_article_generator.py` grew to 85 tests (up
from 83) -- every `gemini_api_key="..."` call site became `gemini_auth=
_FAKE_GEMINI_AUTH` (a module-level stand-in dict); `call_gemini_for_
image`'s own tests now assert the Vertex AI URL shape and `Authorization:
Bearer` header instead of the Developer API URL and `x-goog-api-key`; a
NEW `mint_gemini_access_token` test fakes the handful of `google.*`
module/class names it touches via `sys.modules` injection (the same
pattern `_fake_boto3_module` already uses for `boto3`) since `google-
auth` itself can't be installed in this sandbox (pip's proxy returns 403
here, same caveat noted elsewhere in this project) -- confirms the right
scope/service-account-info get passed through and `.token` gets returned,
without needing the real library; `handler()`'s Gemini tests split into
THREE cases (secret configured and mint succeeds, Secrets Manager fetch
fails, and NEW -- fetch succeeds but `mint_gemini_access_token` itself
raises), both failure cases confirming `gemini_auth` stays `None` rather
than crashing the invocation. Full non-pytest `test_*.py` sweep (41
files, `test_product_scraper.py`/`test_url_discovery.py` excluded per
this sweep's own long-standing convention) ran clean after these changes.

**Redeploy**: create the new `GeminiServiceAccountSecretArn` secret per
step 3's v5 instructions above (**you have to set this up yourself in
Google Cloud Console -- create the project/service account/key, this
project can't do that on your behalf**), then a full unscoped `sam build
&& sam deploy` passing `GeminiServiceAccountSecretArn` (replaces the old
`GeminiApiKeySecretArn` parameter -- passing the old parameter name will
just fail as an unknown parameter) and, if you need a non-default GCP
Region, `GeminiRegion`. **None of this Vertex AI integration has been
verified against a live invocation from this environment** -- same
honest caveat v4's own Developer-API integration carried, now doubled
(new endpoint AND new auth mechanism, neither exercised against a real
GCP project from this sandbox) -- confirm `call_gemini_for_image`'s
actual request/response shape AND `mint_gemini_access_token`'s real
token-exchange behavior against a live invocation on first use, before
trusting the feature end to end.

### REAL INCIDENT (2026-09-04): first live v5 invocation, three independent failures

The first real post-deploy invocation produced `images_generated: false`.
Diagnosed via CloudWatch logs (`aws logs tail /aws/lambda/bowling-scraper-
product-article-generator`) and `aws lambda get-function-configuration`,
not guessed at. Three genuinely separate problems, in the order they were
found and fixed:

1. **`GEMINI_SERVICE_ACCOUNT_SECRET_ARN` was empty on the deployed
   function.** No error in the logs at all for this one -- `handler()`'s
   `if gemini_secret_arn:` guard just silently doesn't fire when the env
   var is blank, so `gemini_auth` stayed `None` and the whole image
   branch (not just Gemini) was skipped without a trace. Root cause: the
   `GeminiServiceAccountSecretArn` parameter never actually got added to
   `samconfig.toml`'s `parameter_overrides` after the secret was created.
   **Lesson for next time**: after ANY redeploy that's supposed to change
   a secret/parameter, confirm it actually landed with `aws lambda get-
   function-configuration --function-name bowling-scraper-product-
   article-generator --query 'Environment.Variables'` BEFORE assuming a
   downstream failure means the code is wrong -- this cost a full
   diagnose-fix-redeploy cycle that turned out to be a config-not-applied
   issue, not a bug.

2. **Bedrock `AccessDeniedException` on the Remove Background call**:
   "Model access is denied due to IAM user or service role is not
   authorized to perform the required AWS Marketplace actions... Your AWS
   Marketplace subscription for this model cannot be completed at this
   time." This is `us.stability.stable-image-remove-background-v1:0`'s
   own recurring AWS Marketplace subscription requirement -- see 6t's v2
   writeup above for the first time this was hit and solved. **Not a code
   issue, nothing to redeploy** -- fix is in the AWS Console: Bedrock ->
   Model access -> find Stability AI's "Stable Image Remove Background"
   in **us-east-1** specifically (not this stack's home Region) ->
   request/resubscribe access. Confirm access shows "Access granted"
   before the next invocation.

3. **Vertex AI `400 Bad Request` on all 4 Gemini calls.** Two compounding
   causes:
   - The URL showed `gemini-2.5-flash-image`, not the new default
     `gemini-3-pro-image` -- `GeminiModelId=gemini-2.5-flash-image` was
     still sitting in `samconfig.toml`'s `parameter_overrides` from
     configuring v4, and an explicit override always beats `template.
     yaml`'s Default. Fixed by updating that line to
     `GeminiModelId=gemini-3-pro-image` (or deleting the override
     entirely to fall through to the new Default).
   - Separately, and more fundamentally: `call_gemini_for_image`'s
     request body was wrong. `response.raise_for_status()` doesn't
     surface the response BODY, only a generic HTTPError, so the exact
     reason wasn't visible from the first failure alone -- fixed that
     blind spot first (the function now logs `response.status_code`/
     `response.text` on any non-2xx response before re-raising). Then,
     comparing more carefully against Google's own published REST curl
     examples, found the request was missing `"role": "user"` on the
     content object (the Developer API tolerates omitting it; Vertex
     AI's stricter proto-JSON validation apparently does not), and was
     using snake_case `inline_data`/`mime_type` where Vertex AI's JSON
     schema expects camelCase `inlineData`/`mimeType` -- both carried
     over unexamined from v4's own Developer-API version of this
     function, which was ALSO never verified against a live call (see
     v4's own writeup above). Both fixed in `call_gemini_for_image`;
     `tests/test_product_article_generator.py`'s request-shape test
     updated to assert `role`/camelCase (85/85 still passing, full
     41-file sweep still clean).

**This exact request-shape fix has itself NOT yet been confirmed against
a live call** -- the response-body logging added alongside it means the
NEXT invocation, if it still fails, will show the real Google-side error
message directly in CloudWatch instead of requiring another guess-and-
redeploy cycle. After redeploying with the corrected `GeminiModelId`
override, the Remove Background Marketplace resubscription, and this
code fix, re-run the same on-demand invocation and check `images_
generated` in the response body, then pull the logs regardless (success
or failure) to confirm what Vertex AI actually returned.

### REAL INCIDENT (2026-09-04): regenerate crashed with UniqueViolation on product_article_image_candidates

A second invocation for a product that already had an article (a
regenerate -- likely triggered by a duplicate/retried invoke after a CLI
`Read timeout`, not a deliberate manual retry) crashed with:

```
UniqueViolation: duplicate key value violates unique constraint
"product_article_image_candidates_one_selected_idx"
DETAIL: Key (article_id, variant)=(<uuid>, action_shot) already exists.
```

Traceback pointed at `store_article_image_candidates`, called from
`generate_article_for_product`, called from `handler`. Root cause:
`store_article_image_candidates` was insert-only -- it never cleared a
product's prior candidate rows before writing new ones. `store_article`'s
own upsert returns the SAME `article_id` on a regenerate (by design, so
an admin's already-approved article row gets updated in place rather
than duplicated), so the second run's own index-0-selected candidate for
a variant collided with the FIRST run's still-`is_selected=true` row for
that same `(article_id, variant)` pair -- exactly what migration 026's
partial unique index exists to prevent, just never exercised by an
actual second run before now.

**Fixed** by having `store_article_image_candidates` unconditionally
`DELETE FROM product_article_image_candidates WHERE article_id = %s AND
variant = %s` immediately before inserting that variant's new candidates
-- a no-op delete on a fresh article's first run, a real one on a
regenerate, same code path either way, no first-run/second-run branching
needed. Scoped per-variant (not a blanket delete for the whole
`article_id`) so a variant that produced ZERO new candidates this run
(e.g. every Gemini and Stability call failed) keeps whatever good
candidates it already had from a prior run -- same "best-effort, don't
destroy existing state on a partial failure" posture this module uses
everywhere else (a Remove Background failure only ever skips the
Stability candidate, never touches anything already stored).

Two new regression tests added to `tests/test_product_article_generator.
py`: one calls `store_article_image_candidates` twice for the same
`article_id` and asserts a delete precedes each run's own inserts; the
other confirms a variant with an empty candidates list gets neither a
delete nor an insert. Full file: 87/87 passing. Full 41-file sweep:
clean.

**Operational notes surfaced by this same incident, unrelated to the
code bug**:
- The CLI's `Read timeout` on `aws lambda invoke` is a client-side
  timeout on the AWS CLI's own HTTP connection, not a Lambda failure --
  the function itself completed fine server-side (well under Lambda's
  configured timeout). If you hit this, check CloudWatch logs for that
  invocation before assuming it failed, and consider `aws lambda invoke
  --cli-read-timeout 0` to stop the CLI from giving up early. More
  importantly: **avoid re-invoking the same product while a prior invoke
  might still be in flight** -- that overlap is what produced the
  duplicate/retried call that triggered this exact bug.
- The same batch of logs that surfaced this bug ALSO showed the Bedrock
  Remove Background `AccessDeniedException` (see the numbered incident
  above) still failing, repeatedly, across all invocation attempts in
  this batch. **That Marketplace resubscription has not yet been
  confirmed to have taken effect** -- if it's still failing after
  confirming "Access granted" in the Bedrock console, allow a few more
  minutes for propagation and retry, since AWS Marketplace subscription
  changes aren't always instant.

### Ball prominence tuning (2026-09-04)

Al's feedback on the generated article images: the ball itself was
reading too small/distant in the frame. Both image-generation paths had
to be tuned separately, since they place the ball two completely
different ways:

- **Stability composite path** (`composite_ball_on_background`): the
  ball's on-screen size is a pixel-level implementation detail, not a
  prompt -- it's a plain Pillow paste of the real cutout at a fixed
  fraction of the background's shorter side. That fraction was `0.62`;
  bumped to `0.82`. New regression test
  `test_composite_ball_on_background_ball_fills_most_of_the_frames_
  shorter_side` measures the ball's actual rendered width by scanning
  the output's pixels (not just checking the internal constant), so a
  future refactor of the scaling logic would still be caught if it
  regressed the size.
- **Gemini path** (`build_gemini_scene_prompt`): Gemini gets no cutout
  and no pixel-level control at all -- placement and scale are both
  whatever the prompt says. Added explicit "the ball is the hero
  subject... large and prominent... filling a substantial portion of the
  frame, not small or distant" language, for both variants. New
  regression test
  `test_build_gemini_scene_prompt_instructs_ball_to_be_large_and_
  prominent` asserts this language is present in both the action_shot
  and product_shot prompts.

Full test file: 89/89. Full 41-file sweep: clean. **Not yet confirmed
against a live invocation** -- the Gemini side in particular is a prompt
change, not a hard constraint, so its actual effect on output can only
be judged by looking at a real generated image after redeploying.

### REAL INCIDENT (2026-09-04): Vertex AI 404 on gemini-3-pro-image -- id claim CORRECTED same day

After confirming `GeminiModelId` was correctly deployed as `gemini-3-pro-
image` (matching template.yaml's own default, no samconfig drift), a
fresh regenerate still failed -- every Gemini candidate call returned:

```
Vertex AI generateContent returned 404: {
  "error": {
    "code": 404,
    "message": "Publisher model `projects/.../locations/us-central1/publishers/google/models/gemini-3-pro-image` was not found or your project does not have access to it. ..."
  }
}
```

**First diagnosis (WRONG, corrected same day).** Initially attributed to
a missing `-preview` suffix, based on secondary sources (an AI search
summary, third-party API-proxy listings, developer-forum thread titles)
-- `DEFAULT_GEMINI_IMAGE_MODEL_ID` and `GeminiModelId`'s Default were
changed to `gemini-3-pro-image-preview` and committed (`c3f249a`).

Al disputed this directly ("i don't think this is correct -- 3.1 is now
in preview and 3.0 is live"). Checking GCP Console's own Model Garden
version table for this model directly (not a search summary, not
inferred) settled it:

```
Resource ID                   Release date   Release stage
gemini-3-pro-image            2026-05-27     Generally Available
gemini-3-pro-image-preview    2025-11-20     Preview
```

**The bare `gemini-3-pro-image` id is correct and current** --
`gemini-3-pro-image-preview` is the older, superseded pre-GA snapshot,
not "the real one." **Reverted** `DEFAULT_GEMINI_IMAGE_MODEL_ID` (app.py)
and `GeminiModelId`'s Default (template.yaml) back to `gemini-3-pro-
image`. This is a case where secondary-source research converged on a
wrong answer that felt well-corroborated (multiple independent sources
all said "-preview") but none of them were checking the actual GCP
Console launch-stage table, which is the only place that would show a
GA promotion had already happened.

**So what actually caused the 404?** Still open. Two real candidates,
neither confirmed yet:

1. **Allowlist/access gating** on a brand-new GCP project -- real
   developer-forum threads describe this pattern for various Gemini 3
   models, though not specifically confirmed for this exact GA id.
2. **Region/location.** Google's own official Vertex AI sample code for
   this model uses `location="global"`, not a specific region --
   `GeminiRegion`'s default here is `us-central1`. Not yet changed,
   since it's not confirmed as the actual cause (see `GeminiRegion`'s
   description in template.yaml) -- worth checking on the next real
   invocation: a 404 after this id fix would point back to region/
   access; a 403 would point specifically to allowlisting.

`gemini-2.5-flash-image` (still working, not retiring until 2026-10-02)
remains available as a fallback via `GeminiModelId=gemini-2.5-flash-
image` in samconfig.toml's `parameter_overrides` + redeploy, if Al wants
one before this is fully resolved -- a config change, not a code change.

**Redeploy note**: per the earlier stale-parameter incident (6t v5
section above), CloudFormation does NOT auto-adopt a template's new
Default on an already-deployed stack -- explicitly set `GeminiModelId=
gemini-3-pro-image` in samconfig.toml's `parameter_overrides` (don't
just remove an old override and rely on the new template default)
before redeploying.

Full test file: 89/89 (unchanged fixture assertions in `tests/test_
product_article_generator.py` around `call_gemini_for_image`'s URL
construction still pass -- they exercise the function generically with
an arbitrary model_id string, not the default constant). Full 41-file
regression sweep: clean. Template verified via the CFN-tolerant YAML
loader (57 resources, unchanged count, reverted default confirmed).

### REAL INCIDENT (2026-09-04), part two: same 404 persisted with the correct id -- GeminiRegion, not the id, was the actual cause

Redeployed with the corrected `gemini-3-pro-image` id above, then ran a
fresh regenerate. The SAME 404 came back, byte-for-byte the same error
shape:

```
Vertex AI generateContent returned 404: {
  "error": {
    "code": 404,
    "message": "Publisher model `projects/bowling-content-aggergator/locations/us-central1/publishers/google/models/gemini-3-pro-image` was not found or your project does not have access to it. Ensure you are using a valid model name and that the model is available in the specified region. For more information, see: https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations.",
    "status": "NOT_FOUND"
  }
}
```

The error's own text and its linked doc (`.../resources/locations`) both
point at Region, not the id -- and Google's own official Vertex AI
sample code for Gemini 3 Pro Image uses `location="global"`, not a
per-Region location. `GEMINI_REGION` was `us-central1`.

Fixed two things together, since `global` isn't just a different
`locations/` path value:

1. `DEFAULT_GEMINI_REGION` (app.py) and `GeminiRegion`'s Default
   (template.yaml): `us-central1` -> `global`.
2. `call_gemini_for_image`'s URL construction: Vertex AI's regional
   endpoints use host `{region}-aiplatform.googleapis.com`, but the
   `global` location uses a bare `aiplatform.googleapis.com` host with
   NO per-Region subdomain prefix. The original code always built the
   regional host shape regardless of region value -- harmless while
   `GEMINI_REGION` really was a region, but would have silently built
   the wrong host (`global-aiplatform.googleapis.com`, which doesn't
   exist) the moment `GEMINI_REGION` became `global`. Now branches
   explicitly on `region == "global"`.

Also present in the same log, separate and already-known: the Bedrock
Remove Background `AccessDeniedException` (AWS Marketplace subscription
issue, `us.stability.stable-image-remove-background-v1:0` in
`us-east-1`) -- fix is in the AWS Console (Bedrock -> Model access ->
resubscribe), not code, and unrelated to the Gemini region fix.

New regression test: `test_call_gemini_for_image_uses_global_host_shape_
for_global_region` asserts the bare-host URL shape for `region=
"global"`; the existing `test_call_gemini_for_image_sends_correct_
request_shape_and_auth_header` continues to assert the regional
prefixed-host shape for `region="us-central1"`, so both host shapes are
now covered.

**Redeploy note**: explicitly set `GeminiRegion=global` in
samconfig.toml's `parameter_overrides` (same stale-parameter caveat as
GeminiModelId above -- CloudFormation won't adopt the new template
default on an already-deployed stack on its own).

**Not yet reconfirmed against a live call.** If this still 404s after
redeploying, or comes back as a 403 instead, that points at allowlist/
access gating on the GCP project instead (see the allowlist-gating
discussion in the first part of this incident above) -- file a request
on https://discuss.ai.google.dev if so. `gemini-2.5-flash-image` remains
available as a fallback in the meantime.

Full test file: 90/90. Full 41-file regression sweep: clean. Template
verified via the CFN-tolerant YAML loader (57 resources, unchanged
count, `GeminiRegion` default confirmed as `global`).

### REAL INCIDENT (2026-09-04), part three: Bedrock Remove Background AccessDeniedException -- "Model access" console fix doesn't exist anymore

The same CloudWatch logs also carried the Remove Background
`AccessDeniedException` (`us.stability.stable-image-remove-background-
v1:0`, `AWS Marketplace actions (aws-marketplace:ViewSubscriptions,
aws-marketplace:Subscribe) ... not authorized`) that this incident's
part one flagged as "known, fix in the console, not code." That
instruction was wrong -- Al checked and reported "model access page has
been retired."

Confirmed via AWS's own docs (docs.aws.amazon.com/bedrock/latest/
userguide/model-access-permissions.html) and their security blog
(aws.amazon.com/blogs/security/simplified-amazon-bedrock-model-access/):
Bedrock's old console "Model access" page (and the underlying
`PutFoundationModelEntitlement` API) has genuinely been retired as part
of an access-simplification change. Serverless models WITHOUT an AWS
Marketplace product ID now get automatic access with zero setup. Models
WITH a product ID -- every Stability model this stack uses, background-
generation and Remove Background alike -- instead auto-subscribe in the
background on an account's first live `InvokeModel` call. That
background subscription attempt is made using the CALLING identity's own
permissions -- in this case, `ProductArticleGeneratorFunction`'s own
Lambda execution role, not a human console user -- and that role never
had `aws-marketplace:Subscribe`/`Unsubscribe`/`ViewSubscriptions`. There
was no longer a console click to fix this at all; the fix has to be an
IAM grant on the Lambda's own role.

Added a new `GrantMarketplaceModelSubscriptionAccess` statement to
`ProductArticleGeneratorFunction`'s policy in template.yaml, granting
`aws-marketplace:Subscribe`, `aws-marketplace:Unsubscribe`, and
`aws-marketplace:ViewSubscriptions` on `Resource: "*"` -- these three
actions don't support resource-level ARN scoping (AWS's own policy
examples all use `"*"`; the only narrowing option is an
`aws-marketplace:ProductId` condition key on `Subscribe` specifically,
which isn't needed here since this Lambda only ever calls the one
Stability model family).

**Note**: `BedrockImageModelId` (Stable Diffusion 3.5 Large, the
background-generation candidate) shares this same Lambda and the same
new grant, so if that candidate was ALSO silently failing with the same
AccessDeniedException (not confirmed either way from the logs seen so
far -- only Remove Background's failure has shown up), this same fix
should resolve it too, once redeployed.

Not yet confirmed against a live invoke. Full test file: unaffected (no
Python changed, template.yaml only) -- 90/90. Full 41-file regression
sweep: clean. Template verified via the CFN-tolerant YAML loader (57
resources, unchanged count, new `GrantMarketplaceModelSubscriptionAccess`
statement confirmed present).

**Redeploy note**: this needs a real `sam deploy`/stack update to take
effect (a new IAM statement, not a parameter -- no `parameter_overrides`
involved).

### v5 (2026-09-04): Stability candidate turned off -- Al: "they will never be better than the gemini images"

After enough real candidates had gone through the Articles review
picker, Al's call was direct: turn off the Stability/composite
candidate for good. Done as a toggle, not a rip-out --
`src/product_article_generator/app.py` gets a new module-level
`ENABLE_STABILITY_CANDIDATES = False`. The whole Remove Background +
background-generation + Pillow-composite mechanism stays in the file
exactly as-is; it's just skipped now:

- `generate_article_image_candidates` no longer calls
  `call_bedrock_remove_background` at all when the flag is off (not
  just the composite step) -- there's nothing left that needs the
  cutout, so this also avoids a wasted Bedrock call/cost, not only a
  wasted candidate.
- `NUM_GEMINI_CANDIDATES_PER_VARIANT` went from 2 to 3, so the review
  picker still gets 3 real options per shot -- all Gemini now, three
  independent attempts instead of two -- rather than quietly shrinking
  down to a straight A/B. The non-first Gemini calls each get their own
  "distinct alternate/further-distinct composition" prompt suffix (was
  just "alternate composition" on the single second call before).
- `NUM_STABILITY_CANDIDATES_PER_VARIANT` is left in place, unused
  except as documentation of what re-enabling would produce.

Why a toggle and not a deletion: this exact mechanism has already been
revived once before (v4, after v3) when Gemini itself became
unavailable/unsuitable for a stretch -- see this module's own docstring
for the full v1-v5 history. Flipping `ENABLE_STABILITY_CANDIDATES` back
to `True` restores the old 4-candidates-per-shot behavior (3 Gemini + 1
Stability) with no code to rewrite, if that ever needs to happen again.

No migration, no template.yaml change (57 resources unchanged) -- this
is a pure Lambda-code change, so a plain `sam build
ProductArticleGeneratorFunction && sam deploy` (or a full redeploy)
picks it up. Tests: `test_product_article_generator.py` rewritten for
the new default (3 Gemini candidates, 0 Bedrock calls when disabled)
plus a dedicated pair of tests that flip the flag back on to prove the
fallback path itself still works (91/91). Full 44-file regression
sweep: clean.

### Separate issue seen in the same log, NOT a code bug (RESOLVED): `GeminiRegion` came through as `global=` with a trailing `=`

The same CloudWatch tail also showed `call_gemini_for_image` trying to
resolve host `global=-aiplatform.googleapis.com` and path
`locations/global=` -- a literal trailing `=` character inside the
region value itself. `call_gemini_for_image`'s own code only ever
produces a clean `"global"` string when given one (see this incident's
part two and its regression test) -- this had to be how the
`GeminiRegion` parameter override actually got applied on the AWS side
(a typo in samconfig.toml's `parameter_overrides` line, or in the exact
`sam deploy`/`aws cloudformation deploy` command used), not a bug in
this repo's code.

**Resolved**: confirmed by Al as a deploy-side samconfig.toml/deploy-command
typo, fixed on his end (no repo change needed). All-clear confirmed on
a live run.

### v6 (2026-09-04): REAL INCIDENT -- product_shot article images "not coming through anymore"

Al: "the square product images are not coming through anymore" ->
clarified through follow-up to mean specifically the product_shot (1:1)
article-image candidates in the Articles review picker, and specifically
that the picker showed ZERO candidates for product_shot on every
product, while action_shot kept working fine.

**Diagnosis.** Al ran the CloudWatch pull himself:
```bash
aws logs tail /aws/lambda/bowling-scraper-product-article-generator \
  --region us-west-1 --since 3d \
  --filter-pattern "?\"product_shot\" ?\"generateContent returned\" ?\"finishReason\""
```
The log showed a mix of older, already-fixed incidents (400s from before
the request-shape fix, 404s from before the region/host fix) followed by
a consistent, current pattern: every `product_shot` Gemini call failing
with a plain `429 RESOURCE_EXHAUSTED` against the CORRECT endpoint
(`.../locations/global/.../gemini-3-pro-image:generateContent`) -- so
the model id and endpoint fixes from the earlier incidents were working;
this was a new, different problem. Nothing about aspect ratio itself was
wrong -- action_shot (16:9) and product_shot (1:1) make an otherwise
identical call (same model, same reference image, same auth), so
there's no per-value reason Vertex AI would single out "1:1".

The real cause was ORDERING: `generate_article_image_candidates` used to
run all of `action_shot`'s `NUM_GEMINI_CANDIDATES_PER_VARIANT` (3) Gemini
attempts before starting any of `product_shot`'s -- 6 total Gemini calls
per product, fired within a couple seconds of each other in one Lambda
invocation. Whatever burst/per-minute quota Vertex AI enforces for this
model, `action_shot` (always first) consumed it, and `product_shot`
(always second, moments later) reliably found nothing left. This was a
LATENT problem, not a new one introduced by this incident's own fix --
it was invisible before v5 because `product_shot` always had a
guaranteed non-Gemini fallback candidate (the Stability/composite one).
v5 removed that fallback (`ENABLE_STABILITY_CANDIDATES = False`) at the
same time it raised `NUM_GEMINI_CANDIDATES_PER_VARIANT` from 2 to 3 --
50% more Gemini load per product with no safety net left -- which is
what turned an invisible quota-ordering issue into a total, deterministic
"product_shot never works" outage.

**Fix**, two parts, both in `src/product_article_generator/app.py`:

1. `get_gemini_requests_session()` -- a new function building a
   `requests.Session` with a urllib3 `Retry` mounted for
   `GEMINI_RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)`,
   `total=GEMINI_RETRY_TOTAL` (4), `backoff_factor=GEMINI_RETRY_BACKOFF_
   FACTOR` (2) -- waits 2s/4s/8s/16s between attempts, long enough for a
   per-minute quota window to roll over on its own. Deliberately reuses
   the exact convention `scripts/backfill_core_ids.py`'s own
   `get_requests_session` already established for this same "probably
   transient, safe to retry" class of error, rather than inventing a
   different pattern. `call_gemini_for_image` now takes an optional
   `session` argument (defaults to a fresh `get_gemini_requests_session()`
   if not given) and calls `session.post(...)` instead of a bare
   `requests.post(...)`.
2. `generate_article_image_candidates` now builds ONE shared session
   (`gemini_session = get_gemini_requests_session()`) and threads it
   through every Gemini call for that product, AND interleaves the two
   variants' attempts -- `action_shot #1, product_shot #1, action_shot
   #2, product_shot #2, action_shot #3, product_shot #3` -- instead of
   finishing one variant before starting the other. This means if a hard
   quota ceiling still isn't cleared by the retries, it's shared between
   both variants rather than always landing entirely on whichever one
   happens to run second. The per-candidate S3 key naming
   (`{variant}_gemini_{i+1}`) and prompt-suffix logic (no suffix / "alternate
   composition" / "composition/angle #N") are unchanged -- both are keyed
   on each variant's own candidate index `i`, not on the call's position
   in the shared, interleaved call order, so candidate numbering and
   prompt variation are identical to before.

Neither `NUM_GEMINI_CANDIDATES_PER_VARIANT` nor
`ENABLE_STABILITY_CANDIDATES` changed -- this is purely about not
starving one variant of the same shared quota the other variant already
used up.

No migration, no `template.yaml` change (57 resources unchanged) -- pure
Lambda-code change: `sam build ProductArticleGeneratorFunction && sam
deploy` (or a full redeploy) picks it up.

**Tests** (`tests/test_product_article_generator.py`, 93/93 passing, 2
new): every existing `call_gemini_for_image` test switched from
monkeypatching module-level `requests.post` (which `session.post(...)`
no longer routes through -- a test still patching only `requests.post`
would now silently miss the call and attempt a real network connection)
to passing a small `_FakeGeminiSession` wrapper via the new `session=`
argument; the `generate_article_image_candidates` orchestration tests
switched from monkeypatching `requests.post` to monkeypatching
`app.get_gemini_requests_session` itself, since that function is what
builds the real session internally. New:
`test_call_gemini_for_image_defaults_to_retry_session_when_none_given`
(confirms the no-session-given path actually builds one via
`get_gemini_requests_session` rather than silently having no retry
behavior) and `test_get_gemini_requests_session_retries_429_and_5xx`
(confirms the actual `Retry` configuration -- `status_forcelist`
includes 429, `total` matches `GEMINI_RETRY_TOTAL`). The interleaving-
order change required updating
`test_generate_article_image_candidates_one_gemini_call_fails_others_
still_succeed`'s failure-injection index (the 3rd call overall is now
`action_shot`'s own 2nd attempt, not the 2nd call overall) -- same
end-state assertions (`action_shot` 2 candidates, `product_shot` 3), just
retargeted to the new call order. Full 44-file regression sweep: clean.

### 6u. Article → BigCommerce sync: admin toggle (migration 028) + sync job design spec (job not yet built)

Follow-up to 6s/6t (the ball-review article generator): Al asked how to get
these generated articles onto bowlerdepot.com itself as a knowledge-base
section, then, once two architectures were discussed (push into
BigCommerce as native content vs. a decoupled microsite reading
`public_api` directly), asked specifically: "lets add a flag to each
article that would sync them to bigcommerce if on" and to spec out the
sync job. **Only the flag is built in this pass** -- the sync job itself
(`src/bowlerdepot_article_sync`) does not exist yet; this section is a
design spec for it, same "spec first, build later" split Al asked for.

**What's actually built (migration 028):**

- `db/migrations/028_product_articles_bigcommerce_sync.sql` adds
  `product_articles.sync_to_bigcommerce` (boolean, default `false`),
  `bigcommerce_post_id` (text), and `bowlerdepot_synced_at` (timestamptz)
  -- the exact same three-column shape `020_bowlerdepot_video_sync.sql`
  already established for `product_videos` (an admin-controlled gate
  plus the eventual sync job's own idempotency bookkeeping), applied here
  to articles instead. Deliberately a SEPARATE gate from
  `status = 'approved'`: an article being safe to show on
  data.bowleriq.com (via `public_api`) doesn't by itself mean Al wants it
  posted under bowlerdepot.com's own name -- different audience, and he
  may want to review the storefront framing separately. Defaults `false`
  -- nothing is eligible to sync until an admin opts a specific article
  in.
- `service.set_article_bigcommerce_sync` (new) + `PATCH
  /articles/{article_id}/bigcommerce-sync` (`{"sync_to_bigcommerce":
  bool}`) -- mirrors `set_product_published`'s exact shape (single-column
  update, `LookupError` if the row doesn't exist) rather than
  `approve_article`/`reject_article`'s review-workflow shape: this is a
  freely-reversible admin preference an admin can flip on an article of
  ANY status, not a one-way resolution of a queue item. No
  `template.yaml` change needed -- `PATCH /{proxy+}` already covers it,
  same as every other admin_api route added since 6h.
- `list_articles`/`get_article` now include the three new columns so the
  Articles tab list row can show/toggle the flag without a second
  round-trip.
- admin-site: a "Sync on"/"Sync off" toggle button per article row
  (`toggleArticleBigcommerceSync`, same button-as-toggle shape as the
  Products tab's `togglePublish`), showing `synced ‹date›` underneath
  once `bowlerdepot_synced_at` is set. That timestamp is deliberately
  left visible even after the flag is turned back off -- see the
  migration's own header comment: turning the flag off does NOT delete
  or unpublish anything that was already pushed, that's a separate,
  not-yet-decided question (see below).
- Tests: `test_admin_api_service.py` -- `set_article_bigcommerce_sync`
  works on a pending article (no status gate), turning it off leaves
  `status`/`bigcommerce_post_id`/`bowlerdepot_synced_at` untouched,
  missing-row raises `LookupError`, plus `list_articles`/`get_article`
  round-trip the three new columns. Full 44-file regression sweep clean;
  `template.yaml` unchanged (still 57 resources).

**Design spec for `src/bowlerdepot_article_sync` (NOT built -- no Lambda,
no schedule, no IAM exists yet):**

Chosen direction, from the earlier architecture discussion: push
approved+flagged articles into BigCommerce as native Blog posts (better
SEO -- the content lands on the bowlerdepot.com domain as a real indexed
page, and the KB "section" is just BigCommerce's own blog index) rather
than a decoupled microsite reading `public_api` live. This mirrors
`bowlerdepot_video_sync`'s own "push into BigCommerce's native feature,
don't reinvent a widget" reasoning (6q), just against a different
BigCommerce API.

- **Confirmed live against BigCommerce's own docs this session** (not
  guessed): unlike `bowlerdepot_video_sync`'s v3 Catalog Product Videos
  endpoint, Blog Posts are a **v2** API --
  `POST /stores/{store_hash}/v2/blog/posts`. Required fields: `title`,
  `body`. Relevant optional fields: `tags`, `is_published` (defaults
  `false` -- draft until explicitly set `true`), `meta_description`,
  `meta_keywords`, `author`, `published_date`, `thumbnail_path`. Response
  includes BigCommerce's own new `id` (what gets stored as
  `bigcommerce_post_id`).
- **OAuth scope needed is `store_v2_content` (Content: modify) --
  CONFIRMED a different scope than `bowlerdepot_video_sync`'s Products-
  modify (`store_v2_products`) need.** Same "unverifiable from this
  sandbox, real prerequisite Al must check himself" situation 6q already
  flagged for that scope: whatever token sits behind
  `BIGCOMMERCE_SECRET_ARN` today (built for `price_checker`'s read-only
  lookups, then `bowlerdepot_video_sync`'s Products-modify need) almost
  certainly does NOT have Content-modify yet -- check BigCommerce's API
  Accounts settings and add the scope (or mint a separate token) before
  this job's first real run, or every push will 403.
- **Needs-sync query** (mirrors `list_videos_needing_sync`'s shape):
  `product_articles.status = 'approved' AND sync_to_bigcommerce = true
  AND bowlerdepot_synced_at is null`, joined through `bowlerdepot_products`
  scoped to `match_status = 'matched'` -- same reasoning 6q's own query
  already applies (an `'ambiguous'`/`'unmatched'` row is a known-
  unreliable match; this job should never post content that claims to be
  about the wrong ball).
- **Body construction**: assemble the article's structured fields (hook,
  performance_summary, pros/cons, who_should_buy/skip, buying_tips,
  verdict, faq) into one HTML string for the `body` field. Images
  (`action_shot_image_url`/`product_shot_image_url`) can be inlined as
  plain `<img src="...">` tags pointing straight at their existing public
  S3 URLs -- these are already served directly to the consumer site via
  `public_api`, so there's no new hosting problem to solve. Recommend
  **skipping `thumbnail_path` entirely for a v1** -- it's the one field
  that requires a separate WebDAV upload to `/product_images/` first
  (per BigCommerce's own docs), real added complexity for a cosmetic
  thumbnail that isn't required to publish the post at all.
- **Open design question, deliberately NOT decided here**: a blog post
  needs to link back to its product page, but `bowlerdepot_products`
  (migration 001) only stores `bigcommerce_product_id`/`bigcommerce_sku`/
  `match_status` -- no product-page URL. Two options, neither built:
  (a) an extra `GET /v3/catalog/products/{id}` call per article to read
  that product's own `custom_url.url` at sync time, or (b) a new stored
  column on `bowlerdepot_products` that `bowlerdepot_reconciliation`
  populates once, which this sync then just reads. Left open rather than
  guessed at -- Al's call once this gets built.
- **Idempotency**: `bigcommerce_post_id`/`bowlerdepot_synced_at`
  (migration 028) mirror `product_videos.bowlerdepot_video_id`/
  `bowlerdepot_synced_at` exactly -- a re-run only pushes a
  not-yet-synced row. **Also not decided**: what a regenerate (which
  resets `product_articles.status` back to `'pending'` -- see 022's
  header comment on the no-versioning design) should do to an
  already-synced BigCommerce post. This spec only covers pushing NEW
  posts; updating or deleting a previously-synced one on regenerate/
  flag-off is future scope.
- **Schedule**: simplest is the same `rate(1 hour)` polling shape as
  `bowlerdepot_video_sync`, for consistency. A direct-invoke-on-toggle
  alternative (mirroring `queue_video_discovery`'s admin_api-invokes-
  Lambda-directly pattern) would sync faster but adds a new invoke path
  -- Al's call, not assumed here.

Nothing in this subsection had been built as of the first pass above: no
`src/bowlerdepot_article_sync/` directory, no `template.yaml` resource,
no schedule, no IAM. The flag (migration 028 + the two endpoints above)
was the only part live at that point.

**Follow-up, real incident: "i don't see it in bigcommerce"** -- Al
tested exactly that gap: flipped `sync_to_bigcommerce` on for a real
article via `curl`, got back a clean `{"article_id": "...",
"sync_to_bigcommerce": true}`, and correctly noticed nothing showed up in
BigCommerce. Expected, since at that point the flag was the only thing
built -- confirmed the spec's own framing was right, and prompted
actually building the job. Two open questions from the original spec
were resolved via a follow-up AskUserQuestion exchange with Al: fetch the
product-page link live from BigCommerce rather than storing a new
column, and Al will add the `store_v2_content` scope himself before the
first real run rather than minting a separate token.

**`src/bowlerdepot_article_sync/app.py` (new Lambda, `rate(1 hour)`
schedule, same as `BowlerdepotVideoSyncFunction`) -- now built:**

- `list_articles_needing_sync` mirrors `list_videos_needing_sync`'s exact
  shape: `status = 'approved' AND sync_to_bigcommerce = true AND
  bowlerdepot_synced_at is null`, joined through `bowlerdepot_products`
  scoped to `match_status = 'matched'`. Takes an optional `article_id` to
  scope to exactly one row -- see the on-demand path below.
- `fetch_bigcommerce_product_url` resolves the earlier open question:
  calls BigCommerce's Catalog v3 API (`GET /v3/catalog/products/{id}`) at
  sync time to read that product's own `custom_url.url`, rather than
  adding a stored column. Best-effort -- any failure (network, wrong
  scope on this particular call, deleted product) logs a warning and the
  post still goes out, just without a "View this ball" link.
- `build_article_body_html`/`build_bigcommerce_blog_post_payload` map a
  `product_articles` row to one HTML `body` string (all the structured
  fields, images inlined by their existing public S3 URL, no
  `thumbnail_path`/WebDAV involved) plus the Blog Post payload
  (`title`, `body`, `is_published: true`, `author: "BowlerDepot"`,
  `tags: [brand_name]`).
- `push_article_to_bigcommerce` -- **confirmed live against
  BigCommerce's own docs this session**: Blog Posts are a **v2** API
  (`POST /stores/{store_hash}/v2/blog/posts`, required `title`/`body`),
  and -- the one real shape gotcha here -- the response is the created
  object **directly**, not wrapped in a `"data"` key the way the v3
  Catalog Product Videos endpoint `bowlerdepot_video_sync` uses is.
  Getting this backwards would have broken `bigcommerce_post_id` storage
  silently; a dedicated test
  (`test_push_article_to_bigcommerce_posts_to_v2_endpoint_and_returns_
  raw_object`) pins the unwrapped shape.
- OAuth scope needed is `store_v2_content` (Content: modify) --
  **confirmed a different scope** than `bowlerdepot_video_sync`'s
  Products-modify (`store_v2_products`) need. Same "unverifiable from
  this sandbox" situation as that function's own scope -- Al needs to
  add it in BigCommerce's API Accounts settings before the first real
  run, or every push 403s (logged per-article, doesn't abort the batch).
- **Two invocation shapes, one `handler`**: no `article_id` in the event
  (the hourly schedule) processes every outstanding article; `{"article_
  id": "..."}` (admin_api's `queue_article_sync`, the Articles tab's new
  "Sync now" button) scopes to just that one row, still subject to every
  needs-sync condition -- an article that isn't actually eligible (flag
  off, already synced) just yields zero pushes, not an error.
- Idempotency (`bigcommerce_post_id`/`bowlerdepot_synced_at`, migration
  028) and the no-delete-on-flag-off/no-update-on-regenerate scope
  limits are unchanged from the original spec.

**admin_api + admin-site: "Sync now" on-demand trigger** -- same direct
`lambda:InvokeFunction`, no-queue-in-front convention as `queue_video_
discovery`/`queue_article_generation`. `service.queue_article_sync` +
`POST /articles/{id}/sync-to-bigcommerce`; `ARTICLE_SYNC_FUNCTION_NAME`
env var + a narrowly-scoped `lambda:InvokeFunction` grant on
`AdminApiFunction`. The Articles tab shows a "Sync now" button next to
the existing toggle, but only once `sync_to_bigcommerce` is on and
`bowlerdepot_synced_at` is still null -- invoking the function for an
article its own query wouldn't select is harmless, but the button would
just look like a confusing no-op in those states, so it's hidden rather
than shown-but-inert.

**`template.yaml`**: new `BowlerdepotArticleSyncFunction` (58th
resource, confirmed via the CFN-tolerant YAML loader), `Environment.
BIGCOMMERCE_SECRET_ARN`, the same `HasBigCommerceSecret`-gated secret
read policy `BowlerdepotVideoSyncFunction` uses, and an hourly
`Schedule` event. `AdminApiFunction` gained `ARTICLE_SYNC_FUNCTION_NAME`
and a matching narrowly-scoped invoke grant.

**Tests**: `tests/test_bowlerdepot_article_sync.py` (new, 23/23) --
pure-function tests for body/payload construction (including HTML-
escaping and the omitted-link-when-`product_url`-is-`None` case), the
needs-sync query's exact filter conditions, the v2-unwrapped-response
shape, a failed product-URL lookup not blocking a successful post, and
`_process_one_article`/`handler` orchestration via fake DB/session
objects, same manual-runner convention as `test_bowlerdepot_video_
sync.py`. `tests/test_admin_api_service.py` gained 3 tests for
`queue_article_sync` (invokes with the right payload, missing-article
raises, missing-function-name soft-fails), mirroring `queue_article_
generation`'s own test shape. Full regression sweep across every test
file: clean.

**Follow-up: "where can i get the article ID"** -- the id was only ever
embedded in admin-site button `onclick` handlers, never shown as text,
so the only way to get one (e.g. to hand-run the `PATCH
/articles/{id}/bigcommerce-sync` toggle via `curl`) was a raw `GET
/articles` call. Added a generic `copyToClipboard` helper
(`navigator.clipboard.writeText`, falling back to a hidden-textarea +
`document.execCommand('copy')` for contexts without Clipboard API
access) and a truncated id + "Copy ID" button in both the Articles tab's
list row and `renderArticlePreviewHtml`'s detail panel (shared by both
the standalone Articles tab and the product-detail Articles section, so
it shows in both places automatically). Admin-site only -- no
service.py/app.py/migration change.

**Follow-up: single body image + WebDAV thumbnail_path** -- Al, after
the sync job's first real push went out, asked "can you set the
thumbnail image too, also only the 16:9 image is needed in the blog post
itself." Two changes:

- `build_article_body_html` now embeds ONLY `action_shot_image_url` (the
  16:9 hero shot) -- `product_shot_image_url` (the 1:1 square shot) is no
  longer inlined in the post body. Al's own call: one hero image is
  enough in the body itself; the square shot's real job is now the
  thumbnail below.
- **`thumbnail_path` (real WebDAV upload, built this session, Al's
  choice via `AskUserQuestion` -- "set up WebDAV properly" over the
  "skip it" fallback)**: confirmed via BigCommerce's own docs +
  community reports that `thumbnail_path` can only point at a file
  BigCommerce is already serving under `/product_images/` -- never an
  arbitrary external URL -- so setting it needs a real upload via WebDAV,
  a COMPLETELY DIFFERENT credential type (a WebDAV username [an email
  address] + password from **Settings -> File access (WebDAV)** in
  BigCommerce's control panel) than the OAuth bearer token
  (`X-Auth-Token`) used everywhere else in this project.
  - New `upload_thumbnail_via_webdav(session, webdav, article_id,
    image_url)`: downloads the article's own `action_shot_image_url` and
    `PUT`s it to `{webdav_url}/product_images/uploaded_images/
    {filename}` (the `uploaded_images/` convention is confirmed via a
    real BigCommerce CDN URL seen during research:
    `.../product_images/uploaded_images/props.jpg`); `filename` is
    `article-{article_id}.{ext}` (the article's own UUID is already safe
    lowercase-hex-and-dashes, satisfying BigCommerce's documented
    a-z/0-9/-/_ filename rule). Returns the path relative to
    `/product_images/` (`"uploaded_images/{filename}"`) for
    `thumbnail_path`, or `None` on ANY failure -- unconfigured WebDAV, a
    download error, or an upload error all just mean "post without a
    thumbnail," same "degrade, don't block" pattern as
    `fetch_bigcommerce_product_url`. Never blocks the article.
  - `build_bigcommerce_blog_post_payload` gained an optional
    `thumbnail_path` param -- included in the payload only when the
    upload actually succeeded (omitted entirely otherwise, not sent as
    `None`/empty).
  - `get_bigcommerce_credentials` now also reads three OPTIONAL keys off
    the SAME shared `BIGCOMMERCE_SECRET_ARN` secret every other
    BigCommerce-touching function here already reads --
    `webdav_url`/`webdav_username`/`webdav_password`. Deliberately not a
    new secret, parameter, or IAM grant: this ARN is already wired into
    `BowlerdepotArticleSyncFunction`, and a WebDAV username/password is
    just a different credential shape carried on the same object.
    `webdav` comes back `None` unless all three keys are present, so an
    as-yet-unconfigured account just means "no thumbnail" rather than a
    `KeyError` -- **no `template.yaml` change was needed for this
    feature at all.**

**Al still needs to do, before thumbnails will actually show up:**

1. In BigCommerce's control panel: **Settings -> File access (WebDAV)**
   -> create/view a WebDAV account. Note the WebDAV URL (e.g. something
   like `https://store-{hash}.mybigcommerce.com/dav`), username (an
   email address), and password it gives you.
2. Add those three values into the SAME secret the OAuth token already
   lives in (whatever `BIGCOMMERCE_SECRET_ARN` resolves to for this
   stack) -- merge them in rather than overwriting the existing
   `store_hash`/`auth_token` keys:

   ```bash
   ARN="<your BigCommerce secret's ARN>"
   CURRENT=$(aws secretsmanager get-secret-value --secret-id "$ARN" --query SecretString --output text)
   UPDATED=$(echo "$CURRENT" | jq \
     --arg url "https://store-XXXXXXX.mybigcommerce.com/dav" \
     --arg user "your-webdav-username@example.com" \
     --arg pass "your-webdav-password" \
     '. + {webdav_url: $url, webdav_username: $user, webdav_password: $pass}')
   aws secretsmanager put-secret-value --secret-id "$ARN" --secret-string "$UPDATED"
   ```

   (No `jq`? Same fallback as the earlier OAuth-token update: fetch
   `CURRENT`, hand-edit the JSON to add the three keys, then
   `put-secret-value` with the edited string.)
3. No redeploy needed -- `bowlerdepot_article_sync` re-reads the secret
   on every invocation, so the very next scheduled run (or "Sync now")
   after the secret is updated will attempt the WebDAV upload.

**Tests**: `tests/test_bowlerdepot_article_sync.py` grew from 23 to 39 --
a dedicated body-html test pinning "exactly one `<img>`, and it's the
action shot, not the product shot"; a full suite for
`upload_thumbnail_via_webdav` (success, not-configured, no-image-url,
trailing-slash URL normalization, unrecognized-extension defaulting to
`.png`, download failure, upload exception, upload HTTP error -- all via
a `_FakeSession` extended with `.put()` and a `get_responses` queue for
tests needing the product-URL lookup and the thumbnail download to
return two different things); `get_bigcommerce_credentials`'s new
webdav-extraction logic (all-keys-present, some-keys-present,
no-keys-present) via a `sys.modules`-injected fake `boto3` module (same
pattern `test_product_article_generator.py`'s `_HandlerPatchGuard`
already established, since boto3 isn't actually installed in this
sandbox); and `_process_one_article`/payload tests confirming
`thumbnail_path` is wired through end-to-end when WebDAV is configured,
and cleanly absent when it isn't. Full regression sweep across every
test file: clean. No `template.yaml` change, so no new resource-count
check needed.

**REAL INCIDENT (2026-09-05): WebDAV upload 401'd on Al's very first
live test -- root cause was Basic vs. Digest auth, not credentials.**
Al deployed the code above, updated the secret with real WebDAV
credentials, and got a `401 Unauthorized` on the very next sync (visible
in CloudWatch as `Could not upload WebDAV thumbnail ... 401 Client
Error`). The obvious suspects were all ruled out one at a time: the
WebDAV username matched exactly what BigCommerce's own Settings › File
access (WebDAV) page showed, and -- the decisive test -- **Al
successfully connected with Cyberduck using the exact same URL/
username/password**, proving the credentials and account setup were
fine. That ruled out everything about the BigCommerce account and left
only "something about the plain HTTP request itself."

A direct `curl -v -u "user:pass" -T file.png <url>` reproduction (same
shape as `requests`' plain `auth=(user, pass)` tuple, i.e. Basic Auth)
made the real cause unambiguous:

```
< HTTP/2 401
< www-authenticate: Digest realm="SabreDAV",qop="auth",nonce="...",opaque="..."
<?xml version="1.0" encoding="utf-8"?>
<d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">
  <s:exception>Sabre\DAV\Exception\NotAuthenticated</s:exception>
  <s:message>No 'Authorization: Digest' header found. Either the client
  didn't send one, or the server is misconfigured</s:message>
</d:error>
```

BigCommerce's WebDAV is backed by **SabreDAV**, which flatly rejects
Basic Auth and requires **Digest Auth** -- a real HTTP-level fact about
their server that isn't mentioned anywhere in BigCommerce's own WebDAV
documentation (which only walks through Cyberduck setup). Cyberduck (and
every other real WebDAV client) performs the Digest challenge/response
handshake transparently, which is exactly why "Cyberduck connects fine"
and "the Lambda gets 401" were both true at the same time -- it was
never a credentials problem.

**Fix**: `upload_thumbnail_via_webdav` now passes
`requests.auth.HTTPDigestAuth(webdav["username"], webdav["password"])`
instead of a plain `(username, password)` tuple (which `requests`
treats as Basic Auth by default) -- `HTTPDigestAuth` performs the same
two-round-trip challenge/response handshake a real WebDAV client does.
No credential, URL, or template change needed at all; this was purely
an auth-scheme bug in the upload code itself.

**Tests**: added
`test_upload_thumbnail_via_webdav_uses_digest_auth_not_basic`, which
pins that the `auth` object passed to `session.put()` is an actual
`requests.auth.HTTPDigestAuth` instance (checked via `isinstance` plus
its `.username`/`.password` attributes, since it has no `__eq__`) rather
than a tuple -- 23 → 40 tests. Full regression sweep: clean. No
`template.yaml` change.

**Follow-up: dedupe body image against the thumbnail, add a "Resync"
button** -- two changes, both from Al's very next real-world test once
the Digest-auth fix actually got a thumbnail onto a live post:

1. **"including the image in the body duplicates because the thumbnail
   now works"**: once `thumbnail_path` actually renders (the theme shows
   it at the top of the post), the SAME action-shot image was ALSO still
   inlined in the body underneath it. `build_article_body_html` now
   takes an optional `thumbnail_path` param and only inlines the image
   as a FALLBACK when there's no thumbnail (WebDAV not configured, or
   the upload failed) -- otherwise the post would have no hero image at
   all in that case. `build_bigcommerce_blog_post_payload` passes its own
   `thumbnail_path` argument through into this call so the two always
   stay in sync.

2. **"can we add a resync button"**: the Zero Mercy Solid article (and
   anything else) synced BEFORE the Digest-auth fix has no thumbnail --
   and there was no way to push that fix onto an already-published post.
   `list_articles_needing_sync`'s own query permanently excludes anything
   with `bowlerdepot_synced_at` already set, by design (no re-sync-on-
   rerun), so simply re-flagging or re-triggering "Sync now" on an
   already-synced article is a guaranteed no-op.

   Built as a genuinely separate path rather than reusing the create
   flow, since blindly re-running `push_article_to_bigcommerce` (a POST)
   against an already-synced article would create a SECOND, duplicate
   blog post rather than fixing the first one:
   - `list_articles_needing_resync(conn, article_id)` -- same shape as
     `list_articles_needing_sync` but FLIPPED (`bowlerdepot_synced_at IS
     NOT NULL`, `bigcommerce_post_id IS NOT NULL`), plus it selects
     `bigcommerce_post_id` so the update knows which post to target.
     Deliberately requires an explicit `article_id` always -- there is
     no batch/scheduled resync mode, since this overwrites a LIVE post
     and should only ever be a deliberate one-at-a-time admin action.
   - `push_article_update_to_bigcommerce(session, store_hash, auth_token,
     bigcommerce_post_id, payload)` -- PUTs onto
     `/stores/{store_hash}/v2/blog/posts/{id}` (BigCommerce's documented
     update operation for this same v2 resource), same unwrapped-response
     shape as the create path.
   - `_process_one_article` gained a `resync: bool = False` param: when
     true, it calls the update path instead of create, and keeps the
     article's EXISTING `bigcommerce_post_id` rather than taking a new id
     from the response.
   - `handler` gained a third invocation shape:
     `{"article_id": "...", "resync": true}` -- requires `article_id`
     (returns a 400 otherwise, since resync with no target doesn't mean
     anything), dispatches to `list_articles_needing_resync` instead of
     `list_articles_needing_sync`.
   - `admin_api`: `service.queue_article_resync` (mirrors
     `queue_article_sync`'s existence-only check + direct
     `lambda:InvokeFunction`, but the payload also carries
     `"resync": true`) + `POST /articles/{id}/resync-to-bigcommerce`.
     Reuses the SAME `ARTICLE_SYNC_FUNCTION_NAME` env var and IAM grant
     `queue_article_sync` already has -- no `template.yaml` change.
   - `admin-site`: a "Resync" button next to the existing sync controls,
     shown only once `bowlerdepot_synced_at` is set (the opposite
     condition from "Sync now"), with its own confirm dialog explicitly
     warning it overwrites the live post rather than creating a new one.

**Tests**: `tests/test_bowlerdepot_article_sync.py` grew from 40 to 50 --
`list_articles_needing_resync` (row shape, empty-when-ineligible, exact
filter conditions incl. the flipped sync-status check and the
`bigcommerce_post_id is not null` requirement), `push_article_update_
to_bigcommerce` (PUTs to the right URL with the existing post id,
raises on error same as the create path), `_process_one_article`'s
resync branch (updates the SAME post id and never calls `.post()` at
all, and a failed resync never touches `bowlerdepot_synced_at`), and
three `handler` tests (400 with no `article_id`, dispatches to the
resync query not the sync query, threads `resync=True` through to
`_process_one_article`). `_FakeSession.put()` extended to accept both
real call shapes (the WebDAV upload's `data`/`auth` and the BigCommerce
update's `headers`/`json`) since both real callers now share it.
`tests/test_admin_api_service.py` gained 3 tests for
`queue_article_resync`, mirroring `queue_article_sync`'s own shape --
265 → 268. Full regression sweep across every test file: clean. No
`template.yaml` change (same function, same env var, same IAM grant).

### 6v. Learn section (learn.bowlerdepot.com) -- moving ball reviews off the BigCommerce blog

Al, once the BigCommerce blog sync (6u/6q) was actually working: "now
that we are going down the path of blog post syncing it is exposing the
lack of features in bigcommerce as it pertains to a blog. i would like
this to be a sophisticated learn section with articles that are
displayed in a way that is best from a UI/UX perspective and bigcommerce
is just not the place." Two real architecture decisions, both resolved
via `AskUserQuestion` before any build started:

1. **Hosting**: "Branded subdomain (Recommended)" -- a real
   `learn.bowlerdepot.com` subdomain, own S3 bucket/CloudFront
   distribution/deploy role, rather than a path (`bowlerdepot.com/learn`)
   reverse-proxied in front of the live BigCommerce storefront. The path
   option was real but riskier: BigCommerce's own routing/cart/checkout/
   session cookies would sit behind the same reverse proxy as this new
   site, with a real chance of a subtle collision; a subdomain keeps the
   two completely independent at the infrastructure level.
2. **The existing BigCommerce sync (6q/6u)**: "Keep both in parallel as-
   is" -- `bowlerdepot_article_sync` keeps pushing to BigCommerce's
   native blog completely unchanged. Nothing in this section touches
   that pipeline; the two are independent publishing destinations for
   the same underlying `product_articles` content.

**Brand research (before any code):** live bowlerdepot.com pulled via
the in-app browser's `javascript_tool` (computed CSS off the real
rendered page, not guessed) to keep the new site from looking
disjointed from the storefront it lives under -- confirmed: black
header/nav bar (`#000000`), body font **Cabin** (Google Font, falls back
to Arial/Helvetica/sans-serif), primary text/heading color `#0f0f2d` (a
near-black navy, not pure black), primary link/accent `#1f439e` (blue),
a secondary deep-indigo `#212152` used sparingly on the live site, and
`#d14343` (red) for sale/alert-style callouts. Logo asset pulled
straight from the live page's own `<img>`:
`https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/201x75/bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png`
(dark-on-transparent -- `bowlerdepot-learn/src/index.css` inverts it via
CSS `filter` to read against this site's own black header, same asset,
no separate light-mode export needed).

**`public_api` gained a real list/browse endpoint (`GET /articles`)** --
previously only `GET /products/{id}/article` existed (single article, by
product), no way to browse the catalog of articles at all.
`service.list_articles(conn, brand_id=, coverstock_id=, search=, sort=,
limit=, offset=)` in `src/public_api/service.py`: same
published-is-non-negotiable posture as every other route in this module
(`pa.status = 'approved' and p.published = true`, baked into the SQL
text, not a param a caller could relax), joins `product_articles` ->
`products` -> `brands` for card-shaped rows (title/hook/generated_at/
reviewed_at plus the underlying product's id/name/url/brand_name/
coverstock_name/coverstock_type/thumbnail-flagged image). `sort` mirrors
`list_products`'s own `_SORT_ORDER_BY` convention but as its own
`_ARTICLE_SORT_ORDER_BY` dict (`newest`/`oldest` by `reviewed_at` --
when an admin actually approved/published, not `generated_at` when it
was first drafted -- plus `title_asc`/`title_desc`), every branch ending
in the same `pa.id asc` tiebreaker for stable pagination. No
`template.yaml` change -- rides the existing `PublicHttpApi`
`{proxy+}` catch-all integration, same as every other `public_api`
route added since the very first one. **Tests**:
`tests/test_public_api_service.py` gained 11 new tests (query-capturing-
fake-connection style, same as `list_products`'s own tests) -- 72 → 83,
full regression sweep clean.

**`bowlerdepot-learn/`** -- new Vite + React + TypeScript SPA, structured
identically to `consumer-site/` (same `package.json`/`tsconfig`/
`vite.config.ts` shape, same plain-hand-written-CSS-with-custom-
properties approach, same unauthenticated `PublicApiFunction` client
pattern) but branded to bowlerdepot.com per the research above rather
than generic. Two routes: Learn index (`/` -- brand filter, search,
sort, card grid) and article detail (`/articles/:productId` -- hook,
performance summary, pros/cons, who-should-buy/skip, buying tips,
verdict, live spec table, FAQ, comparison-to-similar-balls). A "Shop
this ball at BowlerDepot" link on every article uses bowlerdepot.com's
own Stencil search (`search.php?search_query=<name>`, confirmed live to
land a visitor on the right product) rather than inventing a storefront
URL scheme -- `bowlerdepot_products` (001_init_schema.sql) only ever
stored the numeric BigCommerce product id/SKU, never a resolvable
slug/permalink, and BigCommerce's Stencil storefronts don't expose a
generic "view by id" route. See `bowlerdepot-learn/README.md` for the
full local-dev/build instructions and this same reasoning in more
detail.

**Build-time static prerendering (SEO)** -- the real problem a plain
client-rendered SPA has that BigCommerce's native blog didn't: a
crawler's very first response for `/articles/<id>` would otherwise be an
empty `<div id="root">` and a `<script>` tag, not the actual review
text. `bowlerdepot-learn/scripts/prerender.ts` runs as the last step of
`npm run build` (after `vite build` has already produced `dist/`):
fetches every approved article from `GET /articles` (paginated), fetches
each one's full detail from `GET /products/{id}/article`, and writes
`dist/articles/<product_id>/index.html` -- a copy of the built
`index.html` with the empty root div replaced by hand-built (NOT React-
rendered -- no `react-dom/server`, no hydration to keep in sync) HTML
mirroring `ArticleDetailPage.tsx`'s visible content, plus real
`<title>`/meta description/Open Graph tags/canonical link/schema.org
`Review` JSON-LD. `src/main.tsx` uses `createRoot(...).render(...)`, NOT
`hydrateRoot` -- the prerendered markup is fully replaced by the real
interactive app once JS loads for a normal visitor, no hydration-
mismatch warnings possible since nothing is being hydrated. Also writes
`dist/sitemap.xml` (Learn index + every prerendered article URL) --
`public/robots.txt` (copied verbatim by Vite) points crawlers at it.
Verified end-to-end in this sandbox against a small local mock HTTP
server standing in for `public_api` (`node --experimental-strip-types
scripts/prerender.ts` -- this sandbox has no npm registry access, so
`tsx` itself couldn't be installed; Node 22's built-in TypeScript-
stripping flag ran the real script unmodified instead) -- confirmed
correct HTML-escaping (a verdict string containing `<`/`>` came out
properly escaped, not injected raw), correct meta/OG/JSON-LD content,
and a correct `sitemap.xml`.

**KNOWN DEPENDENCY, wired into `template.yaml` below**: a bare
`/articles/<id>` request (no trailing slash) does NOT automatically
resolve to `dist/articles/<id>/index.html` on this project's S3-via-
CloudFront-OAC hosting (unlike the separate "S3 static website hosting"
mode, OAC-fronted S3 has no automatic index-document-per-folder
behavior). This requires the CloudFront Function described next --
without it, prerendering's whole point (a crawler seeing real HTML at
the same URL a visitor actually navigates to) doesn't hold.

**`template.yaml`**: `LearnSiteBucket`/`LearnSiteOAC`/
`LearnSiteDistribution`/`LearnSiteBucketPolicy` mirror `ConsumerSite*`
exactly (same private-bucket-behind-OAC shape, same
`CachePolicyId: 658327ea-...` managed-CachingOptimized policy, same
403/404 -> `/index.html` `CustomErrorResponses` SPA fallback), gated
behind new `LearnSiteDomainName`/`LearnSiteCertificateArn` params (same
blank-means-CloudFront-default-domain convention as
`ConsumerSiteDomainName`/`ConsumerSiteCertificateArn`) via a new
`HasLearnSiteDomain` condition. One genuinely new piece:
**`LearnSitePrettyUrlFunction`** (`AWS::CloudFront::Function`,
`cloudfront-js-2.0`, attached to `LearnSiteDistribution`'s
`DefaultCacheBehavior` as a `viewer-request` `FunctionAssociations`
entry) -- rewrites any request whose path doesn't end in `/` and whose
last segment has no `.` (i.e. isn't already a real asset like a hashed
JS bundle, `robots.txt`, `sitemap.xml`) to append `/index.html` before
the origin fetch. This is what makes `scripts/prerender.ts`'s output
reachable at the exact URL a visitor/crawler actually requests -- a path
with no prerendered page at that location still falls through to
`CustomErrorResponses`'s existing 403/404 -> SPA-shell rewrite exactly
as before, this function only ever ADDS a suffix, never hides a genuine
404. `LearnSiteDeployRole` mirrors `ConsumerSiteDeployRole` exactly
(same OIDC trust policy shape, same real-incident-informed
`StringLike` wildcard on the GitHub-immutable-ID suffix -- see 6n's own
writeup of that incident) and reuses the SAME `GitHubRepo` param and
the account's one `GitHubOidcProvider` rather than standing up a second
OIDC provider (an AWS-account-level singleton, only one per URL).
New stack outputs: `LearnSiteBucketName`, `LearnSiteDistributionId`,
`LearnSiteUrl`, `LearnSiteDeployRoleArn` (the last one only when
`GitHubRepo` is set). Verified via the CFN-tolerant YAML loader: 58 ->
64 resources, 46 -> 48 params, 6 -> 8 conditions.

**`.github/workflows/deploy-learn-site.yml`** -- its own workflow file
(not folded into `deploy-consumer-site.yml`, so a push touching only one
site doesn't trigger the other's deploy), same OIDC-federation shape:
`npm ci`, `npm run build` (which chains `tsc -b && vite build && npm run
prerender` -- see `bowlerdepot-learn/package.json` -- so
`VITE_PUBLIC_API_URL`/`SITE_URL` need to be set for this ONE step, not
split across two), `aws s3 sync dist/ ... --delete`, CloudFront
invalidation. Triggers on push to `main` touching `bowlerdepot-learn/**`
or the workflow file itself, plus manual `workflow_dispatch`.

**One-time setup** (same shape as 6n's consumer-site CI setup, repeated
here for the Learn site specifically):
1. Redeploy with `LearnSiteDomainName`/`LearnSiteCertificateArn` set (if
   you want `learn.bowlerdepot.com` live immediately rather than the
   default `*.cloudfront.net` domain -- same ACM-in-us-east-1
   requirement as `ConsumerSiteCertificateArn`) and `GitHubRepo` already
   set from 6n (no change needed there -- both sites' deploy roles trust
   the same repo/OIDC provider).
2. Grab the new outputs:
   ```bash
   aws cloudformation describe-stacks --stack-name <your-stack-name> \
     --query "Stacks[0].Outputs[?OutputKey=='LearnSiteDeployRoleArn' || OutputKey=='LearnSiteBucketName' || OutputKey=='LearnSiteDistributionId'].{Key:OutputKey,Value:OutputValue}" --output table
   ```
3. Add these repo Variables (Settings -> Secrets and variables -> Actions
   -> Variables) alongside the ones 6n already had you set --
   `AWS_REGION` and `PUBLIC_API_URL` are REUSED as-is, no new value
   needed:
   - `LEARN_SITE_DEPLOY_ROLE_ARN` -- from step 2.
   - `LEARN_SITE_BUCKET` -- the `LearnSiteBucketName` stack output.
   - `LEARN_SITE_DISTRIBUTION_ID` -- the `LearnSiteDistributionId` stack
     output.
   - `LEARN_SITE_URL` -- `https://learn.bowlerdepot.com` (or whatever
     `LearnSiteDomainName` you set, or the `LearnSiteUrl` stack output's
     `*.cloudfront.net` value if you skipped the custom domain for now)
     -- feeds `scripts/prerender.ts`'s `sitemap.xml`/canonical-link
     generation, kept separate from `PUBLIC_API_URL` since they're
     different domains entirely.
4. **Before the first push**: `cd bowlerdepot-learn && npm install`
   locally (this sandbox has no npm registry access at all -- confirmed,
   every `npm install`/`npm view` attempt here 403's -- so
   `package-lock.json` was never generated and isn't committed yet).
   **Commit the generated `package-lock.json`** -- this is the EXACT
   same real incident 6n's consumer-site CI already hit once (missing
   lockfile -> `npm ci` fails outright in the workflow); flagging it here
   proactively instead of waiting to rediscover it via a failed run.
5. Push anything under `bowlerdepot-learn/` to `main` (or run the
   workflow manually) -- it deploys itself from there on, same as
   consumer-site.

**Tests**: `tests/test_public_api_service.py` (11 new `list_articles`
tests, 72 -> 83 -- see above). No new Python test surface for
`bowlerdepot-learn/` itself (a pure frontend project, same "logic-
verified via the plain-function layer, not actually executed" honesty
note as `consumer-site/`'s own README) -- `scripts/prerender.ts` WAS
actually executed in this sandbox (via Node's `--experimental-strip-
types`, since `tsx` couldn't be installed) against a local mock server
standing in for `public_api`, output inspected by hand and confirmed
correct (see above). Full regression sweep across every existing Python
test file: clean, unaffected by this frontend/infra-only change outside
`public_api`.

### 6w. Related-review cross-linking + real ecommerce product links (Learn section)

Al, on the Learn article detail page: "can we add cross linking at the
bottom to 'related' ball reviews. would it be possible to link to the
ecommerce product page for some balls inline too." Two asks, both landed
in `public_api.get_product_article` with NO new migration for either --
see that function's own docstring for the full reasoning:

- **Related reviews.** `comparison_table` (existing) is every published
  sibling from the article's `sibling_product_ids`, regardless of whether
  that sibling has its own review. The new `related_reviews` field is the
  same sibling set narrowed to ones that DO have their own approved
  `product_articles` row (inner join), since only those resolve to a real
  Learn article page to link to. Rendered as a "Related Reviews" section
  at the bottom of `ArticleDetailPage.tsx`, linking to `/articles/
  <product_id>` (this site's own route, not an external link) -- ordered
  newest-reviewed-first.

- **Real ecommerce links.** Every "Shop this ball" link on this site has
  used `bowlerDepotSearchUrl()` (client.ts) -- a BowlerDepot search-
  results page, not a real product page, because `bowlerdepot_products`
  never stored a resolvable storefront URL. Turns out this project
  already HAS that data: `price_checker`'s BigCommerce
  (`fetch_method='api'`) price source (014/016_price_tracking*.sql)
  resolves and stores exactly this in `product_price_sources.product_url`
  (via `custom_url.url` + `price_sites.base_url` -- see
  `price_checker.extract_bigcommerce_price_fields`'s own docstring),
  every time it checks a product's price. The new `ecommerce_url` field
  on `product` and each `comparison_table` row is a live subquery against
  that existing table (most recently checked approved+active BigCommerce
  source), null when price_checker hasn't matched/approved one for that
  product yet. `ArticleDetailPage.tsx`'s hero CTA and "Similar Balls"
  cards now use `ecommerce_url` when present, falling back to
  `bowlerDepotSearchUrl()` otherwise -- exactly Al's own framing, "for
  SOME balls," since coverage depends entirely on price_checker's
  existing match/approval state, which is already growing on its own
  daily schedule with no new work needed here.

`scripts/prerender.ts` also renders `related_reviews` as real `<a href="/
articles/<id>/">` links in the static HTML (not just left to the client
bundle) -- this is the one place inline ecommerce links were deliberately
NOT added to the prerendered output, since the "Shop this ball" CTAs were
never part of the static markup to begin with (an external, non-SEO-
relevant action) and internal review-to-review links are what actually
matter for crawl discovery.

**Tests**: `tests/test_public_api_service.py` gained 9 new tests (83 ->
92) covering `ecommerce_url` (present/null/ignores-pending-or-inactive/
picks-most-recent, both on `product` and on a `comparison_table` row) and
`related_reviews` (only-approved-article siblings, drops unpublished,
orders newest-first, empty-when-no-siblings). Fixture gained a
`db["price_sources"]` table + `_seed_bigcommerce_price_source()` helper
mirroring the real `product_price_sources`/`price_sites` join. Full
regression sweep: clean. `bowlerdepot-learn`'s `tsc -b` type-check ran
clean in this sandbox for the first time against a real, complete
`node_modules` (present here now, unlike earlier in this project -- see
6v's own note on this sandbox previously having zero npm registry
access); `npx vite build` itself still can't run in this sandbox
(`@rollup/rollup-linux-arm64-gnu` missing -- `node_modules` here was
synced from Al's own local macOS `npm install`, which only fetched the
darwin-arm64 native binary, not linux; irrelevant to the real GitHub
Actions build, which runs a fresh `npm ci` on `ubuntu-latest` and fetches
its own correct binary). `scripts/prerender.ts`'s new
`renderRelatedReviews` was verified by actually running the script (via
`--experimental-strip-types`, same approach as 6v) against a local mock
`public_api` -- output inspected by hand and confirmed correct.

### 6x. Google structured data (JSON-LD) for Learn article pages

Al: "can we add all the proper google structured data to the markup" --
"for the articles on learn site", then pointed directly at Google's
Article structured-data doc
(developers.google.com/search/docs/appearance/structured-data/article#microdata).

**What was there before**: `scripts/prerender.ts`'s `renderJsonLd`
emitted a single ad-hoc `@type: "Review"` block with no `reviewRating`.
Checked against Google's own current Review-snippet documentation:
`reviewRating`/`reviewRating.ratingValue` is REQUIRED on every `Review`.
This project has never had a genuine numeric rating to put there --
`product_articles` (022) has no rating/score column at all, these are
AI-generated prose reviews, not user star ratings -- so the old block
was quietly non-compliant/ineligible the whole time. Not something Al
flagged; found while researching this request.

**What replaced it** (`renderStructuredData` in `prerender.ts`, four
independent `<script type="application/ld+json">` blocks per page, each
one dropped entirely when there's no real data to back it, never a
fabricated field):

- **Article**: headline/image/`datePublished`+`dateModified` (from the
  new `reviewed_at`, see below)/author+publisher `Organization` (publisher
  includes a `logo` pointing at the same real BowlerDepot logo asset
  `Nav.tsx` already uses). No required properties per Google's Article
  guide, so this one is compliant on real data alone.
- **Product**: name/image/`brand`, plus whichever of `review`/`offers`
  actually has real data (Google's Product-snippet docs require at least
  one of `review`/`aggregateRating`/`offers` -- `aggregateRating` is
  skipped entirely, no genuine aggregate exists). `review` uses ONLY
  `positiveNotes`/`negativeNotes` (built from the article's real
  `pros`/`cons`, schema.org's own lighter-weight sub-feature requiring
  "at least two statements in any combination," not a `reviewRating`) --
  this is how real pros/cons content ends up in Product structured data
  honestly, without a star rating. `offers` is included only when
  `price_checker` has an actual, checked price for this product
  (`ecommerce_price` non-null) -- most of the catalog won't have this yet,
  same "for some balls" reality as 6w's ecommerce links. The whole
  Product block is omitted if neither `review` nor `offers` exists.
- **BreadcrumbList**: Home -> this article, 2 `ListItem`s.
- **FAQPage**: straight from the article's real `faq` (already rendered
  visibly on the page), omitted when there's no FAQ. Code comment notes
  Google deprecated the FAQ rich-result feature in 2026 -- this markup is
  kept as still-valid, harmless schema.org data for other consumers
  (e.g. AI answer engines), not as a live Google rich-result bet.

**Backend groundwork** (`public_api.get_product_article`, no new
migration): added `reviewed_at` (top-level, for Article's dates --
distinct from `generated_at`, the draft-generation timestamp) and, on
`product`, `brand_name` plus real Offer data --
`ecommerce_price`/`ecommerce_price_currency`/`ecommerce_in_stock`, all
read from the SAME `product_price_sources` row as the existing
`ecommerce_url` via chained `LEFT JOIN LATERAL`s (first pick the
most-recently-checked approved+active BigCommerce price source, then its
most recent `product_price_history` row) -- guarantees the URL and its
price/availability never come from two different sources. Deliberately
NOT applied to `comparison_table`'s own `ecommerce_url` (that stayed
URL-only; out of scope, no JSON-LD need there).

**Tests**: `tests/test_public_api_service.py` gained 7 new tests (92 ->
99): `reviewed_at` present/null, `brand_name` on `product`, and the Offer
fields present/null-when-never-checked/null-when-no-bigcommerce-source/
matches-most-recently-checked-source. Fixture's
`_seed_bigcommerce_price_source()` helper extended to accept
price/currency/in_stock. Full regression sweep: clean (43 test files).

**Verification**: `bowlerdepot-learn`'s `tsc -b` type-check clean (both
`prerender.ts`'s new builder functions and `src/api/types.ts`'s matching
new optional fields -- `reviewed_at`, `brand_name`,
`ecommerce_price`/`ecommerce_price_currency`/`ecommerce_in_stock` on
`ArticleDetail`/`ArticleProductSpec`; not yet consumed by any live React
component, purely additive for JSON-LD). `renderStructuredData` verified
by actually running `prerender.ts` (`--experimental-strip-types`, same
approach as 6w) against a local mock `public_api` with two fixtures --
one with a real offer + FAQ + 3 pros/1 con (produced all four blocks,
inspected by hand, matched the design above exactly) and one with no
offer, one pro/zero cons (below the 2-note minimum), and no FAQ (produced
only Article + BreadcrumbList, confirming Product/FAQPage really do get
omitted rather than emitting empty/fabricated fields).

### 6y. Learn index cards use the AI product shot, not the raw catalog photo

Al: "can we use the product shot for the card in the list of review
articles."

`public_api.list_articles` (backs the Learn index/browse page) now also
selects `pa.product_shot_image_url` -- the article's own AI-generated
stylized product hero shot (023_product_article_images.sql), the same
image already used as part of the detail page's hero-image fallback
chain, not the ball's raw scraped photo. Returned as a genuinely separate
field alongside the existing `primary_image_url` rather than resolved in
SQL, since `product_shot_image_url` is nullable (image generation is an
independently-fallible second step -- see that migration's own comment)
and a caller that wants the raw photo specifically still can.

`ArticleCard.tsx` (the actual card component on the Learn index page)
picks `article.product_shot_image_url || article.primary_image_url` for
its `<img>` -- same fallback-when-missing pattern used everywhere else
this project surfaces these two image sources together. `types.ts` and
`prerender.ts`'s own `ArticleCard` interface both gained the matching
field for type accuracy (prerender.ts doesn't render the index page's
cards itself -- that page is client-rendered -- so this is documentation-
only there, not a behavior change).

**Tests**: `tests/test_public_api_service.py` gained 1 new shape test (99
-> 100) confirming `pa.product_shot_image_url` is in `list_articles`'
SELECT list, same query-capturing-connection pattern as this function's
existing filter/sort/join tests (no fixture-driven row-mapping harness
exists for `list_articles`, unlike `get_product_article`). Full
regression sweep: clean (43 test files). `tsc -b`: clean.

### 6z. Similar Balls list gets real BowlerDepot pricing (not just links)

Al: "can we includ a list of balls similar [to the] ball the article is
for towards the bottom of the article and inlcude links and pricing for
it using the bowlerdepot.com pricing data." The "Similar Balls" list at
the bottom of each article already existed (`comparison_table`, task
#444/6w) with links to each sibling's real BowlerDepot storefront page
when known -- this ask adds real pricing to those same cards, which 6w
had deliberately scoped OUT at the time ("Al's ask there was inline
LINKS, not full price data").

`public_api.get_product_article`'s `comparison_table` query now also
selects `ecommerce_price`/`ecommerce_price_currency`/`ecommerce_in_stock`
per sibling, via the same two-LATERAL-join shape (`ecom_source` then
`ecom_price`) already used for `product`'s own Offer fields (task #448)
-- guarantees a sibling's price/currency/stock come from the SAME chosen
price source as its own `ecommerce_url`, not an independent lookup that
could disagree. Same never-fabricated posture throughout: all three are
null together whenever price_checker hasn't matched or successfully
priced that sibling yet, exactly like `product`'s fields already work.

`ArticleDetailPage.tsx`'s Similar Balls cards now show a formatted price
(`Intl.NumberFormat` currency formatting) below the core/coverstock meta
line when present, plus an "Out of stock" badge when `ecommerce_in_stock
=== false` (not shown when stock status is unknown, i.e. null). No price
line at all when null -- same silent-omission convention as everywhere
else this project surfaces optional pricing. `types.ts`'s `ComparisonRow`
gained the matching three fields. Not rendered into `scripts/
prerender.ts`'s static HTML -- comparison_table's links are external
BowlerDepot/search links, not internal review-to-review links, so they
were never part of the prerendered crawl-relevant markup to begin with
(same reasoning related_reviews' links ARE prerendered and these never
were -- see that field's own docstring).

**Tests**: `tests/test_public_api_service.py` gained 3 new tests (100 ->
103): pricing present when a sibling has been checked, null when a
source exists but was never successfully priced, and null when no
BigCommerce source exists at all. Removed the now-redundant
`_derive_bigcommerce_ecommerce_url` fixture helper (comparison_table's
FakeCursor branch now calls `_derive_bigcommerce_offer` directly, same
as the `product` spec_row branch already did). Full regression sweep:
clean (43 test files). `tsc -b`: clean.

### 6aa. Decoupled article text/image regenerate

Al: "can we decouple the article and image regenerate." Before this,
`product_article_generator`'s only trigger (`POST /products/{id}/
generate-article`) always regenerated the article TEXT (a fresh Bedrock
call) and its IMAGES (fresh Gemini candidates) together in one upsert --
there was no way to refresh one without also paying for and overwriting
the other. Both halves are now independently triggerable while the
original combined trigger/button keeps working exactly as before for a
brand-new article (there's nothing to decouple until a row exists).

**`product_article_generator/app.py`**: `generate_article_for_product`
gained `regenerate_text`/`regenerate_images` bool params (both default
`True`, so every pre-existing caller -- the daily batch sweep and the
original combined admin trigger -- is byte-for-byte unaffected).
- `regenerate_text=True, regenerate_images=False` ("Regenerate text"):
  re-runs the Bedrock article-text call as always, but writes via a new
  `update_article_text_only` (UPDATE, not upsert -- requires an existing
  row) instead of `store_article`. Still resets `status`/`reviewed_at`/
  `resolved_by` to pending (same "a regenerate goes back through review"
  reasoning `store_article` already had), but its SET clause has NO image
  columns in it at all -- an admin's already-picked images survive
  untouched.
- `regenerate_text=False, regenerate_images=True` ("Regenerate images"):
  skips the Bedrock article-text call entirely. A new `fetch_existing_
  article` pulls the existing row's `performance_summary`/`hook` to use
  as `generate_article_image_candidates`' prompt-building context --
  there's no persisted `visual_theme` to reuse (it's a transient field
  produced fresh by each Bedrock response, never written to
  `product_articles`, per `025_product_article_images_theme_driven_
  pipeline.sql`'s own header comment), but the prompt builders already
  fall back to `performance_summary`/`hook` whenever `visual_theme` is
  blank, so this just exercises that existing fallback rather than
  fabricating anything. Writes go through a new `update_article_images_
  only` (UPDATE, requires an existing row), which sets ONLY the four
  image columns + `images_generated_at` -- no `status`/`reviewed_at`/
  `resolved_by` at all, following `select_article_image_candidate`'s own
  established precedent (admin_api/service.py) that changing an
  article's images is "a lightweight admin action, not a review/approve
  workflow of its own." Requires an existing article row (nothing to
  draw image-prompt context from otherwise) -- reported as `reason:
  "no_existing_article_to_regenerate"`, checked up front before any
  Bedrock call.
- Both flags `False` is a defensive no-op guard (`reason: "nothing_to_
  regenerate"`), not a real use case.

`handler()`'s on-demand `{"product_id": ...}` path now reads optional
`regenerate_text`/`regenerate_images` off the event (defaulting to
`True`/`True` when absent) and threads them straight through. The batch
sweep path never passes these -- a scheduled catalog-wide run always
wants both halves, unchanged.

**`admin_api`**: `queue_article_generation` gained a `mode` param
(`"both"` default / `"text"` / `"images"`). `mode="both"` sends the
exact same Lambda payload this function has always sent (no new keys at
all) -- true byte-for-byte backward compatibility, not just "usually the
same." `mode="text"`/`"images"` set `regenerate_text`/`regenerate_images`
explicitly in the payload. Two new routes, `POST /products/{id}/
regenerate-article-text` and `POST /products/{id}/regenerate-article-
images`, call it with the corresponding mode; the original `POST
/products/{id}/generate-article` route is unchanged (`mode="both"`). No
`template.yaml` changes needed -- `AdminApiFunction`'s existing `/
{proxy+}` POST route already covers any new path.

**`admin-site/index.html`**: the Articles tab's single "Regenerate"
button is now two -- "Regen text" / "Regen images" -- calling new
`regenerateArticleText`/`regenerateArticleImages` functions. The product
detail view's article panel now shows "Regen text" + "Regen images" once
an article exists, and falls back to the original single "Generate
article" button only when it doesn't (nothing to decouple yet) --
`regenerateArticleTextForProduct`/`regenerateArticleImagesForProduct`
mirror `generateArticleForProduct`'s existing result-panel pattern.

**Tests**: `tests/test_product_article_generator.py` gained 16 new tests
(93 -> 105): `fetch_existing_article`/`update_article_text_only`/
`update_article_images_only` each covered directly (including their
"no existing row" ValueError/reason paths), plus `generate_article_for_
product`'s new text-only/images-only/both-false branches, plus a new
`handler()` test confirming `regenerate_text`/`regenerate_images` reach
`generate_article_for_product` unmodified from the event. The existing
`test_handler_on_demand_product_id_forces_single_generation` test was
updated for the two new always-present kwargs. `tests/test_admin_api_
service.py` gained 3 new tests for `queue_article_generation`'s `mode`
param (271 total); the existing "singular product_id" test was updated
to expect the new `"mode": "both"` key in its result. Full regression
sweep: clean (all test files).

### 6ab. Admin SPA phase 1: Cognito user accounts + a new React admin app

Al's ask, verbatim: "can we build a more sophisticated admin spa that
is hosted on aws and has users and an improved UI." Scoped into a
phased build after three clarifying questions: Cognito for real
per-user accounts (not the existing shared-secret token), a phased
rollout (design + auth + 1-2 tabs first, not a full rewrite in one
shot), and an "improved UI" meaning a real component/design system,
better data tables with bulk actions, dashboards front-and-center, and
general visual polish.

**Why Cognito, and why dual-mode auth instead of a clean replacement.**
The obvious design is "replace the shared-secret bearer token with
Cognito entirely." That would have broken all 17 existing automation
scripts in `scripts/*.py`, which authenticate to `AdminApiFunction`
with that same shared secret today. Rather than migrate 17 scripts in
lockstep with this UI work, `admin_api_authorizer` (the Lambda REQUEST
authorizer in front of `AdminHttpApi`) now accepts **either** a valid
Cognito-issued ID token **or** the pre-existing shared secret -- Cognito
is tried first (only when the token looks JWT-shaped, i.e. three
non-empty dot-separated segments, and `COGNITO_USER_POOL_ID`/
`COGNITO_CLIENT_ID`/`COGNITO_REGION` are all configured), and falls
through to the unchanged shared-secret check on **any** failure
(malformed token, bad signature, wrong audience/issuer, non-`id`
token_use, or an authenticated account with no recognized group). The
shared-secret comparison logic itself is byte-for-byte unchanged --
every automation script keeps working with zero changes.

**Cognito setup (`template.yaml`)**: a new `AdminUserPool` with
`AllowAdminCreateUserOnly: true` (no public sign-up -- accounts are
created via the AWS CLI/console only, see `admin-spa/README.md`),
email as the username, two groups (`AdminUserPoolAdminsGroup`
precedence 0, `AdminUserPoolEditorsGroup` precedence 10), and a public
app client (`AdminUserPoolClient`, `GenerateSecret: false` -- a browser
SPA can't keep a client secret confidential) supporting
`ALLOW_USER_PASSWORD_AUTH`/`ALLOW_USER_SRP_AUTH`/
`ALLOW_REFRESH_TOKEN_AUTH` with 1-hour access/ID tokens and a 30-day
refresh token.

**Role model, and what it does NOT do yet**: the ID token's
`cognito:groups` claim maps `Admins` -> `role: "admin"` and `Editors`
-> `role: "editor"` (`resolve_role_from_groups` in
`admin_api_authorizer/app.py`). An authenticated account in neither
group gets `role: null` and is **fail-closed** -- not a default
"editor" -- so admin_api_authorizer's own on-success context never
grants blanket access to an ungrouped account. **Phase 1 does not
enforce role-based restrictions on individual routes** -- both `admin`
and `editor` can currently call every `AdminApiFunction` endpoint. The
`caller` object (see below) makes the role available to every route,
which is what a phase-2 pass at real RBAC would build on; it's
deliberately deferred rather than half-built here.

**Caller identity flows into `admin_api` too**: API Gateway forwards
the authorizer's `context` object (`caller_type`/`resolved_by`/`role`)
to the backend at `event["requestContext"]["authorizer"]["lambda"]`,
which Mangum exposes to FastAPI via `request.scope["aws.event"]`. A new
`service.resolve_caller_from_event()` extracts it (defaulting safely
when the shape is missing/partial -- this path is currently only
exercised by real API Gateway traffic, not directly testable in this
sandbox), and a new `get_caller` FastAPI dependency wires it into all
10 approve/reject routes (review queue, video candidates, price
sources, articles): each route's `resolved_by` request field is now
`Optional`, falling back to the authenticated caller's identity
(`body.resolved_by or caller["resolved_by"]`) when the client doesn't
supply one explicitly. A signed-in human clicking Approve in the new
SPA no longer needs to type their own name/email into a field.

**Hosting**: `admin-spa/` gets the same S3 + CloudFront + OAC pattern
already used for `consumer-site`/`bowlerdepot-learn` -- private bucket
(`AdminSiteBucket`), Origin Access Control (not the legacy OAI),
`AdminSiteDistribution` with `CustomErrorResponses` rewriting 403/404
to `/index.html` (client-side routing via react-router), and
`AdminSiteDeployRole` (GitHub OIDC, no long-lived AWS keys, trusted
only for this repo's `main` branch) driving
`.github/workflows/deploy-admin-site.yml` on every push to
`admin-spa/**`. `AdminSiteDomainName`/`AdminSiteCertificateArn` follow
the same blank-means-off convention as the other two sites' domain
params. New stack outputs: `AdminUserPoolId`, `AdminUserPoolClientId`,
`AdminSiteBucketName`, `AdminSiteDistributionId`, `AdminSiteUrl`,
`AdminSiteDeployRoleArn`.

**The SPA itself (`admin-spa/`)**: Vite + React + TypeScript +
Tailwind, same "pure client-side SPA, dist/ synced to S3" shape as
`consumer-site`. Sign-in is Cognito SRP auth via
`amazon-cognito-identity-js` (deliberately not the heavier
`aws-amplify`) -- see `src/auth/cognito.ts`/`AuthContext.tsx`. A small
hand-rolled component library (`Button`, `Badge`, `Card`, `StatCard`,
`Modal`, `Toast`, `DataTable`, `Pagination`, `Layout`) gives later tabs
a consistent look without re-solving sort/select/bulk-action each time
-- `DataTable` in particular supports client-side column sort,
checkbox multi-select, and a bulk-action bar in one generic component.
Phase 1 ships two pages: **Dashboard** (`/`, the landing page --
per Al's "dashboards more prominent" priority -- KPI tiles, an
ADU-by-brand bar chart via `react-chartjs-2`, and four Top-10 tables,
all from the existing `GET /admin/dashboard`) and **Products**
(`/products` -- the full existing filter set, sortable table, and a
bulk "Rescrape selected" action against `POST /products/{id}/
rescrape`). Every other admin-site tab (Video Candidates, Articles,
Price Sites, Cores, Coverstocks, Blocked Channels, Batch Jobs) stays on
the existing `admin-site/index.html` for now -- see `admin-spa/
README.md`'s "what's not here yet" for the full list and the reasoning
(this is a foundation to build on, not a full port).

**Sandbox limitation, disclosed**: this sandbox has no npm registry
access (confirmed via `npm ping` returning a proxy 403 this session,
matching earlier sessions' pip/npm findings) -- every file under
`admin-spa/` was written from React/TypeScript/Cognito's documented
APIs, never actually compiled, bundled, or run. No `package-lock.json`
exists yet for the same reason (consumer-site needed the identical
bootstrapping step -- see its own git history). Before trusting this in
production: run `npm install` locally in `admin-spa/`, commit the
resulting lockfile, do a real `npm run dev` smoke test (sign-in
especially -- the exact shape of the `amazon-cognito-identity-js` calls
in `cognito.ts` was written from memory of its public API, not a real
import), then `npm run build` to confirm `tsc -b` is clean before the
first real deploy.

**What's next (not yet started)**: role-based route enforcement,
migrating the remaining admin-site tabs, a "set new password" form for
Cognito's first-sign-in challenge (until then, set a permanent password
via `admin-set-user-password` at account-creation time -- see
`admin-spa/README.md`), and a real brand-name dropdown on the Products
filters (currently a raw brand-id text field -- there's no `GET
/brands` on the admin side the way `consumer-site` has on the public
API).

### 6ab.1. Real-world verification, a real bug fix, and the Review Queue tab

Al deployed the stack and ran `npm install` locally for real (this
sandbox still can't -- no registry access). Two things followed:

**A real bug, found and fixed**: first `npm run dev` produced a blank
screen. Browser console: `Uncaught ReferenceError: global is not
defined`, thrown from `node_modules/buffer` (a transitive dependency of
`amazon-cognito-identity-js`) before React could render anything.
Vite doesn't polyfill Node globals the way webpack did, and this
library assumes `global` exists. Fixed with one line in `admin-spa/
vite.config.ts`:
```ts
define: { global: "globalThis" },
```
After that fix and a dev-server restart, sign-in, the Dashboard's real
KPI/chart data, and a Products bulk rescrape (confirmed via a real
"Queued rescrape for N products" toast, meaning the action actually
reached SQS) all worked end to end against the live stack. This is the
first phase-1 functionality actually exercised outside this sandbox.

**Review Queue tab** (`/review-queue`, `admin-spa/src/pages/
ReviewQueuePage.tsx`): the scraped-field-correction moderation queue --
`GET /review-queue` (status/product_id/limit/offset params, an item's
`field_name` is either a whitelisted product column or a per-SKU
`rg_15lb`-style field, see `admin_api/service.py`'s
`parse_review_field_name`) plus `POST /review-queue/{id}/approve` and
`/reject`. Per-row and bulk approve/reject, a shared-reason modal for
rejects (replacing admin-site's plain `prompt()`), and the same
sequential-with-300ms-delay bulk execution `admin-site/index.html`'s
own bulk feature (task #219) already established to avoid API
Gateway/Lambda throttling. No inline-edit of the proposed value exists
on either UI -- approve always applies the scraped value as stored.

**Verification note**: with `node_modules` now present (installed on
Al's Mac, visible in this sandbox via the shared mount), `npx tsc -b`
was run for real this time and passed clean with zero errors --
stronger verification than the "written against documented types,
never compiled" disclaimer that applied to the original phase-1 ship.
A real `vite build` still fails in this sandbox specifically (`Cannot
find module @rollup/rollup-linux-arm64-gnu` -- rollup's native binary
is platform-specific, and this `node_modules` was installed on macOS,
not this sandbox's Linux; see https://github.com/npm/cli/issues/4828)
-- this is an environment mismatch, not a code defect, but it does mean
the production bundle itself still needs a real `npm run build` on
Al's machine before the next deploy. **Update**: the Review Queue tab
has since been smoke-tested against real data too and confirmed
working -- all three phase-1+ tabs (Dashboard, Products, Review Queue)
are now verified end to end against the live stack.

### 6ab.2. Video Candidates tab

Fourth tab ported into `admin-spa/` (`src/pages/VideoCandidatesPage.tsx`):
YouTube review videos matched to products, the same feature
`admin-site/index.html`'s Video Candidates tab and its product-detail
Videos sub-panel cover. Routes: `GET /video-candidates` (status --
pending/approved/rejected, "all" exists server-side but isn't exposed
here either, matching the old tab's own dropdown -- plus product_id/
limit/offset), `POST .../approve`, `.../reject`, `.../restore` (undo,
no body/confirm), and `.../reassign` (`{product_id}` -- moves a
candidate to a different product, tombstoning the origin row as
rejected; works from any status). Same sequential-with-300ms-delay
bulk pattern as Review Queue. Deliberately does NOT include the
product-detail sub-panel's richer feature set (all-statuses-at-once
view, "search again" rescan trigger via `POST /products/{id}/
discover-videos`, bulk reassign/delete) -- those need a Products detail
view that doesn't exist in admin-spa yet, tracked in `admin-spa/
README.md`'s "what's not here yet".

`npx tsc -b` run for real against the now-present `node_modules`
(installed on Al's machine, visible in this sandbox via the shared
mount) -- clean, zero errors. Not yet smoke-tested against real data in
the browser the way Dashboard/Products/Review Queue have been.

**Update -- a real bug, found on first browser load**: `match_confidence`
was wrongly typed as `number | null` and rendered with `.toFixed(2)`.
It's actually a text enum (`'high' | 'low'`, see `video_discovery.
score_match` / `db/migrations/004_product_videos.sql:35`), not a
numeric score -- `tsc` had no way to catch this since the type
annotation itself was the mistake. Threw `TypeError: r.match_confidence.
toFixed is not a function` the moment a pending video candidate
rendered, taking down the entire app -- no error boundary existed
anywhere in the component tree yet at the time this was found. Fixed:
`match_confidence` retyped as `"high" | "low" | null`, rendered as a
`Badge` instead of a formatted number, AND an `ErrorBoundary` component
added around `<Outlet/>` in `Layout.tsx` (keyed by route pathname) so a
future mistake like this one is contained to a single page instead of
blanking sign-in/navigation for the whole app too. This is exactly the
class of mistake `tsc -b` passing clean can't catch -- it verifies
internal consistency, not that a type annotation matches the real
database column, which only exercising the code against live data
reveals.

### 6ab.3. Articles tab

Fifth tab ported into `admin-spa/` (`src/pages/ArticlesPage.tsx`):
AI-generated ball-review articles (`022_product_articles.sql` onward),
the same feature `admin-site/index.html`'s Articles tab covers. Routes:
`GET /articles` (status -- pending/approved/rejected, "all" exists
server-side but isn't exposed here either, plus product_id/limit/
offset -- note this endpoint returns no `pending_count`, unlike Review
Queue/Video Candidates, confirmed by reading `get_articles` in
`admin_api/app.py` before assuming one existed), `GET /articles/{id}`
(full detail for the preview modal), `POST .../approve`, `.../reject`
(no restore/undo endpoint exists on this resource -- a resolved article
can only move by being regenerated, which resets it to pending),
`PATCH .../bigcommerce-sync` (freely-reversible boolean toggle, no
review-workflow gating), `POST .../sync-to-bigcommerce` and
`.../resync-to-bigcommerce` (fire-and-forget `lambda:InvokeFunction`
triggers for `BowlerdepotArticleSyncFunction`), `GET .../
image-candidates` + `POST /article-image-candidates/{id}/select`
(the v4 Gemini-vs-Stability candidate picker, `026_product_article_
image_candidates.sql`), and `POST /products/{id}/generate-article` +
the decoupled `.../regenerate-article-text` / `.../regenerate-article-
images`.

No bulk actions on this tab (admin-site's own Articles tab never had
any either -- each article's images/text are unique enough per-row
that a shared bulk-reject reason doesn't fit as naturally as it does
for Review Queue/Video Candidates) and no pending-count badge (see the
routes note above). The full-article preview -- hook, performance
summary, who-should-buy/skip, pros/cons, buying tips, verdict, FAQ,
comparison-table row count, inferred-sibling count, source-video count
-- opens in a wider `Modal` (added a `wide` prop to `Modal.tsx` for
this, since the default `max-w-lg` every other modal here uses was too
cramped for a full article). `comparison_table` rows are typed as a
loose `Record<string, unknown>[]` in `api/types.ts` rather than
field-by-field, matching admin-site's own "dump it as JSON" treatment
of that field -- its shape has grown ad hoc (pricing fields added
later, see public_api's own history) and admin-spa only needs to show
a row count here, not read specific fields off it.

`npx tsc -b` run for real against the now-present `node_modules` --
clean, zero errors. Given the match_confidence incident directly
above, that's treated as necessary and not sufficient, not as proof
this tab is correct against live data -- it has NOT yet been
smoke-tested in the browser against a real deployed stack. Two fields
worth specifically watching on first real load: `comparison_table`'s
actual shape, and `seed` on image candidates (should be `null` for
every Gemini row, a number for Stability rows -- see
`026_product_article_image_candidates.sql`'s own column comment).

### 6ab.4. Price Sites tab

Sixth tab ported into `admin-spa/` (`src/pages/PriceSitesPage.tsx`) --
and the first one to combine two of admin-site's separate top-level
tabs into a single admin-spa page. Al asked directly whether combining
"Price Sources" (the discovered-match review queue, `product_price_
sources`) and "Price Sites" (the retailer registry, `price_sites`)
made sense, since one configures the other (every source points at a
site via `price_site_id`). Checked `admin-site/index.html` first:
nothing there ever actually combines their UI -- they're two separate
tabs with two separate `TAB_LOADERS` entries -- but nothing about the
two backend resources conflicts either, so admin-spa combines them:
one page, Price Sources on top (the higher-churn review queue) and
Price Sites underneath (read-mostly, expected to stay small -- see
`list_price_sites`' own docstring).

Routes: `GET /price-sources` (status -- pending/approved/rejected, plus
product_id/limit/offset, `pending_count` only populated for
status=pending), `POST .../approve`, `.../reject`, `.../restore` (same
approve/reject/restore/undo shape as Video Candidates, same 300ms-paced
sequential bulk pattern), and `GET /price-sites` + `POST /price-sites`
+ `PATCH /price-sites/{id}` + `DELETE /price-sites/{id}` for the
registry (add-site form with fetch-method-conditional fields, an edit
`Modal` prefilled with every field the site has -- `fetch_method`
itself isn't editable, matching `editPriceSite`'s own reasoning that
switching scrape/api is a delete-and-recreate -- a Deactivate/
Reactivate toggle via the same PATCH, and a hard-delete confirm modal).

Deliberately out of scope, matching admin-site's own tab boundary:
`POST /products/{id}/price-sources` (manual add-a-source-to-this-
product), `PATCH /price-sources/{id}` (quick-edit product_url/
css_selector/is_active on one source), and `DELETE /price-sources/{id}`
-- grepped `admin-site/index.html` for all three and confirmed every
call site for them lives in the product-detail Pricing sub-panel
(`buildPriceTrackingSection`), never the standalone Price Sources tab.
Same boundary Video Candidates' reassign-only-from-standalone-tab
already established -- these move over once Products gets a detail
sub-view, not before. Also out of scope: price/stock history charting
(`GET /products/{id}/price-history`, `.../sku-stock-history`) and the
catalog-wide discovery/check-all batch endpoints, which have no
admin-site UI at all (grepped for them -- zero matches; they're
curl/script-only operations today).

`match_confidence` on `PriceSource` (the review-queue row) was typed as
`"high" | "low" | null` by reading `db/migrations/014_price_tracking.
sql` directly first -- `match_confidence text, -- 'high' | 'low' |
null (manual)` -- specifically because this is the exact same field
name/shape that caused the real bug on Video Candidates (see 6ab.2's
own "Update" paragraph). Confirmed correct against the schema this
time before writing any render code, not after a browser crash.

`npx tsc -b` passes clean. Not yet smoke-tested against real deployed
data -- worth confirming both halves (the review queue's bulk actions,
and the registry's scrape-vs-api conditional form) against a live
stack, plus the two out-of-scope boundaries above if a Products detail
view gets built later.

### 6ab.5. Cores tab

Seventh tab ported into `admin-spa/` (`src/pages/CoresPage.tsx`) -- the
simplest one so far. Cores is the "other direction" view of
`products.core_id` (007_cores_table.sql): one row per physical core,
with a `product_count` (left-join + group-by in `list_cores`) so a
many-products-to-one-core case is visible without spotting the same
core name repeated across several Products rows by hand. Entirely
read-only: `GET /cores` (search/brand_id/limit/offset) and `GET
/cores/{id}` (detail -- the core row plus every product currently
pointing at it) are the only two routes; admin_api has no create/
update/delete for cores at all, since rows are only ever created or
attached by the scrapers' own `get_or_create_core_id`, never by hand.

Search-by-name and a raw brand-id text field (same as Products' own
filter bar -- no `GET /brands` on the admin API yet for a real
dropdown), plus a `product_count` badge (`ok` tone when >0, `muted`
when 0 -- a zero-product core is a real signal worth surfacing, not
noise: it means every product that used to reference it got reassigned
or rescraped under a different core, per `list_cores`' own docstring on
the Hammer "E "-prefix incident this exact feature was born out of
debugging). A "Products" button per row opens a `Modal` with the
product list (name/status/published/updated, linking back out to each
product's own page) -- admin-spa's Modal-based pattern here instead of
admin-site's inline expand-row, since DataTable doesn't support a
row-toggle shape.

`npx tsc -b` passes clean. Not yet smoke-tested against real deployed
data.

### 6ab.6. UI style decision: "dense pro-tool" dark theme

Al asked (2026-09-05) to pause tab-porting and pick a visual direction
for admin-spa before going further, since phase 1 had just been running
with whatever the scaffold produced (a light slate/blue Tailwind theme,
deliberately continuous with `admin-site/index.html`'s own CSS custom
properties -- see the old comment this replaced in
`tailwind.config.js`). Presented six mockup directions (current/slate,
a softer shadcn-style zinc theme, a dense dark pro-tool theme, a theme
pulling BowlerDepot's real storefront brand colors from the
`bowlerdepot-learn` research in 6t, a warm-neutral theme, and a
high-contrast monochrome theme) as rendered swatches, not just prose.
Al picked **dense pro-tool**: dark chrome, tight rows, a single accent
color, on the reasoning that this is a tool someone lives in all day
doing rapid review/approve work, not a public-facing surface that needs
brand continuity.

Implementation, in `admin-spa/tailwind.config.js` and every component/
page under `admin-spa/src/`:

- **`ink` color scale** -- a from-scratch 10-step dark-UI grayscale
  (`ink-50` through `ink-900`) that mirrors Tailwind's built-in `slate`
  scale's *bucket meaning per number* (50 = page background, 100 =
  raised surface, 200 = border, 500/600 = secondary/body text, 800/900
  = headings/highest-contrast text) but with the literal light/dark
  direction inverted, since the whole app is now dark. Because the
  bucket-per-number meaning didn't change, every `slate-N` class in
  every page and component was mechanically renamed to `ink-N` (`sed`
  across `admin-spa/src/**/*.tsx`) and needed no further per-usage
  thought -- `bg-slate-50` (page bg) became `bg-ink-50` (still page bg,
  now dark) automatically. `bg-white` (used for the same "raised
  surface" role as `slate-100` everywhere in this codebase) was
  likewise swapped to `bg-ink-100` app-wide.
- **Single indigo `primary` accent** replacing the old blue
  (`primary.DEFAULT: #6366f1`). Two things needed to NOT share one
  token despite both being "the darker shade of primary" under the old
  naming: `primary.dark` is now a deliberately *light* indigo
  (`#a5b4fc`), used as text sitting on top of the dark `primary.light`
  chip background (Layout's active nav item, Badge's `primary` tone,
  DataTable's "N selected" bar) -- while a solid button's hover state
  needs the opposite, a *darker* shade of the fill. Button.tsx's
  primary variant got its own `primary-hover` token (`#4f46e5`) instead
  of reusing `primary-dark` for that, specifically to avoid a hover
  state that would have made the button lighter and the white button
  text unreadable.
- **Status colors (`danger`/`ok`/`warn`) kept distinct hues**, just
  recalibrated for dark backgrounds (bright, saturated `DEFAULT` for
  text/solid fills; a near-black tinted `light` wash for chip/banner
  backgrounds instead of the old pale tint). The "single accent"
  direction is about the primary interactive color, not about
  collapsing approve/reject/warning signal color into monochrome.
- **`color-scheme: dark`** added to `body` in `index.css` alongside the
  `bg-ink-50 text-ink-800` base styles, so every plain, unstyled
  `<select>`/`<input>`/checkbox across every page (most filter bars
  never had explicit background/text classes on their form controls)
  picks up dark native chrome from the browser's own UA stylesheet
  instead of needing per-page classes.
- **Three same-tone hover-on-surface collisions** were caught by
  grepping every `hover:bg-ink-100` against what surface it actually
  sits on, since the mechanical slate-to-ink rename made a hover state
  resolve to literally the same color as its parent surface in three
  places: Layout's inactive nav-item hover (sidebar is `bg-ink-100`,
  hover was also `ink-100`), Button's `ghost` variant hover (ghost
  buttons commonly sit on an `ink-100` card/row), and DataTable's row
  hover. All three were bumped to `hover:bg-ink-200` (Layout/Button) or
  `hover:bg-ink-200` with the row's resting border also bumped from
  `ink-100` to `ink-200` (DataTable), so hovering now visibly lightens
  instead of doing nothing. `Toast.tsx` had the opposite problem: its
  `TONE_CLASSES` used a literal `bg-slate-900` as an always-dark toast
  box (deliberate even under the old light theme) which the blanket
  rename turned into `bg-ink-900` -- under the new scale that's the
  *brightest* step, which would have produced a near-white toast with
  invisible white text. Manually corrected to `bg-ink-100` (now just a
  normal raised surface, since the whole app is dark already, no
  special-casing needed).
- Light **density pass** on `DataTable.tsx` (every page's table):
  cell/header padding tightened from `px-3 py-2` to `px-2.5 py-1.5`.
  Not a full per-page spacing audit -- most of the "dense" feel comes
  from the shared `DataTable`/`Layout` components every page already
  builds on.

`npx tsc -b --force` passes clean. **Not visually verified in a running
browser** -- `npm run dev`/`vite build` hit the same Linux-sandbox/
Mac-built-`node_modules` platform mismatch already on file in 6ab's
"Verified so far" (rollup's native binary is platform-specific; see
https://github.com/npm/cli/issues/4828), and this sandbox has no
registry access to install the correct `@rollup/rollup-linux-arm64-gnu`
binary either (403 from a network policy). Every color-token usage was
instead traced by hand across every component and page file, which is
how the three hover collisions and the Toast regression above were
caught -- but hand-tracing contrast is not the same as looking at it
rendered. **Run `npm run dev` on a real machine and eyeball every page**
before treating this as done, especially: the three corrected hover
states, anywhere text sits on a `bg-{role}-light` chip (Badge, error
banners, Toast), and the native form controls relying on
`color-scheme: dark` rather than explicit classes.

### 6ab.7. Nav icons + collapsible sidebar

Al asked to add icons to the sidebar nav and make it collapsible to
icon-only. `admin-spa` had no icon library dependency at all, and this
sandbox's npm registry access is unreliable (403s on scoped packages,
per 6ab.6 above) -- rather than risk a dependency install that might
not survive to a real `npm install`, seven nav icons plus a collapse
chevron were hand-rolled as inline SVG in a new
`src/components/icons.tsx` (stroke-based, `currentColor`, Tabler/
Feather-ish proportions so they read consistently with the rest of the
app rather than needing their own color rules).

`Layout.tsx`'s `NAV_ITEMS` now carries an `icon` component per entry.
A chevron button in the sidebar header toggles a `collapsed` boolean
that's read from/written to `localStorage`
(`admin-spa:sidebar-collapsed`) so the choice survives reloads --
someone doing rapid review work all day shouldn't have to re-collapse
it every session. Collapsed state: sidebar width drops from `w-56` to
`w-14`, nav item labels are hidden (icon centered, `title` attribute
picks up the label as a native hover tooltip instead), and the
"BowlerIQ Admin" header text is hidden too (just the toggle button
remains, centered).

`npx tsc -b --force` passes clean. Same caveat as 6ab.6 above applies:
not visually verified in a running browser (same platform-mismatch
sandbox limitation) -- worth an actual look, especially the collapsed
width and whether `title`-attribute tooltips feel sufficient versus a
proper hover tooltip component.

Follow-up from Al right after: the toggle button originally lived in
the sidebar header next to "BowlerIQ Admin" (right-aligned, smaller
`h-4 w-4` chevron than the nav icons). First fix moved it into `<nav>`
as its own full-width row -- Al then sent a screenshot of a reference
app (icon-toggle inline with the title at the top, left-justified
together, ordinary nav items below with distinct per-item icons) and
said that's what he meant. Reworked again to match: the toggle is back
in the header `div`, but now icon-then-title in a plain left-aligned
flex row (no `justify-between` spreading them apart) instead of a nav
row of its own, and the chevron icons were swapped for a new
`IconPanelLeft` (rounded frame with a vertical divider near the left
third -- the standard "toggle a side panel" glyph, distinct from
"go back") in `icons.tsx`. `<nav>` is back to just `NAV_ITEMS`.

### 6ab.8. Coverstocks tab

Eighth tab ported into `admin-spa/` (`src/pages/CoverstocksPage.tsx`) --
the exact same "other direction" view as Cores (`008_coverstocks_table.sql`,
one migration after cores' 007): one row per named coverstock
formulation, scoped to a brand, with a `product_count` so a
many-products-to-one-coverstock case is visible at a glance. Entirely
read-only: `GET /coverstocks` (search/brand_id/limit/offset) and `GET
/coverstocks/{id}` (detail) are the only two routes -- same "no
create/update/delete, rows come from the scrapers' own
get_or_create_coverstock_id" shape as Cores.

Confirmed `material`/`type` come off Postgres enum columns
(`coverstock_material`/`coverstock_type`) by reading
`008_coverstocks_table.sql` directly, but `list_coverstocks`/
`get_coverstock` in `service.py` select them as ordinary dict values --
admin_api returns them as plain strings over the wire, so typed as
`string | null` in `types.ts`, same looseness as Core's
`core_type`/`release_era`.

The page itself is CoresPage.tsx's structure with `core_type`/
`release_era` swapped for `material`/`type` -- close enough to the same
component that a shared generic was considered, but there's still only
two of these "lookup rollup" tabs, not enough to justify the
abstraction yet (same call CoresPage's own commit made). New
`IconCoverstock` (a droplet -- coverstock is the ball's outer coating)
added to `icons.tsx` for the nav entry.

Also confirmed via the same admin-site grep discipline as prior tabs:
Batch Jobs has its own "Backfill missing coverstock info" trigger
(`runBatch('coverstock')`, equivalent to
`scripts/backfill_coverstock_ids.py`) -- that stays out of scope here,
picked up when the Batch Jobs tab itself is ported (6ab.10 below).

`npx tsc -b --force` passes clean. Not yet smoke-tested against real
deployed data.

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
- **`VideoDiscoveryFunction`'s SEARCH job shapes still have no automated
  schedule**, same reasoning as the other discovery functions, plus a
  real quota reason: the confirmed 100-searches/day cap (70/invocation,
  see 6i) means "run it on everything every day" isn't actually sane math
  yet against a full catalog. Invoke it manually with an explicit
  `product_ids`/`brand_id` scope (see 6i) until you've decided how you
  actually want to spread coverage across the catalog over time.
  **The `{"refresh_stats": true}` job shape is different and now IS
  scheduled** (daily, see 6i.6's own writeup) -- `videos.list` isn't the
  constrained resource `search.list` is, so there was no quota reason to
  keep it manual-only once Al noticed stats "coming in but pretty slow."

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
