# bowlerdepot-learn

The Learn section -- BowlerDepot's own ball-review content, moved off
BigCommerce's native blog (see the main repo's `DEPLOY_RUNBOOK.md` 6u
section for the sync feature this replaces the *destination* for, not
the source: `product_articles`/`bowlerdepot_article_sync` are unchanged
and keep running in parallel, per Al's own choice when this project was
proposed).

Vite + React + TypeScript SPA, same shape as `consumer-site/` in this
repo, talking to the same unauthenticated `PublicApiFunction` (`GET
/articles`, `GET /brands`, `GET /products/{id}/article`). Branded to
match the real bowlerdepot.com storefront -- black header, the real
logo asset, Cabin font, and the storefront's own blue/navy palette (see
`src/index.css`'s header comment for exactly where those values came
from) -- so a visitor doesn't feel like they left BowlerDepot.

Two pages:

- **Learn index** (`/`) -- brand filter, search, sort (newest/oldest/
  title A-Z/Z-A), card grid linking into each article.
- **Article detail** (`/articles/:productId`) -- full review (hook,
  performance summary, pros/cons, who should buy/skip, buying tips,
  verdict, FAQ, live spec table, comparison to similar balls), plus a
  "Shop this ball at BowlerDepot" link.

## Why no real BowlerDepot product-page link

`bowlerdepot_products` (the table that maps a scraped product to its
BigCommerce listing) only stores the numeric BigCommerce product id and
SKU, not a resolvable storefront URL/slug -- BigCommerce's Stencil
storefronts don't expose a generic "view by id" route either. Rather
than invent a URL scheme this project can't verify actually resolves,
"Shop this ball" links to bowlerdepot.com's own search
(`search.php?search_query=...`), confirmed live to land a visitor
directly on the matching product's real search result. Worth revisiting
if a real stored storefront URL/slug becomes available later.

## Local dev

```bash
cd bowlerdepot-learn
npm install
cp .env.example .env.local   # fill in VITE_PUBLIC_API_URL
npm run dev
```

`VITE_PUBLIC_API_URL` is the same `PublicApiUrl` stack output
consumer-site/.env.example points at:

```bash
aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='PublicApiUrl'].OutputValue" --output text
```

## Build + deploy

```bash
npm run build          # tsc -b && vite build && npm run prerender -- outputs to dist/
```

`npm run build`'s last step (`prerender`) generates real static HTML for
every published article on top of the SPA build -- see
`scripts/prerender.ts`'s own header comment for why (SEO: a pure
client-rendered SPA has no crawlable per-article HTML the way
BigCommerce's native blog gave for free). See the main repo's
`DEPLOY_RUNBOOK.md` (Learn section) for the S3 sync + CloudFront
invalidation commands -- `LearnSiteBucket`/`LearnSiteDistribution` in
`template.yaml` are the hosting infrastructure this build deploys to,
targeting `learn.bowlerdepot.com`.

## What's not here yet

- No automated tests, and `npm install`/a real `tsc`/`vite build` were
  never run against this code -- same sandbox limitation noted in
  consumer-site/README.md (this environment's npm registry access
  returns 403). Review the TypeScript with a bit more scrutiny than
  code that was actually compiled.
- No analytics.
- The comparison-table "similar balls" cards link out to BowlerDepot
  search rather than to their own Learn article pages -- most siblings
  won't have their own approved article yet, and linking to a 404 would
  be worse than linking to a real product search.
