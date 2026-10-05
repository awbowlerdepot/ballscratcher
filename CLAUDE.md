# BowlerIQ / brunswick-scraper

This is a serverless pipeline built with AWS SAM. It scrapes bowling ball manufacturer sites into Postgres (RDS) and adds video reviews, AI-written articles, AI product shots and retailer pricing on top. It also feeds several websites:

- the admin SPA at admin.bowleriq.com
- the BowlerDepot Learn site at learn.bowlerdepot.com
- the consumer site at data.bowleriq.com
- the marketing site at bowleriq.com
- the public API at api.bowleriq.io
- the partner API at api.bowleriq.io/partner/v1, a versioned, key-protected read-only contract for other platforms (runbook 6cj, docs/partner-api-v1.md)
- the image resizer at img.bowleriq.io

The GitHub repo is `awbowlerdepot/ballscratcher`, and everything deploys to the AWS region `us-west-1`.

## Where the context lives

- `DEPLOY_RUNBOOK.md` (about 16k lines) is the project history. It has a numbered section for every feature, incident and fix (6a, 6b, … 6bm), and "Troubleshooting quick reference" at the end. **Grep it for the topic before changing anything.** Don't read it top to bottom.
- `README.md` gives the architecture overview. The long module docstrings in each `src/*/app.py` explain *why* the code works the way it does, often quoting Al's original request. Read those before changing a module.
- Development used to happen in a Cowork task that has been retired. Its record ends at commit `4888b6f`.

## Layout

- `src/<function>/app.py` holds one Lambda per directory, each with its own `requirements.txt`. The runtime is Python 3.13.
  - Scrapers are grouped by site platform:
    - Craft CMS (`product_scraper`, `url_discovery`) for Brunswick, Radical and DV8
    - `commercebuild_*` for Storm and Roto Grip
    - `netsuite_*` for Motiv
    - `woocommerce_*` for Swag
    - `shopify_*` for Hammer, Track and Ebonite
  - The pipeline chains `url_discovery` → SQS → `product_scraper` → `pdf_parser` / `image_processor`. Every queue has a dead-letter queue (DLQ), and consumers use partial batch responses.
  - Video: `video_discovery` (YouTube search, 100 searches a day) → `video_transcript_fetcher` → `video_summarizer` (Bedrock Claude Haiku).
  - `product_article_generator` writes articles and generates images with Gemini.
  - The `bowlerdepot_*` functions reconcile and sync with bowlerdepot.com, which runs on BigCommerce. `price_checker` handles price-source discovery and the daily price checks.
  - `partner_api` serves the partner API. Its response shape is a locked contract: `models.py` defines it and `tests/test_partner_api_contract.py` diffs the OpenAPI schema against a committed snapshot. v1 may only gain optional fields or endpoints; anything else is a `/v2`. Its plotter-neighbor code is a verbatim copy of public_api's, kept in sync by `tests/test_plotter_neighbors_sync.py`.
- `admin_api` is FastAPI behind Mangum. Its logic lives in `service.py`, and `app.py` only does routing. `admin_api_authorizer` accepts either Cognito JWTs or a shared bearer token.
- `template.yaml` defines about 31 functions, the queues, buckets and CloudFront distributions. `samconfig.toml` holds the deploy parameters.
- `db/migrations/NNN_*.sql` holds numbered migrations (001–042), applied by hand with `psql` in order. Add a new migration with the next number.
- `scripts/` holds one-off backfill and rescrape jobs, run locally against the database. `scripts/home_transcript_fetcher*.py` runs on a Raspberry Pi at home, because YouTube blocks transcript fetches from AWS IP addresses.
- `tests/` has one `test_<module>.py` per module. Each test file inserts `src/<module>` into `sys.path` and does `import app`, and there is no conftest.
- The frontends are Vite + React + TypeScript + Tailwind: `admin-spa/`, `bowlerdepot-learn/` (its build prerenders pages) and `consumer-site/`. `marketing-site/` and `admin-site/` are single static `index.html` files.

## Commands

```bash
# Backend deploy: always a full, unscoped build. A scoped `sam build AdminApiFunction`
# has shipped a zip missing fastapi (runbook 6a.5). samconfig already sets
# use_container, cached and parallel. If a dependency mysteriously goes missing: sam build --clear-cache
sam build && sam deploy

# Frontends deploy through GitHub Actions on push to main (paths admin-spa/**,
# bowlerdepot-learn/**, consumer-site/**). Build locally first to catch type errors.
cd admin-spa && npm run build        # tsc -b && vite build; needs .env.local (see .env.example)
git push

# Marketing site (not in CI)
aws s3 cp marketing-site/index.html s3://<MarketingSiteBucketName>/index.html --cache-control "no-cache"

# Logs: every log group is /aws/lambda/bowling-scraper-<name>
aws logs tail /aws/lambda/bowling-scraper-price-checker --since 1h

# Tests: local venv at .venv/ (gitignored). Set it up once with pytest plus the
# deps of the functions you're testing, e.g.:
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt -r src/admin_api/requirements.txt psycopg2-binary boto3 requests pillow google-auth
# Run ONE FILE PER INVOCATION. Test files import the function under test by its bare
# module name (`app`, `service`), so two files in one pytest run shadow each other
# (e.g. admin + public API together shows ~115 bogus failures).
.venv/bin/python -m pytest -q tests/test_admin_api_service.py
```

## Conventions

- **Record every change in `DEPLOY_RUNBOOK.md`.** Add a new numbered `6xx` section before `## 7. Ongoing operations`, covering what changed, why, the deploy steps and how to verify it.
- Comments and docstrings explain the reasoning behind a change, often quoting Al's request, and they're long. Match that density in the modules you touch.
- Several discovery jobs have **no schedule on purpose** and run only when triggered, through the admin SPA's Batch Jobs tab or `aws lambda invoke`. These include URL discovery for Swag, Motiv and Storm, the video search, and the catalog-wide price discovery. Check `template.yaml` and the runbook before you assume something runs automatically.
- Admin API sort options follow one pattern: a `_*_SORT_ORDER_BY` dict plus `.get(sort, DEFAULT)`. Product images come from `coalesce(stored_url, source_url)`, choosing the thumbnail first and then the lowest `display_order`.
- Hand Al exact copy-paste commands for deploys, invokes and SQL. Commits should be authored by Al's GitHub user.
- Never write secrets into the repo or into chat. Database, BigCommerce, YouTube, Gemini and admin-token credentials live in Secrets Manager (see the ARNs in `samconfig.toml`).
