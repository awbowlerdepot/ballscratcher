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
docstring).

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

Two-stage pipeline as of the "split the architecture" change: approving a
candidate publishes to `VideoSummarizeQueue`, consumed by the non-VPC
`video_transcript_fetcher` (fetches the transcript, no DB access), which
publishes its result to `VideoTranscriptResultQueue`, consumed by
`video_summarizer` (DB write + Bedrock call, no YouTube fetch anymore).
See src/video_transcript_fetcher/app.py's module docstring for why this
split happened -- real, live-tested evidence pointed at YouTube's
consumer-facing surface (`www.youtube.com`) getting different treatment
from this stack's VPC/NAT-gateway IP than from a residential IP; moving
the YouTube-facing fetch off VPC is the (unverified as of this deploy) fix
being tried.

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
5. Confirm it made it through both stages -- `video_transcript_fetcher`
   (off `VideoSummarizeQueue`) then `video_summarizer` (off
   `VideoTranscriptResultQueue`) both run async, so check back after a
   minute or two:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" "$ADMIN_API_URL/video-candidates/<video-id>"
   ```
   Expect either a real `transcript`/`summary`, or `transcript_note` set to
   `no_captions_available` (a real, expected outcome for videos without
   captions, not a bug). If neither appeared after a few minutes, check
   CloudWatch logs for `bowling-scraper-video-transcript-fetcher` first
   (that's the function actually talking to YouTube now), then
   `bowling-scraper-video-summarize-dlq` and
   `bowling-scraper-video-transcript-result-dlq` -- same
   first-stop-for-failures convention as every other DLQ in this project.

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

Cron, once a day (put the env vars in a `chmod 600` wrapper script rather
than the crontab itself, so the token isn't sitting in plaintext in
`crontab -l` -- and call the venv's `python3` directly by full path, since
cron doesn't run your shell's `source`d activation):
```
0 7 * * * /home/pi/run_transcript_fetcher.sh >> /var/log/bowling-transcript-fetcher.log 2>&1
```
where `run_transcript_fetcher.sh` contains:
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

Cron, same pattern as 6j -- put env vars in a `chmod 600` wrapper script
rather than the crontab itself:
```
0 7 * * * /home/pi/run_browser_transcript_fetcher.sh >> /var/log/bowling-transcript-fetcher-browser.log 2>&1
```

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
