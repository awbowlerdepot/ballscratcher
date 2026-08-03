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

## 2. Run the four migrations, in order

```bash
psql "$DATABASE_URL" -f db/migrations/001_init_schema.sql
psql "$DATABASE_URL" -f db/migrations/002_add_woocommerce_netsuite_platforms.sql
psql "$DATABASE_URL" -f db/migrations/003_date_tracking_and_bowwwl.sql
psql "$DATABASE_URL" -f db/migrations/004_product_videos.sql
```

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
real, hard quota this key is subject to: search.list costs 100 units/call
against a default 10,000 units/day project quota -- ~100 searches/day, not
adjustable from this template (see src/video_discovery/app.py's module
docstring). Separately, `video_summarizer` calls Bedrock -- in the AWS
console, go to Bedrock -> Model access and request access to whatever
model `BedrockModelId` is set to (default
`anthropic.claude-3-5-haiku-20241022-v1:0`); an un-granted model fails at
invoke time, not deploy time, so do this before the first real approval.

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
| `SitemapUrl` | No | Defaults to Brunswick's real sitemap; only override for a second Craft-CMS stack (Radical/DV8) |
| `UrlPathPattern` | No | Defaults to Brunswick's real path pattern; same caveat |
| `SwagCategoryUrl` / `SwagSitemapUrl` | No | Default to SWAG's real values |
| `SwagBrandId` | Only if enabling SWAG | SWAG's id from step 4, else leave blank |
| `MotivCurrentCategoryUrl` / `MotivRetiredCategoryUrl` | No | Default to MOTIV's real values |
| `MotivBrandId` | Only if enabling MOTIV | MOTIV's id from step 4, else leave blank |
| `CommercebuildCategoryUrl` | No | Defaults to the real stormbowling.com bowling-balls category URL |
| `StormBrandId` / `RotoGripBrandId` / `Global900BrandId` | Only if enabling commercebuild | The three ids from step 4, else leave blank (a blank id makes `CommercebuildUrlDiscoveryFunction` skip that brand entirely, see its module docstring -- you can enable them individually, not all-or-nothing) |
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

New this session, unverified against a real YouTube page or a real
Bedrock endpoint (this sandbox has no outbound network access -- see
src/video_discovery/app.py's and src/video_summarizer/app.py's module
docstrings for exactly what's unconfirmed). Test on a small, explicit
product list first, not the default "all published/current" scope --
each product costs one YouTube search.list call against the ~90/day cap.

1. Find a real `product_id` (or a few) via `GET /products?search=...` on
   the admin API (see 6a for the auth header shape).
2. Discover candidates for just those products:
   ```bash
   aws lambda invoke --function-name bowling-scraper-video-discovery \
     --payload '{"product_ids": ["<product-id>"]}' \
     --cli-binary-format raw-in-base64-out /tmp/out.json
   cat /tmp/out.json
   ```
3. Check what landed in `product_videos` as pending:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" "$ADMIN_API_URL/video-candidates?status=pending"
   ```
   Look at `match_confidence` -- 'low' entries are exactly the ones this
   approval step exists for (see README/service.py comments); don't
   rubber-stamp them without eyeballing the title/channel.
4. Approve one:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"resolved_by":"al@bringyourbest.co"}' \
     "$ADMIN_API_URL/video-candidates/<video-id>/approve"
   ```
5. Confirm it landed on `VideoSummarizeQueue` and was processed --
   `video_summarizer` runs async off that queue, so check back after a
   minute or two:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" "$ADMIN_API_URL/video-candidates/<video-id>"
   ```
   Expect either a real `transcript`/`summary`, or `transcript_note` set to
   `no_captions_available` (a real, expected outcome for videos without
   captions, not a bug). If neither appeared after a few minutes, check
   `bowling-scraper-video-summarize-dlq` -- same first-stop-for-failures
   convention as every other DLQ in this project.

## 7. Ongoing operations

- **Check the DLQs periodically** (`bowling-scraper-product-scrape-dlq`,
  `-pdf-parse-dlq`, `-image-process-dlq`, `-woocommerce-product-scrape-dlq`,
  `-netsuite-product-scrape-dlq`, `-commercebuild-product-scrape-dlq`,
  `-video-summarize-dlq`) -- a nonzero count means something's failing
  repeatedly, not just a transient blip (Lambda retries up to
  `maxReceiveCount` before landing there).
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
  the ~90-searches/day cap means "run it on everything every day" isn't
  actually sane math yet against a full catalog. Invoke it manually with
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
| Images look cropped wrong | Pull a few from `ImageBucket` and eyeball against `image_processor/app.py`'s bbox-detection assumptions |
| BowlerDepot reconciliation reports nothing, ever | `CUSTOM_FIELD_NAME_CANDIDATES` mapping is probably wrong for your real store -- see step 6h |
